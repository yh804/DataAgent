"""Web 服务启动入口:python web/run.py

为什么写死 workers=1:
    LangGraph 图(_get_graph 单例)与 SQLite 检查点都是进程级单例,
    多 worker 会导致检查点与 outputs/index.jsonl 竞争写坏;
    本工具面向本地单用户,单进程 + 全局串行锁(见 web/app.py 的 _RUN_LOCK)
    就是正确模型,不要加 --workers。

只绑 127.0.0.1:本地单用户工具,不暴露到局域网,也不做鉴权。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 从任意目录启动都能工作:进程根 = 项目根(数据 / 输出 / 检查点都用相对路径)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host="127.0.0.1",
        port=8000,
        workers=1,  # 写死:多 worker 会导致检查点与 index.jsonl 竞争
        log_level="info",
    )
