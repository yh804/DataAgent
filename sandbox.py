"""沙箱后端：AI 生成代码的唯一执行环境。

设计约束(对应项目红线)：
- 主进程只用 subprocess 拉起容器 / 调用云端 API,绝不 exec / eval AI 代码;
- 所有后端实现统一接口 Sandbox.execute(code) -> dict,返回值结构固定;
- 一切异常(Docker 未启动、镜像缺失、超时、网络失败)都必须结构化返回,
  禁止向调用方抛出让图崩溃的未捕获异常。

后端切换：环境变量 SANDBOX_BACKEND = docker | e2b(默认 docker)。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# 产物目录：路径收敛为常量,避免魔法路径散落各处
OUTPUT_DIR = Path("outputs")
# 数据目录默认值：入口可通过 SANDBOX_DATA_DIR 在运行时覆盖(analyze.py 用此机制)
DEFAULT_DATA_DIR = "data/processed"


def get_data_dir() -> Path:
    """返回当前数据目录(每次执行时读取环境变量 SANDBOX_DATA_DIR)。

    为什么不在导入时读取：analyze.py 会在运行期设置该变量,
    模块级常量会捕获导入时的旧值,导致 Docker 挂载的仍是 data/processed。
    """
    return Path(os.getenv("SANDBOX_DATA_DIR") or DEFAULT_DATA_DIR)


# 沙箱配置统一由环境变量驱动,便于在 Docker / E2B 之间切换而不改代码
SANDBOX_BACKEND = os.getenv("SANDBOX_BACKEND", "docker").strip().lower()
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "data-agent-sandbox:py311")
SANDBOX_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "30"))
# stdout 只保留尾部,避免大输出撑爆 LLM 上下文
STDOUT_LIMIT = 2000
# 图表类产物后缀,用于从 outputs 目录中区分 charts 与普通 files
CHART_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".html", ".pdf"}

# 容器内的固定挂载点约定：代码文件 /code.py,数据 /data,产物 /outputs
_CONTAINER_CODE_PATH = "/code.py"
_CONTAINER_DATA_DIR = "/data"
_CONTAINER_OUTPUT_DIR = "/outputs"


def make_result(
    *,
    stdout: str = "",
    stderr: str = "",
    return_value: Any = None,
    charts: list[str] | None = None,
    files: list[str] | None = None,
    duration_ms: int = 0,
    exit_status: str = "error",
) -> dict:
    """统一构造结构化执行结果,保证所有后端、所有分支返回同构字典。

    参数：
        stdout: AI 代码在沙箱内的标准输出文本(如 print 的结果)。
        stderr: AI 代码在沙箱内的标准错误文本(异常堆栈等)。
        return_value: AI 代码的返回值(当前暂未捕获,固定为 None)。
        charts: 沙箱内生成的图表文件路径列表。
        files: 沙箱内生成的所有产物文件路径列表(含 charts)。
        duration_ms: AI 代码从开始执行到结束的耗时(毫秒)。
        exit_status: 执行状态标识,取值 "success" 或 "error"。

    路径约定(写死,所有后端必须遵守)：
        charts / files 一律为「相对 outputs/ 的 posix 路径」,不带 outputs/ 前缀
        (如 "chart_xxx.png" 或 "e2b_chart_xxx.png");由 main.py 统一拼 outputs/ 前缀。
    """
    return {
        "stdout": stdout[-STDOUT_LIMIT:],
        "stderr": stderr,
        "return_value": return_value,
        "charts": charts or [],
        "files": files or [],
        "duration_ms": duration_ms,
        "exit_status": exit_status,
    }


class Sandbox(ABC):
    """沙箱统一接口：任何后端都必须实现 execute(code) -> dict。

    返回值结构固定为：
    {stdout, stderr, return_value, charts, files, duration_ms, exit_status}
    """

    @abstractmethod
    def execute(self, code: str) -> dict:
        """在隔离环境中执行 AI 生成的代码并返回结构化结果。

        参数：
            code: 由 LLM 生成的 Python 源码字符串,必须是可独立运行的完整脚本
                  (含 import 语句,不依赖外部变量)。

        返回值结构固定为：
            {stdout, stderr, return_value, charts, files, duration_ms, exit_status}
        """
        raise NotImplementedError


def _docker_host_path(path: Path) -> str:
    """把本地路径转成 docker -v 可用的宿主机路径。

    参数：
        path: 本地文件或目录的 Path 对象(如 /home/user/data)。

    返回：
        可直接用于 docker run -v 参数的绝对路径字符串。
        Windows 上使用原生绝对路径(D:\\...),Docker Desktop 可直接识别;
        Linux/macOS 下 str(resolve()) 即为标准绝对路径,无需额外转换。
    """
    return str(Path(path).resolve())


def _collect_outputs(
    out_dir: Path,
    baseline: dict[str, tuple[int, int]] | None = None,
) -> tuple[list[str], list[str]]:
    """从产物目录收集文件清单,并按后缀区分图表与普通文件。

    参数：
        out_dir: 沙箱执行后存放产物的本地目录 Path 对象。
        baseline: 执行前已存在文件的「相对路径 -> (mtime_ns, size)」快照(可选)。
                  传入后只回收本次新增或本次被改写的文件：同名覆盖(mtime/size
                  变化)也算本次产物,避免把「同名重绘的图表」误判为旧文件。

    返回：
        (files, charts)：files 是本次新增/改写的产物文件相对路径列表(相对于 out_dir),
        charts 是 files 中符合图表后缀(.png / .svg / .html 等)的子集。
        若 out_dir 不存在则返回两个空列表。
    """
    files: list[str] = []
    charts: list[str] = []
    if not out_dir.is_dir():
        return files, charts
    for item in sorted(out_dir.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(out_dir).as_posix()
        stat = item.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        # 仅跳过「执行前就存在且本次未改动」的文件;同名覆盖算本次新增
        if baseline is not None and baseline.get(relative) == signature:
            continue
        files.append(relative)
        if item.suffix.lower() in CHART_SUFFIXES:
            charts.append(relative)
    return files, charts


class DockerSandbox(Sandbox):
    """Docker 沙箱后端：python:3.11-slim + 预装 pandas / numpy 的镜像。

    安全边界由容器参数保证：无网络、内存 1g、1 CPU、30s 超时、
    代码文件与数据目录只读挂载、产物写独立可写目录。
    """

    def execute(self, code: str) -> dict:
        """在 Docker 容器中执行 AI 生成的 Python 代码。

        参数：
            code: AI 生成的完整 Python 源码字符串(含 import,不依赖外部变量)。

        安全边界：
            --network none(无网络)、--memory 1g、--cpus 1、30s 超时强杀;
            代码文件只读挂 /code.py、数据目录只读挂 /data、产物目录可写挂 /outputs。
        """
        start = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - start) * 1000)

        # Docker Desktop 未安装 / 未启动时,必须返回结构化 error 而不是抛异常
        if shutil.which("docker") is None:
            return make_result(
                stderr=(
                    "DockerError: 未检测到 docker 命令。请安装并启动 Docker Desktop,"
                    "或设置 SANDBOX_BACKEND=e2b 切换云端沙箱。"
                ),
                duration_ms=elapsed(),
            )

        # 临时工作目录：代码文件单独只读挂载为 /code.py;
        # 产物目录直接用项目 outputs/(可写挂载),让 charts/files 的相对路径
        # 对用户可直接访问,也避免临时目录泄漏
        workspace = Path(tempfile.mkdtemp(prefix="agent_sandbox_"))
        code_file = workspace / "code.py"
        out_dir = OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        # 记录执行前已存在的产物：执行后只回收本次新增文件,
        # 避免历史图表 / 历史归档被误算进本次结果
        baseline = {
            p.relative_to(out_dir).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in out_dir.rglob("*")
            if p.is_file()
        }
        # utf-8 写入,避免中文注释在不同平台默认编码下损坏
        code_file.write_text(code, encoding="utf-8")

        container_name = f"agent-sandbox-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker", "run", "--rm",
            "--name", container_name,
            "--network", "none",
            "--memory", "1g",
            "--cpus", "1",
            # 代码文件只读挂载为 /code.py,防止 AI 代码篡改自身输入
            "-v", f"{_docker_host_path(code_file)}:{_CONTAINER_CODE_PATH}:ro",
            # 产物目录可写：图表 / 导出文件落这里,供主进程回收
            "-v", f"{_docker_host_path(out_dir)}:{_CONTAINER_OUTPUT_DIR}",
            # 工作目录设为产物目录,AI 代码的相对写入也会被回收
            "-w", _CONTAINER_OUTPUT_DIR,
        ]
        # 数据目录只读挂载为 /data,落实「源数据只读」红线;目录不存在则不挂载
        # 每次执行时读取 SANDBOX_DATA_DIR,支持 analyze.py 动态指定分析目录
        data_dir = get_data_dir()
        if data_dir.is_dir():
            cmd += ["-v", f"{_docker_host_path(data_dir)}:{_CONTAINER_DATA_DIR}:ro"]

        cmd += [SANDBOX_IMAGE, "python", _CONTAINER_CODE_PATH]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SANDBOX_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # 超时必须强制清理容器,否则 --rm 只有在进程正常退出时才生效
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=10,
            )
            files, charts = _collect_outputs(out_dir, baseline)
            return make_result(
                stderr=f"TimeoutError: 执行超过 {SANDBOX_TIMEOUT}s,容器已强制终止。",
                charts=charts,
                files=files,
                duration_ms=elapsed(),
            )
        except FileNotFoundError:
            return make_result(
                stderr="DockerError: docker 可执行文件不可用(Docker Desktop 未启动？)",
                duration_ms=elapsed(),
            )
        except Exception as exc:  # 任何启动失败都要结构化返回,禁止静默失败
            return make_result(
                stderr=f"SandboxError: {type(exc).__name__}: {exc}",
                duration_ms=elapsed(),
            )

        files, charts = _collect_outputs(out_dir, baseline)

        # 退出码 125：Docker 守护进程层面的错误(未启动、无权限等)
        if proc.returncode == 125:
            return make_result(
                stderr=f"DockerError: Docker 守护进程不可用或参数错误。{proc.stderr.strip()}",
                files=files,
                charts=charts,
                duration_ms=elapsed(),
            )
        # 退出码 126/127：镜像或命令不存在,多半是镜像未预构建
        if proc.returncode in (126, 127):
            return make_result(
                stderr=(
                    f"ImageError: 无法启动镜像 {SANDBOX_IMAGE}。请先执行 "
                    f"`docker build -t {SANDBOX_IMAGE} sandbox/`。{proc.stderr.strip()}"
                ),
                files=files,
                charts=charts,
                duration_ms=elapsed(),
            )

        if proc.returncode != 0:
            # 容器内代码抛异常：stderr 原样带回,供上层做错误根因分析
            return make_result(
                stdout=proc.stdout,
                stderr=proc.stderr,
                files=files,
                charts=charts,
                duration_ms=elapsed(),
            )

        return make_result(
            stdout=proc.stdout,
            stderr=proc.stderr,
            files=files,
            charts=charts,
            duration_ms=elapsed(),
            exit_status="success",
        )


class E2BSandbox(Sandbox):
    """E2B 云端沙箱后端：接口与 DockerSandbox 完全一致。

    与 Docker 的差异：E2B 是独立远端环境,没有本地挂载,
    因此数据集与代码通过文件 API 上传;产物回传为图表文件。
    """

    def execute(self, code: str) -> dict:
        """在 E2B 云端沙箱中执行 AI 生成的 Python 代码。

        参数：
            code: AI 生成的完整 Python 源码字符串(含 import,不依赖外部变量)。

        与 Docker 的差异：
            E2B 是独立远端环境,没有本地挂载,数据集通过文件 API 上传到 /data,
            代码通过 run_code 执行,图表结果以 base64 形式下载后落盘。
        """
        start = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - start) * 1000)

        try:
            # 延迟导入：未安装 E2B 时不影响 docker 后端正常使用
            from e2b_code_interpreter import Sandbox as E2BClient  # type: ignore[import-not-found]
        except ImportError:
            return make_result(
                stderr=(
                    "E2BError: 未安装 e2b-code-interpreter。"
                    "请执行 `pip install e2b-code-interpreter` 并设置 E2B_API_KEY。"
                ),
                duration_ms=elapsed(),
            )

        if not os.getenv("E2B_API_KEY"):
            return make_result(
                stderr="E2BError: 缺少环境变量 E2B_API_KEY。",
                duration_ms=elapsed(),
            )

        try:
            with E2BClient.create(timeout=SANDBOX_TIMEOUT) as client:
                # 把本地数据集上传到沙箱 /data,保持与 Docker 后端相同的路径约定
                data_dir = get_data_dir()
                if data_dir.is_dir():
                    for item in data_dir.glob("*.csv"):
                        client.files.write(
                            f"{_CONTAINER_DATA_DIR}/{item.name}",
                            item.read_bytes(),
                        )
                execution = client.run_code(
                    code,
                    timeout=SANDBOX_TIMEOUT,
                    envs={"DATA_DIR": _CONTAINER_DATA_DIR},
                )
        except Exception as exc:  # 网络 / 鉴权 / 超时统一结构化返回
            return make_result(
                stderr=f"E2BError: {type(exc).__name__}: {exc}",
                duration_ms=elapsed(),
            )

        stdout = "\n".join(execution.logs.stdout)
        stderr = "\n".join(execution.logs.stderr)

        # 图表以文件路径回传,不把 base64 塞进上下文;
        # 按 make_result 的路径约定只返回文件名(相对 outputs/),不带 outputs/ 前缀
        chart_paths: list[str] = []
        for index, item in enumerate(execution.results or []):
            png = getattr(item, "png", None)
            if not png:
                continue
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            chart_file = OUTPUT_DIR / f"e2b_chart_{int(start)}_{index}.png"
            chart_file.write_bytes(base64.b64decode(png))
            chart_paths.append(chart_file.name)

        error = execution.error
        if error:
            stderr = f"{error.name}: {error.value}".strip() or stderr

        return make_result(
            stdout=stdout,
            stderr=stderr,
            charts=chart_paths,
            duration_ms=elapsed(),
            exit_status="error" if error else "success",
        )


# 后端注册表：新增后端只需在此登记,调用方无需改动
_SANDBOXES: dict[str, type[Sandbox]] = {"docker": DockerSandbox, "e2b": E2BSandbox}


def get_sandbox() -> Sandbox:
    """按 SANDBOX_BACKEND 环境变量返回对应沙箱实例。

    返回：
        Sandbox 实例(DockerSandbox / E2BSandbox),保证一定是可调用的对象;
        若 SANDBOX_BACKEND 配置为未知值,返回一个始终返回 ConfigError 的
        内联错误后端实例,确保调用方零崩溃。
    """
    backend_cls = _SANDBOXES.get(SANDBOX_BACKEND)
    if backend_cls is None:
        # 配置错误也走「实例化一个总能返回结构化 error 的后端」的思路,
        # 但这里属于编程期配置错误,直接抛出更符合「配置即代码」原则;
        # 为保持调用方零崩溃,返回一个内联的错误后端实例
        class _InvalidBackend(Sandbox):
            def execute(self, code: str) -> dict:
                return make_result(
                    stderr=(
                        f"ConfigError: 未知沙箱后端 SANDBOX_BACKEND={SANDBOX_BACKEND},"
                        f"可选值: {sorted(_SANDBOXES)}"
                    )
                )

        return _InvalidBackend()
    return backend_cls()
