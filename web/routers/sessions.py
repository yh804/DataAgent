"""会话相关端点: 列表/详情/删除、发消息(SSE)、审批、信任、trace。"""

from __future__ import annotations

import web.app as A
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from llm import LLMSettings
from web.deps import llm_override

router = APIRouter()


class ClarifyAnswer(A.BaseModel):
    """单个术语的确认结果;remember=false 表示只在本次查询生效。"""

    term: str
    definition: str = ""
    sql_hint: str = ""
    remember: bool = True


class ClarifyIn(A.BaseModel):
    """术语澄清请求体;skip=true 时按候选第一项作为默认口径继续。"""

    answers: list[ClarifyAnswer] = []
    skip: bool = False


@router.get("/api/sessions")
async def list_sessions():
    """会话列表:读 sessions.jsonl,同一 thread_id 只保留最新一条,倒序返回。

    标题兜底:历史损坏数据(含连续 ?? 或 U+FFFD)在展示层替换为「会话 <id前6位>」,
    但不修改库里存的 title 原值(靠删除清理坏数据)。
    """
    latest: dict[str, dict] = {}
    if A.SESSIONS_PATH.is_file():
        for line in A.SESSIONS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = A.json.loads(line)
            except A.json.JSONDecodeError:
                A.logger.warning("sessions.jsonl 跳过坏行: %s", line[:80])
                continue
            thread_id = record.get("thread_id")
            if thread_id:
                latest[thread_id] = record  # 后写覆盖先写 = 保留最新
    items = sorted(
        latest.values(), key=lambda x: x.get("updated_at", ""), reverse=True
    )
    for item in items:
        tid = str(item.get("thread_id") or "")
        title = str(item.get("title") or "")
        if A._is_corrupted_title(title):
            item["title_display"] = f"会话 {tid[:6]}"
            item["title_corrupted"] = True
        else:
            item["title_display"] = title
            item["title_corrupted"] = False
    return items

@router.get("/api/sessions/{thread_id}")
async def get_session(thread_id: str):
    """会话快照:供页面刷新 / 切换会话时恢复对话与审批卡。"""
    pending = await A.run_in_threadpool(A.load_pending_interrupt, thread_id)
    values: dict = {}
    try:
        snapshot = await A.run_in_threadpool(
            A.agent_main._get_graph().get_state,
            {"configurable": {"thread_id": thread_id}},
        )
        values = dict(getattr(snapshot, "values", None) or {})
    except Exception:  # noqa: BLE001 - 检查点读取异常按空会话处理
        values = {}

    if not values and pending is None:
        raise A.HTTPException(status_code=404, detail="会话不存在")

    trusted = await A.run_in_threadpool(A._load_trusted, thread_id)
    # 待审批时补策略理由与列名:刷新页面后重建的审批卡也能显示「为什么被问」
    if pending and pending.get("plan"):
        pending = await A.run_in_threadpool(A._enrich_pending, pending, values, trusted)

    if thread_id in A._RUNNING:
        status = "running"
    elif pending:
        status = "pending"
    elif values.get("error_analysis"):
        status = "error"
    elif values.get("final_answer"):
        status = "done"
    else:
        status = "idle"

    return {
        "thread_id": thread_id,
        "status": status,
        "question": values.get("question") or (pending or {}).get("question") or "",
        "plan": values.get("plan") or (pending or {}).get("plan") or [],
        "reason": values.get("plan_reason") or (pending or {}).get("reason") or "",
        "pending": pending,
        "trusted_datasets": trusted,
        "final_answer": values.get("final_answer") or "",
        "run_id": values.get("run_id") or "",
        "deliver_run_id": values.get("deliver_run_id") or "",
        "next_action": values.get("next_action") or "",
        "warnings": values.get("warnings") or [],
        "error_analysis": values.get("error_analysis") or "",
    }

