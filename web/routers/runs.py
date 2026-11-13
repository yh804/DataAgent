"""运行归档端点: 列表/清空、详情、删除、图表与导出文件。"""

from __future__ import annotations

import web.app as A
import db_connections
from datetime import datetime
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/api/runs")
async def list_runs(limit: int = 50):
    """运行历史:读 index.jsonl 倒序;坏行跳过并记日志,绝不让一条坏数据搞挂整表。"""
    index_path = A.OUTPUTS_DIR / "index.jsonl"
    items: list[dict] = []
    if index_path.is_file():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(A.json.loads(line))
            except A.json.JSONDecodeError:
                A.logger.warning("index.jsonl 跳过坏行: %s", line[:80])
                continue
    items.reverse()
    result = items[: max(limit, 1)]
    # 数据库问答审计只读一次:按 csv_name 建索引,旧记录没有 csv_name 时再按问题+时间兜底匹配
    query_records = db_connections.read_queries(500)
    query_by_csv = {
        str(record.get("csv_name")): record
        for record in query_records
        if record.get("csv_name")
    }
    for item in result:
        # 数据库来源的运行:补上可读库名、表名与 SQL;旧 index 行没有这些字段
        dataset = str(item.get("dataset") or "")
        parsed = db_connections.parse_db_dataset(dataset)
        if parsed:
            item["db_label"] = item.get("db_label") or parsed["label"]
            record = query_by_csv.get(dataset) or _match_query_record(item, query_records)
            if record:
                item["db_label"] = record.get("db_label") or item["db_label"]
                item["db_sql"] = record.get("sql") or ""
                item["db_table"] = _table_from_sql(item["db_sql"])
                item["db_question"] = record.get("question") or ""
        item.setdefault("db_label", "")
        item.setdefault("db_row_count", None)
        item.setdefault("db_table", "")
        item.setdefault("sql_available", False)
    return result


def _table_from_sql(sql: str) -> str:
    """从 SQL 的第一个 FROM 子句里取出表名;取不到返回空串。

    Args:
        sql: 生成的 SELECT 语句。

    Returns:
        表名(不含反引号);SQL 为空或格式不认识时返回空串。
    """
    if not sql:
        return ""
    match = A.re.search(r"\bfrom\s+`?([A-Za-z0-9_\u4e00-\u9fff]+)`?", sql, A.re.IGNORECASE)
    return match.group(1) if match else ""


def _match_query_record(run: dict, records: list[dict]) -> dict | None:
    """旧审计记录没有 csv_name 时,按「问题结尾相同 + 时间相近」兜底匹配。

    Args:
        run: index.jsonl 里的一行运行记录。
        records: 数据库问答审计列表(按时间倒序)。

    Returns:
        匹配到的审计记录;匹配不到返回 None。
    """
    question = str(run.get("question") or "")
    run_ts = _parse_ts(run.get("ts"))
    for record in records:
        asked = str(record.get("question") or "")
        if not asked or not question.endswith(asked):
            continue
        # 查询先于运行落库,时间差通常在几分钟内;没有 ts 的旧行不做时间约束
        record_ts = _parse_ts(record.get("ts"))
        if run_ts and record_ts and abs((record_ts - run_ts).total_seconds()) > 900:
            continue
        return record
    return None


def _parse_ts(value: object) -> datetime | None:
    """解析审计与 index 里两种时间写法(空格或 T 分隔),失败返回 None。

    Args:
        value: 时间字符串,如 "2026-11-13T20:08:08+08:00" 或 "2026-11-13 20:10:23"。

    Returns:
        去掉时区后的 datetime;无法解析时返回 None。
    """
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    text = text[:26]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None

