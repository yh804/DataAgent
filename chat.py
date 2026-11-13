"""交互式 CLI：多轮对话 + 人工审批入口。

启动后生成固定 thread_id,后续每轮都续跑同一检查点,
让 Agent 记住之前锁定的数据集与分析结论,支持「那对照组呢」这类指代。

每轮流程：
    1. run_agent 启动图;若意图是 analyze,图会暂停在 review_plan(HITL);
    2. 打印 planner 的计划与数据集,询问 y / e / n;
    3. y=执行、e=编辑计划后执行、n=取消(不执行任何代码);
    4. 用 resume_agent 恢复同一 thread,继续走到结束。

限制：
    检查点由 checkpoint.build_checkpointer() 装配(默认 SQLite,
    outputs/checkpoints.sqlite,CHECKPOINT_PATH 可覆盖),进程退出后状态仍在;
    因此暂停的审批既可在本进程继续,也可在新进程用 --resume 回读检查点后恢复。

用法：
    python chat.py                          # 交互式多轮对话(新建会话)
    python chat.py --thread-id <id>         # 用指定会话继续对话
    python chat.py --resume <thread_id>     # 跨进程恢复该会话的待审批计划(y/e/n)

退出：
    输入 exit / quit / 退出,或按 Ctrl+C / Ctrl+D。
"""

import argparse
import re
import sys
import uuid

# 引用 main 模块以读取 LAST_TRACE_PATH(本文件也有 main() 函数,故用别名避免遮蔽)
import main as main_module

# 对外入口：启动 / 恢复 / 判断是否暂停等待审批 / 从检查点回读待审批项
from main import (
    get_pending_interrupt,
    load_pending_interrupt,
    resume_agent,
    run_agent,
)


def _edit_plan(plan: list[str]) -> list[str]:
    """读取用户编辑后的计划步骤。

    参数：
        plan: planner 生成的原始计划步骤列表。

    返回：
        新计划步骤列表。多条用分号(; 或 ;)分隔;直接回车则沿用原计划。

    为什么用分号分隔而不是逐行输入：
        交互式 input() 逐行读取会把「空行」当作结束的歧义点,
        单行分号分隔更简单,也便于用户一次改完所有步骤。
    """
    print("[审批] 输入新计划,用分号 ; 分隔多条步骤;直接回车沿用原计划")
    text = input("新计划：").strip()
    if not text:
        return list(plan)
    return [step.strip() for step in re.split(r"[;;]", text) if step.strip()]


def _handle_approval(state: dict, thread_id: str) -> dict:
    """若图暂停在 review_plan,完成一次人工审批并返回恢复后的状态。

    参数：
        state: run_agent 返回的状态(可能含待审批 interrupt)。
        thread_id: 当前会话标识,用于 resume_agent 定位同一检查点。

    返回：
        恢复执行后的状态;若本轮本来就没有待审批项,原样返回 state。
    """
    pending = get_pending_interrupt(state)
    if pending is None:
        return state

    dataset = pending.get("dataset_name") or "(未锁定数据集)"
    plan = pending.get("plan") or []
    reason = pending.get("reason") or ""

    print("\n[审批] Agent 计划如下：")
    print(f"  数据集：{dataset}")
    if plan:
        for index, step in enumerate(plan, 1):
            print(f"  {index}. {step}")
    else:
        print("  (本次没有生成明确步骤)")
    if reason:
        print(f"  依据：{reason}")

    while True:
        choice = input("[y] 执行 / [e] 编辑计划 / [n] 取消：").strip().lower()

        if choice in ("y", "yes"):
            decision = {"action": "approve", "plan": plan}
            break

        if choice in ("n", "no"):
            # reject：不生成、不执行任何代码,直接结束本轮
            decision = {"action": "reject", "plan": []}
            break

        if choice in ("e", "edit"):
            new_plan = _edit_plan(plan)
            if not new_plan:
                print("[审批] 计划不能为空,请重新选择。")
                continue
            decision = {"action": "edit", "plan": new_plan}
            break

        print("请输入 y / e / n。")

    return resume_agent(thread_id, decision)


def _print_result(state: dict) -> None:
    """按优先级打印本轮结果：成功结论 > 结构化错误 > 最后一条 AI 消息。

    参数：
        state: 图执行结束后的完整状态字典。

    为什么要有第三档：
        discover / clarify 分支不写 final_answer / error_analysis,
        它们的结论只存在于 messages 的最后一条。
    """
    final_answer = state.get("final_answer", "")
    error_analysis = state.get("error_analysis", "")
    if final_answer:
        print(f"\nAgent：\n{final_answer}\n")
    elif error_analysis:
        print(f"\nAgent(分析失败)：\n{error_analysis}\n")
    else:
        messages = state.get("messages", [])
        if messages:
            print(f"\nAgent：\n{messages[-1].content}\n")