@router.delete("/api/sessions/{thread_id}")
async def delete_session(thread_id: str):
    """硬删除一个会话:检查点 + sessions.jsonl + traces + approvals 脱敏 + 该会话的全部运行归档。

    会话与运行记录是绑定关系:删会话就必须删掉它产生的 runs 目录与 index 行,
    否则历史列表会留下"点开就 404"的孤儿记录。
    不是软删除:对话内容、待审批计划、执行轨迹里的问题原文都会真正消失。
    顺序:先删检查点(失败则 500,不碰文件),再清理文件与索引。
    全程持有 _RUN_LOCK,避免与刚结束的 run 抢 index/sessions。
    """
    if not A._THREAD_ID_RE.match(thread_id):
        raise A.HTTPException(status_code=400, detail="thread_id 格式非法")
    if thread_id in A._RUNNING:
        raise A.HTTPException(status_code=409, detail="该会话正在执行,请等本轮结束后再删除")

    await A._RUN_LOCK.acquire()
    try:
        # 先探测是否存在(404 判定依据),不做任何修改
        cp_exists = A._checkpoint_thread_exists(thread_id)
        sessions_present = any(
            r.get("thread_id") == thread_id for r in A._read_session_records()
        )
        trace_dir = A.OUTPUTS_DIR / "traces"
        trace_files = []
        if trace_dir.is_dir():
            trace_files = sorted(
                trace_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:A.TRACE_DELETE_SCAN_LIMIT]
        target_traces: list[A.Path] = []
        for path in trace_files:
            try:
                trace = A.json.loads(path.read_text(encoding="utf-8"))
            except (OSError, A.json.JSONDecodeError) as exc:
                A.logger.warning("删除扫描时跳过坏 trace 文件 %s: %s", path.name, exc)
                continue
            if trace.get("thread_id") == thread_id:
                target_traces.append(path)

        if not (cp_exists or sessions_present or target_traces):
            raise A.HTTPException(status_code=404, detail="会话不存在")

        # 1. 检查点:失败则 500,不删任何文件
        try:
            A.agent_main._get_graph().checkpointer.delete_thread(thread_id)
        except Exception as exc:  # noqa: BLE001 - 暴露给用户以便排查
            A.logger.exception("删除检查点失败")
            raise A.HTTPException(
                status_code=500, detail=f"删除检查点失败: {type(exc).__name__}: {exc}"
            )

        # 2. sessions.jsonl:过滤掉该 thread 的所有行
        _, sessions_removed = A._atomic_rewrite_jsonl(
            A.SESSIONS_PATH, lambda r: None if r.get("thread_id") == thread_id else r
        )

        # 3. traces:删除属于该 thread 的文件
        traces_removed = 0
        for path in target_traces:
            try:
                path.unlink()
                traces_removed += 1
            except OSError as exc:
                A.logger.warning("删除 trace 文件失败 %s: %s", path.name, exc)

        # 4. approvals.jsonl:该 thread 的 question 置空 + redacted 标记,保留决策理由与计数
        approvals_redacted = 0
        if A.APPROVALS_PATH.is_file():
            def _redact(record: dict) -> dict:
                if record.get("thread_id") != thread_id:
                    return record
                record = dict(record)
                record["question"] = ""
                record["redacted"] = True
                return record
            # 计算被修改的行数:重写后扫一遍带 redacted 的目标行
            A._atomic_rewrite_jsonl(A.APPROVALS_PATH, _redact)
            for line in A.APPROVALS_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    rec = A.json.loads(line)
                except A.json.JSONDecodeError:
                    continue
                if rec.get("thread_id") == thread_id and rec.get("redacted"):
                    approvals_redacted += 1

        # 5. 删除该 thread 的运行记录与报告(与会话绑定,不再提供开关)
        runs_removed = 0
        files_removed = 0
        run_ids = A._thread_run_ids(thread_id)
        if run_ids:
            runs_root = (A.OUTPUTS_DIR / "runs").resolve()
            for rid in run_ids:
                run_dir = (runs_root / rid).resolve()
                # 严格校验:必须恰好是 runs_root 的直接子目录,禁止穿越
                if run_dir.parent != runs_root or not run_dir.is_dir():
                    continue
                try:
                    A.shutil.rmtree(run_dir)
                    runs_removed += 1
                except OSError as exc:
                    A.logger.warning("删除 run 目录失败 %s: %s", rid, exc)
            # index.jsonl:去掉这些 run_id 的行
            if runs_removed:
                removed_set = set(run_ids)
                _, removed_idx = A._atomic_rewrite_jsonl(
                    A.OUTPUTS_DIR / "index.jsonl",
                    lambda r: None if r.get("run_id") in removed_set else r,
                )
                files_removed = removed_idx

        # 6. 审计:只留 id 与计数,绝不留问题文本
        try:
            A.DELETIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with A.DELETIONS_PATH.open("a", encoding="utf-8") as f:
                f.write(
                    A.json.dumps(
                        {
                            "ts": A.datetime.now().isoformat(timespec="seconds"),
                            "thread_id": thread_id,
                            "sessions_removed": sessions_removed,
                            "traces_removed": traces_removed,
                            "runs_removed": runs_removed,
                            "files_removed": files_removed,
                            "approvals_redacted": approvals_redacted,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as exc:
            A.logger.warning("写 deletions.jsonl 失败: %s", exc)

        return {
            "thread_id": thread_id,
            "deleted": {
                "sessions": sessions_removed,
                "traces": traces_removed,
                "runs": runs_removed,
                "approvals_redacted": approvals_redacted,
            },
        }
    finally:
        A._RUN_LOCK.release()

@router.post("/api/sessions/{thread_id}/messages")
async def post_message(
    thread_id: str,
    body: A.QuestionIn,
    _llm: LLMSettings | None = Depends(llm_override),
):
    question = (body.question or "").strip()
    if not question:
        raise A.HTTPException(status_code=400, detail="问题不能为空")
    # 附件数据集名只拼进 Agent 问题文本,让 planner 锁定数据源;
    # 会话标题与审批审计仍用用户原始问题,不暴露这层内部前缀。
    dataset_name = (body.dataset_name or "").strip()
    database_id = (body.database_id or "").strip()
    if database_id:
        if dataset_name:
            raise A.HTTPException(
                status_code=400, detail="CSV 附件与数据库连接不能同时选择"
            )
        if not A.db_connections.is_valid_id(database_id):
            raise A.HTTPException(status_code=400, detail="数据库连接 id 格式非法")
        record = await A.run_in_threadpool(A.db_connections.get_connection, database_id)
        if record is None:
            raise A.HTTPException(
                status_code=404, detail=f"数据库连接不存在: {database_id}"
            )
    agent_question = (
        f"分析 {dataset_name}：{question}"
        if dataset_name and dataset_name not in question
        else question
    )

    bridge = A.EventBridge(A.asyncio.get_running_loop())

    # 有待审批的计划时拒绝新提问:先审批,避免把暂停中的图状态搞乱
    pending = await A.run_in_threadpool(A.load_pending_interrupt, thread_id)
    if pending:
        bridge.push_local(
            {
                "event": "answer",
                "data": {
                    "thread_id": thread_id,
                    "run_id": "",
                    "status": "error",
                    "final_answer": "",
                    "error_analysis": "本会话有待审批的分析计划,请先处理审批再提问",
                    "warnings": [],
                },
            }
        )
        bridge.push_local({"event": "done", "data": {"thread_id": thread_id}})
        return A._sse_response(bridge)

    continue_session = await A.run_in_threadpool(A._session_exists, thread_id)
    A.asyncio.get_running_loop().create_task(
        A._run_serialized(
            bridge,
            lambda: A._execute_question(
                thread_id, agent_question, continue_session, bridge,
                display_question=question, database_id=database_id or None,
            ),
        )
    )
    return A._sse_response(bridge)

@router.post("/api/sessions/{thread_id}/clarify")
async def post_clarify(
    thread_id: str,
    body: ClarifyIn,
    _llm: LLMSettings | None = Depends(llm_override),
):
    """提交术语澄清:写入术语表(可选)后按原问题重跑,返回同一条 SSE 流。"""
    pending = await A.run_in_threadpool(A.db_glossary.load_pending, thread_id)
    if not pending:
        raise A.HTTPException(status_code=404, detail="没有待澄清的术语")

    database_id = str(pending.get("database_id") or "")
    original_question = str(pending.get("question") or "")
    pending_terms = [item for item in (pending.get("terms") or []) if isinstance(item, dict)]

    question = original_question
    extra_terms: list[dict] = []
    if body.skip:
        # 跳过:按候选第一项作为默认口径,只在本轮生效,不写术语表
        defaults = []
        for item in pending_terms:
            candidates = [str(value).strip() for value in item.get("candidates") or []]
            candidates = [value for value in candidates if value]
            if candidates:
                defaults.append(f"{item.get('term')}→{candidates[0]}")
        if defaults:
            question = "(以下术语按默认口径: " + "、".join(defaults) + ")\n" + original_question
    else:
        for answer in body.answers:
            term = (answer.term or "").strip()
            definition = (answer.definition or "").strip()
            if not term or not definition:
                continue
            item = {
                "term": term,
                "definition": definition,
                "sql_hint": (answer.sql_hint or "").strip(),
            }
            if answer.remember:
                await A.run_in_threadpool(
                    A.db_glossary.upsert_term,
                    database_id,
                    term,
                    item["definition"],
                    item["sql_hint"],
                )
            else:
                extra_terms.append(item)
        if not extra_terms and not any(
            (answer.term or "").strip() and (answer.definition or "").strip()
            for answer in body.answers
        ):
            raise A.HTTPException(
                status_code=400, detail="请至少确认一个术语,或选择跳过"
            )

    await A.run_in_threadpool(A.db_glossary.clear_pending, thread_id)
    bridge = A.EventBridge(A.asyncio.get_running_loop())
    continue_session = await A.run_in_threadpool(A._session_exists, thread_id)
    A.asyncio.get_running_loop().create_task(
        A._run_serialized(
            bridge,
            lambda: A._execute_question(
                thread_id, question, continue_session, bridge,
                display_question=original_question,
                database_id=database_id or None,
                extra_terms=extra_terms,
                skip_check=True,
            ),
        )
    )
    return A._sse_response(bridge)


@router.post("/api/sessions/{thread_id}/approval")
async def post_approval(
    thread_id: str,
    body: A.ApprovalIn,
    _llm: LLMSettings | None = Depends(llm_override),
):
    if body.action not in ("approve", "edit", "reject"):
        raise A.HTTPException(
            status_code=400, detail="action 必须是 approve / edit / reject 之一"
        )
    if body.action == "edit":
        plan = body.plan or []
        if not plan or not all(isinstance(step, str) and step.strip() for step in plan):
            raise A.HTTPException(
                status_code=400, detail="edit 时 plan 必须是非空字符串数组"
            )

    decision = {"action": body.action, "plan": body.plan or []}
    bridge = A.EventBridge(A.asyncio.get_running_loop())
    A.asyncio.get_running_loop().create_task(
        A._run_serialized(
            bridge, lambda: A._execute_approval(thread_id, decision, body.trust, bridge)
        )
    )
    return A._sse_response(bridge)

@router.post("/api/sessions/{thread_id}/trust")
async def post_trust(thread_id: str, body: A.TrustIn):
    """把数据集加入 / 移出本会话信任列表(写入 sessions.jsonl 最新记录)。"""
    dataset = (body.dataset or "").strip()
    if not dataset:
        raise A.HTTPException(status_code=400, detail="dataset 不能为空")
    await A.run_in_threadpool(A._set_trusted, thread_id, dataset, body.trusted)
    return {
        "thread_id": thread_id,
        "dataset": dataset,
        "trusted_datasets": await A.run_in_threadpool(A._load_trusted, thread_id),
    }

@router.get("/api/sessions/{thread_id}/trace")
async def get_session_trace(thread_id: str):
    """该会话最新一份 trace;最多回扫最新 50 个文件,找不到返回 404。"""
    trace_dir = A.OUTPUTS_DIR / "traces"
    if not trace_dir.is_dir():
        raise A.HTTPException(status_code=404, detail="暂无 trace")
    files = sorted(
        trace_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:A.TRACE_SCAN_LIMIT]
    for path in files:
        try:
            trace = A.json.loads(path.read_text(encoding="utf-8"))
        except (OSError, A.json.JSONDecodeError):
            continue
        if trace.get("thread_id") == thread_id:
            return trace
    raise A.HTTPException(status_code=404, detail="该会话暂无 trace")