@router.delete("/api/runs")
async def clear_runs():
    """一键清空全部运行记录:归档目录 + index.jsonl + 会话里的 last_run_id。

    与单条删除共用 _RUN_LOCK;任何会话在跑都直接 409,避免删掉正在写入的归档。
    待删集合取「index 行」与「runs 目录」的并集:前者覆盖索引有行但目录已丢的情况,
    后者覆盖目录还在但索引缺行的孤儿目录。
    """
    if A._RUNNING:
        raise A.HTTPException(status_code=409, detail="有会话正在执行,请等本轮结束后再清空")

    await A._RUN_LOCK.acquire()
    try:
        runs_root = (A.OUTPUTS_DIR / "runs").resolve()
        index_path = A.OUTPUTS_DIR / "index.jsonl"
        run_ids: set[str] = set()
        if index_path.is_file():
            for line in index_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = str(A.json.loads(line).get("run_id") or "")
                except A.json.JSONDecodeError:
                    continue
                if A._RUN_ID_RE.match(rid):
                    run_ids.add(rid)
        if runs_root.is_dir():
            for child in runs_root.iterdir():
                if child.is_dir() and A._RUN_ID_RE.match(child.name):
                    run_ids.add(child.name)

        runs_removed = 0
        bytes_freed = 0
        for rid in sorted(run_ids):
            run_dir = (runs_root / rid).resolve()
            # 严格校验:必须恰好是 runs_root 的直接子目录,防路径穿越
            if run_dir.parent != runs_root or not run_dir.is_dir():
                continue
            try:
                for path in run_dir.rglob("*"):
                    if not path.is_file():
                        continue
                    try:
                        bytes_freed += path.stat().st_size
                    except OSError:
                        continue
                A.shutil.rmtree(run_dir)
                runs_removed += 1
            except OSError as exc:
                # 单个目录删不掉不阻断其余目录,最后用计数告诉前端实际删了多少
                A.logger.warning("清空运行记录时删除 %s 失败: %s", rid, exc)

        # index.jsonl:全部行清空(坏行也一并丢掉,因为索引已无意义)
        _, index_rows = A._atomic_rewrite_jsonl(index_path, lambda record: None)

        # sessions.jsonl:清空所有 last_run_id,避免留下指向已删运行的悬空引用
        sessions_touched = 0

        def _clear_run_ref(record: dict) -> dict:
            nonlocal sessions_touched
            if not record.get("last_run_id"):
                return record
            sessions_touched += 1
            updated = dict(record)
            updated["last_run_id"] = ""
            return updated

        A._atomic_rewrite_jsonl(A.SESSIONS_PATH, _clear_run_ref)

        # 审计:批量删除只留计数,便于回溯
        try:
            A.DELETIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with A.DELETIONS_PATH.open("a", encoding="utf-8") as f:
                f.write(
                    A.json.dumps(
                        {
                            "ts": A.datetime.now().isoformat(timespec="seconds"),
                            "kind": "all_runs",
                            "runs_removed": runs_removed,
                            "index_rows_removed": index_rows,
                            "sessions_touched": sessions_touched,
                            "bytes_freed": bytes_freed,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as exc:
            A.logger.warning("写 deletions.jsonl 失败: %s", exc)

        return {
            "deleted": {
                "runs": runs_removed,
                "index_rows": index_rows,
                "sessions_touched": sessions_touched,
                "bytes_freed": bytes_freed,
            }
        }
    finally:
        A._RUN_LOCK.release()

@router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run_dir = A._resolve_run_dir(run_id)
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise A.HTTPException(status_code=404, detail="运行记录缺少 result.json")
    try:
        result = A.json.loads(result_path.read_text(encoding="utf-8"))
    except A.json.JSONDecodeError:
        raise A.HTTPException(status_code=500, detail="result.json 已损坏") from None
    # 历史归档里可能残留裸 NaN(旧版 json.dumps 未过滤),浏览器 JSON.parse 会报错
    result = A.json_safe(result)
    report_path = run_dir / "report.md"
    report_md = (
        report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    )
    return {"result": result, "report_md": report_md}

@router.delete("/api/runs/{run_id}")   # 定义一个HTTP DELETE接口 客户端调用它来删除指定某次运行
async def delete_run(run_id: str):
    """永久删除单次运行:归档目录 + index.jsonl 行 + 会话里的 last_run_id 引用。

    这是单次运行的硬删除,不碰属于会话的 traces / checkpoints / approvals。
    404 判定与路径穿越校验统一由 _resolve_run_dir 完成;409 语义与 delete_session 一致。
    """
    run_dir = A._resolve_run_dir(run_id)
    # result.json 只用来取所属会话;读不到(缺失/损坏)不阻断删除,按空处理
    thread_id = ""
    try:
        result = A.json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        thread_id = str(result.get("thread_id") or "")
    except (OSError, A.json.JSONDecodeError):
        thread_id = ""

    # 会话正在执行时不允许删该会话的 run,避免执行线程还在写这个归档目录
    if thread_id and thread_id in A._RUNNING:
        raise A.HTTPException(status_code=409, detail="该运行所属会话正在执行,请稍后再删")

    await A._RUN_LOCK.acquire()
    try:
        # 删前统计体积:单个文件 stat 失败只跳过,绝不让统计阻断删除
        bytes_freed = 0
        try:
            for path in run_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    bytes_freed += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            # 目录枚举本身失败时保持 bytes_freed=0,继续删除
            bytes_freed = 0

        try:
            A.shutil.rmtree(run_dir)
        except OSError as exc:
            A.logger.exception("删除运行归档目录失败: %s", run_id)
            raise A.HTTPException(
                status_code=500, detail=f"删除运行归档失败: {type(exc).__name__}: {exc}"
            )

        # index.jsonl:去掉 run_id 相等的行
        _, index_rows_removed = A._atomic_rewrite_jsonl(
            A.OUTPUTS_DIR / "index.jsonl",
            lambda r: None if r.get("run_id") == run_id else r,
        )

        # sessions.jsonl:凡引用该 run 的会话只清空 last_run_id,其余字段原样保留
        sessions_touched = 0

        def _clear_run_ref(record: dict) -> dict:
            nonlocal sessions_touched
            if record.get("last_run_id") != run_id:
                return record
            sessions_touched += 1
            updated = dict(record)
            updated["last_run_id"] = ""
            return updated

        A._atomic_rewrite_jsonl(A.SESSIONS_PATH, _clear_run_ref)

        # 审计:只留 id 与计数,便于回溯批量删除
        try:
            A.DELETIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with A.DELETIONS_PATH.open("a", encoding="utf-8") as f:
                f.write(
                    A.json.dumps(
                        {
                            "ts": A.datetime.now().isoformat(timespec="seconds"),
                            "kind": "run",
                            "run_id": run_id,
                            "thread_id": thread_id,
                            "index_rows_removed": index_rows_removed,
                            "sessions_touched": sessions_touched,
                            "bytes_freed": bytes_freed,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as exc:
            A.logger.warning("写 deletions.jsonl 失败: %s", exc)

        return {
            "run_id": run_id,
            "deleted": {
                "index_rows": index_rows_removed,
                "sessions_touched": sessions_touched,
                "bytes_freed": bytes_freed,
            },
        }
    finally:
        A._RUN_LOCK.release()

@router.get("/api/runs/{run_id}/charts/{name}")
async def get_run_chart(run_id: str, name: str):
    """图表文件:resolve 后必须恰好落在 runs/<run_id>/charts/ 下,否则 404。"""
    run_dir = A._resolve_run_dir(run_id)
    charts_dir = (run_dir / "charts").resolve()
    target = (charts_dir / name).resolve()
    # 严格相等判断:%2e%2e / 绝对路径 / 子目录拼接都会被 resolve 成别处
    if target.parent != charts_dir or not target.is_file():
        raise A.HTTPException(status_code=404, detail="图表不存在")
    return A.FileResponse(target)

@router.get("/api/runs/{run_id}/report.html")
async def get_run_report_html(run_id: str):
    """单文件 HTML 报告(离线可开、图表内嵌);旧运行没有该文件时返回 404。"""
    # 统一路径解析：完成 run_id → 真实目录的映射、防路径穿越、目录不存在抛 404
    # 这是项目里所有 run 相关接口的安全入口，不在每个接口里重复写校验
    run_dir = A._resolve_run_dir(run_id)
    # 定位报告文件本身
    target = run_dir / "report.html"
    # 文件不存在时返回 404 并给友好提示：可能是升级前的旧运行，没跑过 HTML 生成步骤
    if not target.is_file():
        raise A.HTTPException(
            status_code=404, detail="该运行没有 HTML 报告(可能是旧运行)"
        )
    # FileResponse：FastAPI 提供的静态文件响应
    # - 自动按文件大小设 Content-Length
    # - 支持 Range 请求（大文件断点续传）
    # - media_type 告诉浏览器这是 HTML，直接渲染而不是下载
    return A.FileResponse(target, media_type="text/html; charset=utf-8")

@router.get("/api/runs/{run_id}/result.csv")
async def get_run_result_csv(run_id: str):
    """结果表 CSV(沙箱真实 DataFrame 导出);没有导出时返回 404。"""
    # 同样走统一路径解析，保证安全
    run_dir = A._resolve_run_dir(run_id)
    target = run_dir / "result.csv"
    # 没有导出文件说明这次运行（比如纯文本分析、画图、统计）没产出表格
    if not target.is_file():
        raise A.HTTPException(status_code=404, detail="本次运行没有可导出的结果表")
    # FileResponse 下载 CSV：
    # - media_type 指定 text/csv，Excel 能正确识别中文编码
    # - filename 是关键：告诉浏览器「另存为」时用什么文件名，
    #   否则浏览器会直接用 URL 里的 result.csv 当默认名，不够语义化
    #   这里改成 {run_id}.csv，下载后一眼能对上是哪次运行
    return A.FileResponse(
        target,
        media_type="text/csv; charset=utf-8",
        filename=f"{run_id}.csv",
    )
