"""数据分析 Agent 的工具白名单定义。

规则约束：
- 所有数据访问必须「先发现,后读取」。
- 全量数据禁止进入 LLM 上下文,只返回 schema / 摘要 / 抽样。
- execute_code 是 AI 生成代码的唯一执行入口：主进程只负责拉起沙箱,
  绝不 exec / eval AI 生成的代码;沙箱实现统一收敛在 sandbox.py。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from langchain_core.tools import tool

# 沙箱后端统一从 sandbox 模块获取,工具层只保留稳定的函数签名
from sandbox import get_sandbox

# 数据目录默认值：analyze.py 等入口可通过 SANDBOX_DATA_DIR 在运行时覆盖
DEFAULT_DATA_DIR = "data/processed"


def get_data_dir() -> Path:
    """返回当前数据目录(每次调用时读取环境变量 SANDBOX_DATA_DIR)。

    为什么不在模块导入时固化为常量：
        analyze.py 会在运行期把 SANDBOX_DATA_DIR 指向临时目录;
        若在导入时读取,模块常量会捕获旧值,导致后续分析仍然读取 data/processed。
    """
    return Path(os.getenv("SANDBOX_DATA_DIR") or DEFAULT_DATA_DIR)


# describe_dataset 的抽样行数：用于类型推断与空值率估算,绝不全量进内存
PROFILE_ROWS = 1000
# 无法等距抽样时的随机种子:固定后同一文件多次画像结果一致,便于复现
PROFILE_RANDOM_STATE = 42
# 返回给调用方的样本行数
SAMPLE_ROWS = 5
# 行数统计的分块大小：单列分块流式计数,内存占用与文件大小无关
COUNT_CHUNK_SIZE = 100_000

# 编码探测候选顺序：
# - utf-8-sig 优先于 utf-8：带 BOM 的 UTF-8 若用 utf-8 读,首列名会残留 \ufeff;
# - gb18030 覆盖 GBK/GB2312,是中文 CSV 最常见的非 UTF-8 编码;
# - latin-1 几乎能解码任意字节,作为最后兜底(避免探测阶段直接失败)。
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1")


def _detect_encoding(path: Path) -> str:
    """探测 CSV 文件编码：返回第一个能成功解析表头的候选编码。

    参数：
        path: CSV 文件路径(调用方已确认存在且后缀为 .csv)。

    返回：
        可用的编码名。全部候选都失败时返回 "utf-8",
        让后续 read_csv 抛出可读的 UnicodeDecodeError,而不是在这里吞掉错误。

    为什么用 nrows=0：
        只解析表头、不加载数据行,探测成本与文件大小无关;
        中文 CSV 常见 GBK/GB18030,若直接按 UTF-8 读会抛 UnicodeDecodeError。
    """
    for encoding in ENCODING_CANDIDATES:
        try:
            pd.read_csv(path, nrows=0, encoding=encoding)
            return encoding
        except Exception:
            # 该编码不适用(UnicodeDecodeError / 空文件 EmptyDataError 等)就换下一个;
            # 空文件的兜底由调用方按「数据集为空」处理
            continue
    return "utf-8"


@tool
def list_datasets() -> list[str]:
    """扫描当前数据目录(默认 data/processed/),返回所有 .csv 文件的文件名列表。

    为什么只返回文件名而不是全量内容：
    遵守「数据不进上下文」规则,发现阶段只需要知道有哪些数据源,
    具体的 schema / 抽样由 describe_dataset 按需提供。
    """
    data_dir = get_data_dir()
    # 目录尚未创建时(如项目初始化阶段)视为「没有数据源」,返回空列表而非报错,
    # 保证 Agent 的发现节点可以安全调用
    if not data_dir.is_dir():
        return []

    # glob 只匹配 .csv 后缀,不递归子目录：processed 目录约定为「扁平的成品数据层」
    return sorted(p.name for p in data_dir.glob("*.csv"))


# 文件哈希的分块大小(1MB):保证内存占用与文件大小无关
HASH_CHUNK_SIZE = 1_048_576

# 进程内哈希缓存:(绝对路径, mtime_ns, size) -> sha256
# 为什么带 mtime_ns 与 size:同一轮分析里 describe_dataset 可能被重复调用(重试 / 汇总),
# 文件未变时不必重复哈希;键里含元数据,文件被替换后缓存自然失效
_SHA256_CACHE: dict[tuple[str, int, int], str] = {}


def _file_sha256(path: Path) -> str:
    """按 1MB 分块计算文件 sha256,不把整文件读进内存。

    参数:
        path: 已确认存在的文件路径。

    返回:
        64 位十六进制 sha256 字符串。

    为什么分块:大 CSV 可能是 GB 级,整读会触发内存告警(规则 3.6 / 9.2)。
    """
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    cached = _SHA256_CACHE.get(key)
    if cached:
        return cached

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    value = digest.hexdigest()
    _SHA256_CACHE[key] = value
    return value


def _file_meta(path: Path) -> dict[str, Any]:
    """返回数据版本元信息:文件名 / 大小 / 更新时间 / sha256。

    为什么把元信息单独抽成函数:describe_dataset 有多个提前返回分支
    (空文件 / 仅表头),每个分支都要带上同一组版本字段,集中计算避免漏字段。
    """
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        # ISO 8601 便于人读,也便于按字符串排序比较版本
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "sha256": _file_sha256(path),
    }


def fingerprint_text(schema_info: dict) -> str:
    """把 schema 元信息压成可读短指纹:数据集名@sha256 前 12 位。

    参数:
        schema_info: describe_dataset 的返回值(需含 name / sha256)。

    返回:
        形如 "测试数据.csv@a1b2c3d4e5f6" 的短串;元信息缺失时返回空串。

    为什么只取前 12 位:完整哈希 64 位太长,12 位足以区分数据版本;
    完整值仍保留在 schema_info["sha256"] 中可追溯。
    """
    name = str(schema_info.get("name") or "")
    sha = str(schema_info.get("sha256") or "")
    if not name and not sha:
        return ""
    return f"{name}@{sha[:12]}" if sha else name


# 样本行数阈值:低于该值时跳过 IQR 异常值检查,避免小样本下分位数失真;
# quality 摘要里会如实标注「样本不足,未做异常值检查」
IQR_MIN_SAMPLE_ROWS = 10


def _equidistant_rows(total_rows: int, sample_size: int) -> list[int] | None:
    """计算等距抽样要保留的行号(0-based 数据行),无法等距时返回 None。

    Args:
        total_rows: 文件总数据行数(不含表头)。
        sample_size: 期望抽样的行数。

    返回:
        长度等于 sample_size 的升序行号列表;总行数不超过 sample_size
        (无需抽样)或步长太小导致行号重复时返回 None,由调用方退化为随机抽样。

    为什么取每段中点:
        直接取 0, step, 2*step ... 会永远抽不到文件末尾;
        取每段中点能让样本均匀覆盖全文件,避免只看到开头造成的顺序偏差。
    """
    if total_rows <= sample_size:
        return None
    step = total_rows / sample_size
    # 中点抽样:int((i + 0.5) * step);step > 1 时各中点必不重复
    indices = [int((i + 0.5) * step) for i in range(sample_size)]
    return indices if len(set(indices)) == sample_size else None


def _random_rows(total_rows: int, sample_size: int) -> list[int]:
    """在等距抽样不可用时,用固定随机种子抽取 sample_size 个不重复行号。

    Args:
        total_rows: 文件总数据行数(不含表头)。
        sample_size: 需要抽取的行数。

    返回:
        升序行号列表;固定 PROFILE_RANDOM_STATE,保证同文件多次画像结果一致。
    """
    rng = random.Random(PROFILE_RANDOM_STATE)
    return sorted(rng.sample(range(total_rows), sample_size))


def _build_quality_summary(
    row_count: int,
    duplicate_rows: int,
    columns: list[dict],
    iqr_enabled: bool,
    sample_size: int,
    sampling_desc: str,
) -> str:
    """组装一句人话数据质量摘要,供 prompt 与报告直接引用。

    参数:
        row_count: 全量行数(单列分块计数得到)。
        duplicate_rows: 抽样内的重复行数。
        columns: describe_dataset 产出的列结构列表(含 n_unique / outlier_count 等)。
        iqr_enabled: 是否对数值列做了 IQR 异常值统计。
        sample_size: 本次画像实际使用的行数。
        sampling_desc: 抽样方式(等距抽样 / 随机抽样 / 全量读取)。

    返回:
        形如 "800 行(全量读取,统计量按全量口径);无重复;收入缺失 0;
        收入异常值 36 个(IQR);converted 异常值(IQR) 不适用(二值列)" 的中文摘要。
        非全量时首段会写明「基于抽样 N 行」与抽样方式,提醒下游这不是全量统计。

    为什么只做摘要不做判断:
        质量检查的结论(是否剔除异常值 / 如何处理缺失)必须由用户决定,
        本函数只客观呈现统计量,不替用户做清洗决策(红线 2)。
    """
    # 明确标注统计口径:下游提示词不得把抽样统计当成全量统计
    if sample_size < row_count:
        head = f"{row_count} 行(基于抽样 {sample_size} 行,{sampling_desc};统计量非全量口径)"
    else:
        head = f"{row_count} 行(全量读取,统计量按全量口径)"
    parts: list[str] = [head]
    parts.append("无重复" if duplicate_rows == 0 else f"重复行 {duplicate_rows}")

    # 缺失:按抽样空值率外推全量缺失数,给绝对数比百分比更直观
    missing_parts: list[str] = []
    for col in columns:
        ratio = float(col.get("null_ratio") or 0)
        est = int(round(ratio * row_count)) if row_count else 0
        missing_parts.append(f"{col['name']}缺失 {est}")
    parts.append(";".join(missing_parts))

    # 二值列(0/1 标记、bool、二分类标签)不做 IQR:直接写「不适用」,不输出数字
    binary_parts = [
        f"{col['name']}异常值(IQR) 不适用(二值列)"
        for col in columns
        if col.get("is_binary")
    ]
    if binary_parts:
        parts.append(";".join(binary_parts))

    # 连续数值列:只在做了 IQR 统计时呈现数字;未做则显式说明原因
    if not iqr_enabled:
        parts.append("样本不足,未做异常值检查")
    else:
        outlier_parts: list[str] = []
        for col in columns:
            oc = col.get("outlier_count")
            # 二值列的 outlier_count 固定为 None,已在上面单独说明
            if oc is not None and not col.get("is_binary"):
                outlier_parts.append(f"{col['name']}异常值 {oc} 个(IQR)")
        if outlier_parts:
            parts.append(";".join(outlier_parts))

    return ";".join(parts)


# 表头探测只扫描前 30 行:足以判断「列名是不是数据」,成本与文件大小无关
HEADER_PROBE_ROWS = 30
# 列名中「像数据」(可解析为日期 / 纯数字)的比例达到该阈值即怀疑无表头
HEADER_DATA_LIKE_RATIO = 0.5
# 无表头时的固定提示:同时写进 schema 与代码生成 prompt,避免两边口径漂移
NO_HEADER_NOTE = (
    "文件无表头:列名由系统按位置生成,代码必须 "
    "pd.read_csv(path, header=None, names=[...])"
)


def _is_number_like(value: str) -> bool:
    """判断字符串是否为纯数字 / 带小数点的数字。

    Args:
        value: 待判断的列名或单元格文本。

    Returns:
        True 表示可被 float() 解析(如 "361.57" / "-520" / "1e5");
        空串与非数字文本返回 False。
    """
    text = str(value).strip()
    if not text:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def _is_date_like(value: str) -> bool:
    """判断字符串能否被 pandas 解析为日期。

    Args:
        value: 待判断的列名文本。

    Returns:
        True 表示 pd.to_datetime 能无错误解析(如 "2026-01-01");
        纯数字("2026")交给 _is_number_like,这里不算日期,避免误伤数字列名。

    为什么先排除纯数字:pd.to_datetime("2026") 在部分版本会被当成纳秒时间戳,
    这会把真实表头里的年份列名误判成日期。
    """
    text = str(value).strip()
    if not text or _is_number_like(text):
        return False
    try:
        pd.to_datetime(text, errors="raise")
        return True
    except (ValueError, TypeError, OverflowError):
        return False


def _row_looks_like_header(values: list) -> bool:
    """判断一行取值更像表头还是数据行。

    Args:
        values: 该行的原始取值列表(按列顺序)。

    Returns:
        True 表示超过一半是非数字/非日期文本(像列名);空串不参与该比例,
        重复值或数据行(日期 + 金额 + 标签混合)返回 False。

    为什么允许空串:首列没名字时 pandas 会生成 "Unnamed: 0",
    这种占位表头仍可能是真表头,判据交给调用方与占位名条件组合。
    """
    items = [str(value).strip() for value in values]
    if not items:
        return False
    if len(set(items)) != len(items):
        return False
    textual = [
        item
        for item in items
        if item and not _is_number_like(item) and not _is_date_like(item)
    ]
    return len(textual) > len(items) / 2


def _detect_header(path: Path, encoding: str) -> tuple[bool, list[str], list[str]]:
    """确定性判断 CSV 是否有表头,并给出读取列名与质量警告。

    Args:
        path: CSV 文件路径(只读前 HEADER_PROBE_ROWS 行)。
        encoding: 已探测出的文件编码,保证 A / B 两种读法编码一致。

    Returns:
        (has_header, column_names, notes):
        - has_header: True=首行是表头;False=文件无表头(首行就是数据);
        - column_names: 有表头时为原始列名(含 pandas 去重后的 a / a.1),
          无表头时为按位置生成的 col_1..col_n;
        - notes: 需要追加到 quality 的确定性警告(如列名重复)。

    判定规则(任一成立即认定无表头):
        a) A 的列名中 >=50% 可被 pd.to_datetime 解析或是纯数字;
           另外只要有一个列名本身是日期(如 "2026-01-01")也直接判定无表头,
           因为正常表头几乎不会用 ISO 日期命名;
        b) A 的列名与「首行数据」逐列相等(表头被数据行顶掉);
           注意 B(header=None)的第 0 行按构造恒等于 A 的列名,
           所以比较 B 的第 1 行才是真正的首行数据;
        c) A 的列名含空串 / Unnamed 占位名,且 B 首行看起来像表头。
    为什么不用 LLM:表头判定必须同文件同结果,不能依赖模型输出。
    """
    probe_a = pd.read_csv(path, encoding=encoding, nrows=HEADER_PROBE_ROWS)
    probe_b = pd.read_csv(
        path, encoding=encoding, header=None, nrows=HEADER_PROBE_ROWS
    )
    names = [str(column) for column in probe_a.columns]
    notes: list[str] = []

    # pandas 遇到重复列名会自动改写成 a / a.1;与原始首行对比即可发现
    raw_first_row = [str(value) for value in probe_b.iloc[0].tolist()] if len(probe_b) else []
    if len(set(raw_first_row)) != len(raw_first_row):
        notes.append("列名重复,pandas 已改写为 a / a.1 形式,建议按位置读取")

    date_like_count = sum(1 for name in names if _is_date_like(name))
    data_like_ratio = (
        sum(1 for name in names if _is_number_like(name) or _is_date_like(name))
        / len(names)
        if names
        else 0.0
    )
    # B 第 1 行才是「表头之后的第一条数据」;B 第 0 行就是原始首行
    first_data = [str(value) for value in probe_b.iloc[1].tolist()] if len(probe_b) > 1 else []
    header_equals_first_data = (
        bool(names)
        and len(first_data) == len(names)
        and all(column == value for column, value in zip(names, first_data))
    )
    placeholder_trigger = any(
        name == "" or name.lower().startswith("unnamed") for name in names
    ) and _row_looks_like_header(raw_first_row)

    headerless = (
        date_like_count >= 1
        or data_like_ratio >= HEADER_DATA_LIKE_RATIO
        or header_equals_first_data
        or placeholder_trigger
    )
    if not headerless:
        return True, names, notes
    # 无表头时列名必须由系统按位置生成,保证后续读取与 schema 使用同一套名字
    return False, [f"col_{index}" for index in range(1, len(names) + 1)], notes


@tool
def describe_dataset(name: str) -> dict:
    """返回指定数据集的 schema、抽样样本与行数,结果可直接 JSON 序列化。

    参数：
        name: 数据集文件名(如 "orders.csv"),必须是当前数据目录
              (默认 data/processed/,可用 SANDBOX_DATA_DIR 覆盖)下
              已登记的 .csv 文件,不支持路径穿越(如 "../etc/passwd")。

    返回：
        {columns: [{name, dtype, null_ratio, n_unique, is_constant, is_binary,
                    outlier_count?, negative_count?, min?, max?}],
         sample: [抽样5行], row_count, encoding, name, size_bytes, mtime_iso, sha256,
         duplicate_rows, quality, has_header, header_note}
        全量数据不会进内存：表头探测 30 行 + 均匀抽样 PROFILE_ROWS 行估算 dtype/空值率 +
        单列分块计数行数。
        encoding 是探测到的文件编码,供代码生成时显式传给 pd.read_csv。
        name / size_bytes / mtime_iso / sha256 是数据版本指纹,供报告标注「结论基于哪个版本」。
        duplicate_rows / quality 是本次新增的确定性质量检查结果,只基于抽样,不读全量。
        has_header: True=首行是表头;False=文件无表头(首行就是数据),
                    此时 columns 按位置命名为 col_1..col_n,所有读取统一 header=None。
        header_note: 无表头时给出「代码必须 header=None + names=...」的提示,有表头时为空串。
        is_binary=True 的列(0/1 标记、bool、二分类标签)不做 IQR,outlier_count 固定 None。

    读取策略(对应「禁止全量数据进入上下文」红线)：
    1. 先探测编码(UTF-8 / GB18030 / GBK 等),后续所有读取统一带上该编码;
    2. 用「带表头」与「无表头」两种读法各扫 HEADER_PROBE_ROWS 行做确定性表头判定
       (见 _detect_header);判定为无表头时,后续行数与抽样全部用 header=None 读取,
       保证 schema、样本、行数来自同一读法;
    3. nrows=0 只拿表头(有表头时),不加载任何数据行;
    4. 单列分块计数得到 row_count,内存占用与文件大小无关;
    5. 按行号等距抽样 PROFILE_ROWS 行(无法等距时用固定 random_state 随机抽样),
       避免 nrows 固定读开头造成的顺序偏差;
    6. sample 经 to_json 转换：NaN -> None、numpy 类型 -> 原生类型,保证可 JSON 序列化。

    异常：
        FileNotFoundError: 数据目录不存在、数据集未登记、或路径穿越。
        ValueError: 文件后缀不是 .csv。
    """
    # ---------- 先发现、后读取：只允许访问已登记目录内的 csv ----------
    data_dir = get_data_dir()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"数据目录不存在: {data_dir}")

    # resolve 后校验父目录,防止 "../etc/passwd" 之类的路径穿越
    path = (data_dir / name).resolve()
    if path.parent != data_dir.resolve() or not path.is_file():
        raise FileNotFoundError(f"未登记的数据集: {name},请先调用 list_datasets()")
    if path.suffix.lower() != ".csv":
        raise ValueError(f"暂仅支持 csv 数据集,收到: {path.suffix}")

    # ---------- 数据版本元信息：大小 / 更新时间 / sha256(分块哈希) ----------
    # 提前计算,保证空文件 / 仅表头等提前返回分支也能带回版本指纹
    meta = _file_meta(path)

    # ---------- 编码探测：中文 CSV 常见 GBK/GB18030,必须先探测再读 ----------
    encoding = _detect_encoding(path)

    # ---------- 表头判定：必须确定性,同文件同结果(不调用 LLM) ----------
    # 旧实现直接用 nrows=0 把首行当表头;若上游文件无表头,首行数据会被当成列名,
    # 后续代码生成按这些「列名」写死,必然 KeyError。这里先探测再决定读取方式。
    try:
        has_header, column_names, header_notes = _detect_header(path, encoding)
    except pd.errors.EmptyDataError:
        # 空文件(连表头都没有)不算异常：返回空结构,由上层按「数据集为空」处理
        return {
            **meta,
            "columns": [],
            "sample": [],
            "row_count": 0,
            "encoding": encoding,
            "duplicate_rows": 0,
            "quality": "0 行,数据集为空",
            "has_header": True,
            "header_note": "",
        }
    if not column_names:
        # 空文件没有表头,直接返回空结构而不是让后续逻辑猜
        return {
            **meta,
            "columns": [],
            "sample": [],
            "row_count": 0,
            "encoding": encoding,
            "duplicate_rows": 0,
            "quality": "0 行,数据集为空",
            "has_header": True,
            "header_note": "",
        }

    # 无表头时后续所有读取统一 header=None + names;有表头时保持旧行为(不传 header/names)
    read_kwargs: dict[str, Any] = {"encoding": encoding}
    if not has_header:
        read_kwargs["header"] = None
        read_kwargs["names"] = column_names

    # 质量摘要里追加确定性警告(列名重复 / 无表头),让报告读者知道读取口径
    if not has_header:
        header_notes.append("文件无表头,列名由系统按位置生成(col_1..col_n)")

    # ---------- 行数：单列分块流式计数 ----------
    # 为什么不用 len(df)：会全量载入内存;这里只投影首列并分块累加,内存恒定。
    # 行数必须先算出来,才能对 profile 做等距抽样,避免只读文件前 N 行造成顺序偏差。
    # 无表头时首列按位置取(usecols=[0]),避免依赖会随 names 变化列名的字符串匹配。
    count_usecols = [0] if not has_header else [column_names[0]]
    row_count = 0
    for chunk in pd.read_csv(
        path,
        usecols=count_usecols,
        chunksize=COUNT_CHUNK_SIZE,
        **read_kwargs,
    ):
        row_count += len(chunk)

    # ---------- 抽样：在全文范围内均匀取 PROFILE_ROWS 行 ----------
    # 旧实现 nrows=PROFILE_ROWS 固定读文件开头 1000 行,遇到按时间排序的数据
    # 会产生严重顺序偏差(如 ab_data.csv 前 1000 行转化率 23.5%,全量仅 12.2%)。
    if row_count <= PROFILE_ROWS:
        profile = pd.read_csv(path, **read_kwargs)
        sampling_desc = "全量读取"
    else:
        indices = _equidistant_rows(row_count, PROFILE_ROWS)
        if indices is None:
            indices = _random_rows(row_count, PROFILE_ROWS)
            sampling_desc = f"随机抽样(random_state={PROFILE_RANDOM_STATE})"
        else:
            sampling_desc = "等距抽样(按行号均匀分布)"
        # skiprows 只跳过未选中的行;nrows 保证最多加载 PROFILE_ROWS 行。
        # 有表头时物理行号要 +1:第 0 行是表头,一旦被 skiprows 跳过,
        # pandas 会把紧随其后的首行数据顶成列名(这正是「首行数据当列名」的成因)。
        # 无表头时首行就是数据,行号与物理行号一致,不需要偏移。
        skip_offset = 0 if not has_header else 1
        profile = pd.read_csv(
            path,
            skiprows=[index + skip_offset for index in indices],
            nrows=PROFILE_ROWS,
            **read_kwargs,
        )

    # 只有表头 / 0 数据行：返回空样本而非异常;不做空值率除法(0/0 会产生 NaN)
    if profile.empty:
        columns = [
            {
                "name": str(col),
                "dtype": str(profile[col].dtype),
                "null_ratio": 0.0,
                "n_unique": 0,
                "is_constant": True,
                "is_binary": True,
                "outlier_count": None,
            }
            for col in profile.columns
        ]
        empty_quality = "0 行,数据集为空"
        if header_notes:
            empty_quality += ";" + ";".join(header_notes)
        return {
            **meta,
            "columns": columns,
            "sample": [],
            "row_count": 0,
            "encoding": encoding,
            "duplicate_rows": 0,
            "quality": empty_quality,
            "has_header": has_header,
            "header_note": "" if has_header else NO_HEADER_NOTE,
        }

    # 样本行数是否足以做 IQR:小样本分位数不稳定,直接跳过并在 quality 里说明
    iqr_enabled = len(profile) >= IQR_MIN_SAMPLE_ROWS

    columns: list[dict[str, Any]] = []
    for col in profile.columns:
        series = profile[col]
        # null_ratio 基于抽样行估算(不读全量),保留 4 位小数避免浮点噪声
        col_info: dict[str, Any] = {
            "name": str(col),
            "dtype": str(series.dtype),
            "null_ratio": round(float(series.isna().mean()), 4),
            # 唯一值数:基于抽样,nunique 默认排除 NaN,避免常数列被误判
            "n_unique": int(series.nunique(dropna=True)),
        }
        # 常数列:n_unique <= 1(含「全部相同」与「全部缺失」两种情况)
        col_info["is_constant"] = col_info["n_unique"] <= 1
        # 二值列:n_unique <= 2,覆盖 0/1 标记、bool、二分类标签;
        # 这类列只能看取值分布,做 IQR 会把「少数类」误判成异常值
        col_info["is_binary"] = col_info["n_unique"] <= 2

        # 数值列额外统计:异常值 / 负值 / 极值(全部基于抽样样本,不读全量)
        if pd.api.types.is_numeric_dtype(series):
            non_null = series.dropna()
            col_info["negative_count"] = int((non_null < 0).sum())
            if not non_null.empty:
                # 显式转 float:避免 numpy.float64 导致 JSON 序列化失败
                col_info["min"] = float(non_null.min())
                col_info["max"] = float(non_null.max())
            else:
                col_info["min"] = None
                col_info["max"] = None
            if col_info["is_binary"]:
                # 二值列不做 IQR:None 表示「不适用」,由 is_binary 区分于样本不足
                col_info["outlier_count"] = None
            # IQR 异常值:Q1 - 1.5*IQR 以下或 Q3 + 1.5*IQR 以上
            # 只在样本足够时计算,避免 2~3 行样本的分位数毫无意义
            elif iqr_enabled and len(non_null) >= 4:
                q1 = float(non_null.quantile(0.25))
                q3 = float(non_null.quantile(0.75))
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                col_info["outlier_count"] = int(
                    ((non_null < lower) | (non_null > upper)).sum()
                )
            else:
                # None 表示「未计算」,与 0(计算过且无异常值)区分开
                col_info["outlier_count"] = None
        columns.append(col_info)

    # ---------- 整表:重复行数(抽样内) ----------
    duplicate_rows = int(profile.duplicated().sum())

    # ---------- quality:一句人话摘要 ----------
    quality = _build_quality_summary(
        row_count, duplicate_rows, columns, iqr_enabled, len(profile), sampling_desc
    )
    if header_notes:
        quality += ";" + ";".join(header_notes)

    # to_json(orient="records") 天然处理 NaN/时间/numpy 类型,保证结果可 JSON 序列化
    sample = json.loads(
        profile.head(SAMPLE_ROWS).to_json(orient="records", date_format="iso", force_ascii=False)
    )

    return {
        **meta,
        "columns": columns,
        "sample": sample,
        "row_count": row_count,
        "encoding": encoding,
        "duplicate_rows": duplicate_rows,
        "quality": quality,
        "has_header": has_header,
        "header_note": "" if has_header else NO_HEADER_NOTE,
    }


def list_tables() -> list[dict]:
    """枚举数据库中的表 / 视图(数据库场景使用,暂未启用)。"""
    raise NotImplementedError


def describe_table(name: str) -> dict:
    """返回指定表的 schema 与抽样样本(数据库场景使用,暂未启用)。"""
    raise NotImplementedError


@tool
def execute_code(code: str) -> dict:
    """AI 生成代码的唯一执行入口：委托给 sandbox 模块的后端执行。

    参数：
        code: 由 LLM 生成的完整 Python 源码字符串,必须是可独立运行的脚本
              (含 import 语句,不依赖外部变量,数据从 /data/<文件名> 读取)。

    返回：
        {stdout, stderr, return_value, charts, files, duration_ms, exit_status}
        主进程不执行任何 AI 代码,只负责拉起沙箱进程并回收结构化结果。
    """
    return get_sandbox().execute(code)