def _resume_interactive(thread_id: str) -> int:
    """跨进程恢复：回读 SQLite 检查点里的待审批项,走与本进程一致的 y/e/n 审批。

    参数：
        thread_id: 之前 run_agent 使用的会话标识(从 chat.py 退出提示里拿到)。

    返回：
        进程退出码：0=正常(含「没有待审批项」),1=恢复过程异常。

    为什么先回读检查点而不是直接 resume：
        新进程拿不到原 state;盲目 resume 会在不知道计划内容的情况下执行代码,
        与 HITL 的初衷相悖。
    """
    pending = load_pending_interrupt(thread_id)
    if pending is None:
        # 正常情况(已审批完 / 已结束 / thread_id 打错),明确提示后正常退出
        print(f"[resume] 会话 {thread_id} 没有待审批的计划。")
        return 0

    # 把检查点里的 payload 包成 get_pending_interrupt 认识的形态,
    # 复用 _handle_approval 的展示与 y/e/n 交互,保证两种入口语义完全一致
    state = {"__interrupt__": [{"value": pending}]}
    try:
        state = _handle_approval(state, thread_id)
    except (EOFError, KeyboardInterrupt):
        # 审批中途 Ctrl+C:检查点仍在,下次可再 --resume
        print(f"\n[resume] 已取消,计划仍待审批;可再次 python chat.py --resume {thread_id}")
        return 0
    _print_result(state)
    if main_module.LAST_TRACE_PATH:
        print(f"[trace] {main_module.LAST_TRACE_PATH}")
    return 0


def main() -> int:
    """交互式主循环：读用户输入 -> 启动图 -> 按需审批 -> 打印结果。

    会话状态管理：
        - thread_id 缺省启动时生成,也可用 --thread-id 指定已有会话;
        - 新建会话第一轮用 continue_session=False 做完整初始化(登记 data_sources);
        - 后续轮 / --thread-id 续聊用 continue_session=True(继承 data_sources /
          dataset_name,同时重置 iteration / error_analysis / final_answer 等轮级字段)。
        - 传入 --resume 时切到「跨进程恢复」模式,不再进入交互循环。
    """
    parser = argparse.ArgumentParser(description="交互式数据分析对话 / 跨进程恢复审批")
    parser.add_argument(
        "--thread-id", dest="thread_id", default=None,
        help="使用指定 thread_id 继续会话(缺省新建会话)",
    )
    parser.add_argument(
        "--resume", metavar="THREAD_ID", default=None,
        help="恢复指定会话的待审批计划(跨进程),进入 y/e/n 审批交互",
    )
    args = parser.parse_args()

    if args.resume:
        return _resume_interactive(args.resume)

    # --thread-id 视为「续聊已有会话」,第一轮也用续跑模式,保留其 data_sources 等上下文
    continued = bool(args.thread_id)
    thread_id = args.thread_id or uuid.uuid4().hex
    print(f"[chat] 会话已启动,thread_id = {thread_id}")
    print("[chat] 输入 exit / quit / 退出 结束会话;分析类问题会先展示计划等待审批\n")

    first_turn = not continued
    while True:
        try:
            question = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D 优雅退出,不打印堆栈
            print("\n[chat] 会话结束")
            break

        # 空输入不触发图执行,避免浪费 LLM 调用
        if not question:
            continue
        if question in ("exit", "quit", "退出"):
            print("[chat] 会话结束")
            break

        try:
            # 第一轮：完整初始化(覆盖任何旧检查点);
            # 后续轮：续跑(data_sources / dataset_name 跨轮继承)
            state = run_agent(
                question,
                thread_id=thread_id,
                continue_session=not first_turn,
            )
            first_turn = False
            # analyze 意图会暂停在 review_plan,这里完成 y/e/n 审批并恢复
            state = _handle_approval(state, thread_id)
        except (EOFError, KeyboardInterrupt):
            print("\n[chat] 会话结束")
            break

        _print_result(state)
        # 打印本轮 trace 文件路径,便于事后排查节点 / LLM / 工具耗时
        if main_module.LAST_TRACE_PATH:
            print(f"[trace] {main_module.LAST_TRACE_PATH}")

    # 退出时打印 thread_id:进程退出后仍可用 --resume 恢复未完成的审批
    print(f"[chat] thread_id = {thread_id}")
    print(f"[chat] 下次继续本会话:python chat.py --thread-id {thread_id}")
    print(f"[chat] 若有未完成的审批:python chat.py --resume {thread_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
