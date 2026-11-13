"""冒烟自检：不依赖 Docker、不调用 LLM,验证核心模块结构完好。

检查项：
1. import 链：state / prompts / llm / tools / sandbox / trace / db / stats /
   checkpoint / analyze / ask_stats / ask_db / main / chat 全部可导入;
2. sandbox.make_result 返回结构(7 个固定键);
3. db.validate_select 基本行为(SELECT 放行,DROP / 多语句拒绝);
4. tools._detect_encoding 能识别 utf-8 与 gbk 编码文件;
5. checkpoint.build_checkpointer() 在 memory / sqlite 两种后端下都能返回对象。

用法：
    python scripts/smoke.py     # 全部 OK 退出码 0,任一 FAIL 退出码 1
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 把项目根目录加入 sys.path,使脚本在 scripts/ 子目录下可直接运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_failures: list[str] = []


def check(name: str, fn) -> None:
    """执行单项检查,打印 OK / FAIL;失败不中断后续检查。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - 冒烟 harness 必须捕获一切异常继续后续检查
        _failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"OK   {name}")


def check_imports() -> None:
    # 逐个导入并断言模块对象非空,捕获 ImportError / 模块级副作用异常
    for module_name in (
        "state", "prompts", "llm", "tools", "sandbox", "trace",
        "db", "stats", "checkpoint", "analyze", "ask_stats", "ask_db",
        "main", "chat",
    ):
        module = __import__(module_name)
        assert module is not None, f"{module_name} 导入为空"


def check_make_result() -> None:
    import sandbox

    result = sandbox.make_result(stdout="hello", exit_status="success")
    expected = {"stdout", "stderr", "return_value", "charts", "files", "duration_ms", "exit_status"}
    assert set(result.keys()) == expected, f"键不匹配: {set(result.keys())}"
    assert result["stdout"] == "hello" and result["exit_status"] == "success"
    assert result["charts"] == [] and result["files"] == []


def check_validate_select() -> None:
    import db

    # 合法单条 SELECT 放行并返回 SQL 文本
    out = db.validate_select("SELECT id FROM orders")
    assert isinstance(out, str) and "SELECT" in out.upper()
    # 危险关键字拒绝
    for bad in ("DROP TABLE orders", "SELECT 1; DELETE FROM orders", "DELETE FROM orders"):
        try:
            db.validate_select(bad)
        except ValueError:
            continue
        raise AssertionError(f"危险 SQL 未被拒绝: {bad!r}")


def check_detect_encoding() -> None:
    from tools import _detect_encoding

    with tempfile.TemporaryDirectory() as tmp:
        utf8_file = Path(tmp) / "a.csv"
        utf8_file.write_text("列1,列2\n你好,世界\n", encoding="utf-8")
        enc = _detect_encoding(utf8_file)
        assert enc in ("utf-8", "utf-8-sig"), f"utf-8 文件识别为 {enc}"

        gbk_file = Path(tmp) / "b.csv"
        gbk_file.write_bytes("列1,列2\n你好,世界\n".encode("gbk"))
        enc = _detect_encoding(gbk_file)
        assert enc in ("gbk", "gb18030"), f"gbk 文件识别为 {enc}"


def check_build_checkpointer() -> None:
    import checkpoint

    # memory 后端:不产生磁盘文件
    os.environ["CHECKPOINT_BACKEND"] = "memory"
    saver = checkpoint.build_checkpointer()
    assert hasattr(saver, "put") and hasattr(saver, "get_tuple"), "memory 后端返回对象不完整"

    # sqlite 后端:指向临时目录,不污染 outputs/
    os.environ["CHECKPOINT_BACKEND"] = "sqlite"
    os.environ["CHECKPOINT_PATH"] = str(Path(tempfile.gettempdir()) / "smoke_checkpoints.sqlite")
    saver = checkpoint.build_checkpointer()
    assert hasattr(saver, "put") and hasattr(saver, "get_tuple"), "sqlite 后端返回对象不完整"

    # 未知后端应打印警告并降级,而不是崩溃
    os.environ["CHECKPOINT_BACKEND"] = "bogus"
    saver = checkpoint.build_checkpointer()
    assert saver is not None, "未知后端未降级"


def main() -> int:
    print("[smoke] 开始自检(不依赖 Docker / LLM)...\n")
    check("import 链", check_imports)
    check("make_result 结构", check_make_result)
    check("db.validate_select", check_validate_select)
    check("tools._detect_encoding", check_detect_encoding)
    check("checkpoint.build_checkpointer", check_build_checkpointer)

    print()
    if _failures:
        print(f"[smoke] 失败 {len(_failures)} 项: {', '.join(_failures)}")
        return 1
    print("[smoke] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
