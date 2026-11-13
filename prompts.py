"""Prompt 模板集中管理:所有送给 LLM 的提示词统一放在这里。

为什么集中管理:
- prompt 是 Agent 行为的一部分,散落在节点里会导致行为难以审计与回归;
- 改 prompt 时只动这一个文件,配合评估集可快速回归验证。

红线映射:
- 送给 LLM 的只有 schema / 抽样样本 / 行数,绝不包含全量数据;
- 代码规则里显式禁止 exec/eval、联网、写源数据。
"""

# ---------- planner ----------

PLANNER_SYSTEM_PROMPT = """你是一个数据分析 Agent 的规划器。根据用户问题判断意图并给出分析计划。

意图取值:
- discover:用户想知道有哪些数据集 / 数据文件可用;
- analyze:用户想对某个具体数据集做统计、对比、筛选、趋势等分析;
- clarify:问题与数据分析无关,或信息不足无法判断。

约束:
1. dataset_name:问题中明确出现数据集文件名时填入(只填文件名本身);
   对上一轮分析的追问(如「那对照组呢」「再画个图」)沿用上文数据集;
2. plan 用简短的中文步骤描述,最多 4 步;
3. 拿不准就选 clarify,不要硬猜。"""


def build_planner_user_prompt(
    question: str,
    known_datasets: list[str],
    previous_question: str = "",
    previous_answer: str = "",
    current_dataset: str = "",
    domain_block: str = "",
) -> str:
    """组装 planner 的用户消息:当前问题 + 跨轮上下文 + 已登记数据集清单 (+ 领域上下文)。

    参数:
        question: 本轮用户的自然语言问题文本。
        known_datasets: 当前已登记的数据集文件名列表(由 discover 分支填充)。
        previous_question: 上一轮的用户问题(多轮对话时用于指代消解,单轮时为空串)。
        previous_answer: 上一轮的最终答案(截断到 200 字,避免上下文过长拖慢意图识别)。
        current_dataset: 当前已锁定的数据集文件名(跨轮继承,支持「那对照组呢」指代)。
        domain_block: 领域包插入的上下文块,空串时不出现(旧行为逐字节不变)。

    返回:
        格式化后的用户消息字符串。跨轮上下文仅在有历史时附加,单轮场景保持精简。
        已登记的数据集只有文件名,不包含任何数据内容(遵守「数据不进上下文」红线)。
    """
    # 数据集清单只列前 20 个,超出用「等 N 个」概括:意图识别不需要完整清单,
    # 数据集过多时反而会稀释问题语义、拖慢 LLM 响应
    MAX_DATASETS = 20
    if len(known_datasets) > MAX_DATASETS:
        shown = known_datasets[:MAX_DATASETS]
        datasets_text = "、".join(shown) + f" 等 {len(known_datasets)} 个"
    else:
        datasets_text = "、".join(known_datasets) if known_datasets else "(尚未登记任何数据集)"

    parts: list[str] = []

    # 跨轮上下文:仅在有历史时附加,帮助 LLM 理解「那对照组呢」这类指代
    if previous_question or previous_answer or current_dataset:
        context_lines = ["【对话上下文】"]
        if current_dataset:
            context_lines.append(f"当前已锁定数据集:{current_dataset}")
        if previous_question:
            context_lines.append(f"上一轮问题:{previous_question}")
        if previous_answer:
            # 截断到 200 字:意图识别只需大致了解上一轮在分析什么,
            # 过长的答案会拖慢首字、占用 token 预算
            truncated = previous_answer[:200]
            if len(previous_answer) > 200:
                truncated += "…"
            context_lines.append(f"上一轮答案:{truncated}")
        parts.append("\n".join(context_lines))

    parts.append(f"用户问题:{question}")
    parts.append(f"已登记的数据集:{datasets_text}")
    if domain_block:
        parts.append(domain_block)
    return "\n\n".join(parts)


# ---------- SQL 生成(数据库问答) ----------

# 与方言无关的硬约束:只读、单语句、不臆造 schema、聚合别名可读。
SQL_COMMON_RULES = """通用规则(必须严格遵守):
1. 只写单条只读 SELECT / WITH 查询,禁止 INSERT / UPDATE / DELETE / DROP / ALTER /
   CREATE / REPLACE / ATTACH / TRUNCATE 等任何写操作或 DDL;
2. 只能使用用户消息中给出的表和列,不要编造不存在的表名或列名,不要猜测列的含义;
3. 只写一条语句,不要写分号结尾,不要写注释,不要写多语句;
4. 不要自行添加 LIMIT(系统会自动限制返回行数);但用户问题明确要求「前 N 条」时可写 LIMIT N;
5. 聚合列请给出可读的别名(如 AS 销售额),便于后续生成图表与结论;
6. 文本值不要臆造:只使用问题中提到的值,或使用 LIKE 做模糊匹配;
7. 只查询用户业务库中的表与视图,不要查询系统库(information_schema / mysql /
   performance_schema / sys),不要写 USE。"""

SQLITE_RULES = """SQLite 方言规则:
1. 标识符使用双引号包裹(如 "支付金额"),不要使用反引号;
2. 日期按月聚合:月份列形如 "2024-03" 时用 substr(month, 1, 7),带日期的用
   strftime('%Y-%m', date_col);
3. 禁用 MySQL 专有函数(DATE_FORMAT / CONCAT / NOW 等),不支持 PRAGMA / ATTACH;
4. 大表(>20 万行或 >200MB)与重查询规则:
   a. 禁止无条件 GROUP BY 文本列;必须先按时间/状态 WHERE 过滤,
      或先 SELECT DISTINCT 取维度值再 JOIN 聚合;
   b. 单条 SELECT 中 COUNT(DISTINCT ...) 不超过 2 个,且只对低基数列使用;
   c. 确需全表统计时,用 LIMIT 采样(如 LIMIT 200000),并在输出里注明
      「基于 N 行抽样」;
   d. 不确定表规模时,先执行 SELECT COUNT(*) 或读 information_schema.TABLES,
      再决定是否过滤/采样;
   e. 任何可能超过 2 秒的写法,在 SQL 注释里写明原因。"""

MYSQL_RULES = """MySQL 方言规则:
1. 表和列标识符统一用反引号包裹(如 `支付金额`),避免中文列名与保留字冲突;
2. 日期按月聚合用 DATE_FORMAT(date_col, '%Y-%m'),按日聚合可用 DATE(date_col);
3. 字符串拼接用 CONCAT(a, b),不要用 SQLite 的 || 作为首选写法;
4. 禁用 SQLite 专有写法:strftime / PRAGMA / ATTACH;
5. 不要写 USE,不要查询 information_schema 等系统库,只查当前业务库的表与视图;
6. 大表(>20 万行或 >200MB)与重查询规则:
   a. 禁止无条件 GROUP BY 文本列;必须先按时间/状态 WHERE 过滤,
      或先 SELECT DISTINCT 取维度值再 JOIN 聚合;
   b. 单条 SELECT 中 COUNT(DISTINCT ...) 不超过 2 个,且只对低基数列使用;
   c. 确需全表统计时,用 LIMIT 采样(如 LIMIT 200000),并在输出里注明
      「基于 N 行抽样」;
   d. 不确定表规模时,先执行 SELECT COUNT(*) 或读 information_schema.TABLES,
      再决定是否过滤/采样;
   e. 任何可能超过 2 秒的写法,在 SQL 注释里写明原因。"""

_SQL_DIALECT_RULES = {"sqlite": SQLITE_RULES, "mysql": MYSQL_RULES}
_SQL_DIALECT_NAMES = {"sqlite": "SQLite", "mysql": "MySQL"}


def build_sql_system_prompt(dialect: str = "sqlite") -> str:
    """按方言拼装 SQL 生成的 system 提示词。

    参数:
        dialect: "sqlite" 或 "mysql"。

    返回:
        含通用只读约束与对应方言规则的 system 提示词。

    异常:
        ValueError: 不支持的方言。
    """
    key = str(dialect or "sqlite").lower()
    rules = _SQL_DIALECT_RULES.get(key)
    if rules is None:
        raise ValueError(f"不支持的 SQL 方言: {dialect}")
    return (
        f"你是一个 {_SQL_DIALECT_NAMES[key]} SQL 生成器。"
        "根据用户问题与表结构,写出可直接执行的单条只读查询。\n\n"
        f"{SQL_COMMON_RULES}\n\n{rules}"
    )


# 兼容旧调用方:默认 SQLite 方言
SQL_SYSTEM_PROMPT = build_sql_system_prompt("sqlite")


def _format_schema(tables_schema: list[dict]) -> tuple[str, bool]:
    """把表结构格式化成提示词里的行文本。

    参数:
        tables_schema: 表列表;表可带 description,列可带 description(人工说明)。

    返回:
        (schema 文本, 是否包含任何说明)。没有说明时文本与旧版逐字节一致。
    """
    schema_lines: list[str] = []
    has_semantics = False
    for table in tables_schema:
        table_type = table.get("type") or "BASE TABLE"
        column_parts: list[str] = []
        for column in table["columns"]:
            text = f"{column['name']} ({column['type']})"
            column_desc = str(column.get("description") or "").strip()
            if column_desc:
                text += f", {column_desc}"
                has_semantics = True
            column_parts.append(text)
        line = f"- {table_type} {table['name']}"
        table_desc = str(table.get("description") or "").strip()
        if table_desc:
            line += f" (说明:{table_desc})"
            has_semantics = True
        line += ":" + "、".join(column_parts)
        schema_lines.append(line)
    schema_text = "\n".join(schema_lines) if schema_lines else "(无可用表)"
    return schema_text, has_semantics


def build_sql_user_prompt(
    question: str,
    tables_schema: list[dict],
    dialect: str = "sqlite",
    glossary: list[dict] | None = None,
) -> str:
    """组装 SQL 生成的用户消息:用户问题 + 表结构(表名 / 列名 / 类型 / 说明)。

    参数:
        question: 用户的自然语言问题。
        tables_schema: list_tables 的返回值,形如
            [{"name": "orders", "type": "BASE TABLE",
              "columns": [{"name": "amount", "type": "REAL"}]}];
            若人工填写过说明,表和列可额外带 description。
        dialect: "sqlite" 或 "mysql",用于在消息中重申目标方言。
        glossary: 用户确认过的业务术语列表
            [{"term": ..., "definition": ..., "sql_hint": ...}];为空时不出现术语段。

    返回:
        格式化后的用户消息。只包含 schema(表名 / 列名 / 类型 / 说明)与术语口径,
        不含任何数据内容,遵守「全量数据不进 LLM 上下文」红线。
        既没有说明也没有术语时,输出与旧版逐字节一致。
    """
    schema_text, has_semantics = _format_schema(tables_schema)
    parts = [
        f"用户问题:{question}",
        f"目标数据库方言:{str(dialect or 'sqlite').lower()}",
        f"可用表结构:\n{schema_text}",
    ]
    if has_semantics:
        parts.append("未标注的表列只按名称与类型推断,不得臆造业务含义。")
    terms = [item for item in (glossary or []) if str(item.get("term") or "").strip()]
    if terms:
        term_lines = []
        for item in terms:
            line = f"- {str(item['term']).strip()}:{str(item.get('definition') or '').strip()}"
            sql_hint = str(item.get("sql_hint") or "").strip()
            if sql_hint:
                line += f"(SQL 口径:{sql_hint})"
            term_lines.append(line)
        parts.append("业务术语(用户确认过,优先按此口径):\n" + "\n".join(term_lines))
        parts.append("未列出的术语不得臆造;不确定时按字段名直译并说明假设。")
    parts.append("请生成一条只读 SQL 查询。")
    return "\n\n".join(parts)


# ---------- 业务术语检查(SQL 生成之前) ----------

GLOSSARY_CHECK_PROMPT = """你是一个数据库业务术语审校器。用户即将用自然语言查询数据库,
你要找出其中「无法从表说明、列说明或已知术语表确定口径」的业务词。

判定规则:
1. 能直接对应到某个表名或列名的词,不要列出;
2. 能由表说明 / 列说明 / 已知术语表明确得到的词,不要列出;
3. 只列真正影响查询口径的词(如「客单价」「活跃用户」这类计算口径不明的指标);
4. 最多列 3 个;没有就返回空列表,不要为了凑数而列;
5. 每个词给出一句话原因,并给出最多 3 个最可能的字段或口径(表名.列名),
   不确定 candidates 就留空。"""


def build_glossary_user_prompt(
    question: str,
    tables_schema: list[dict],
    glossary: list[dict] | None = None,
) -> str:
    """组装术语检查的用户消息:问题 + 表结构(含说明) + 已确认术语。

    参数:
        question: 用户的自然语言问题。
        tables_schema: 已合并人工说明的表结构。
        glossary: 已确认的术语口径列表,可为空。

    返回:
        格式化后的用户消息;只含 schema 与术语,不含数据内容。
    """
    schema_text, _has_semantics = _format_schema(tables_schema)
    parts = [f"用户问题:{question}", f"可用表结构与说明:\n{schema_text}"]
    terms = [item for item in (glossary or []) if str(item.get("term") or "").strip()]
    if terms:
        lines = [
            f"- {str(item['term']).strip()}:{str(item.get('definition') or '').strip()}"
            for item in terms
        ]
        parts.append("已知术语表(已确认口径):\n" + "\n".join(lines))
    else:
        parts.append("已知术语表(已确认口径):(暂无)")
    parts.append("请找出其中无法确定口径的业务词;没有就返回空列表。")
    return "\n\n".join(parts)


# ---------- 统计分析(方法选择 + 结果解读) ----------

STATS_PLAN_PROMPT = """你是一个统计方法选择器。根据用户问题和数据列结构,从下列 7 种方法中选出最合适的一个,
并填好调用参数。你不写任何计算代码,统计量由系统内的确定性函数计算。

可用方法(method 只能取这些值之一):
1. describe_stats:单列描述统计(均值/中位数/分位数/偏度/95% 置信区间)。
   适用:问「平均多少」「分布如何」「波动多大」。参数:col(数值列名)。
2. ttest:两组均值差异检验。适用:A/B 两组对比、实验组 vs 对照组、前后对比。
   参数:group_col(分组列)、value_col(数值列)、group_a、group_b(两组取值),
   可选 paired(默认 false,前后/配对设计设 true)、welch(默认 true,不假设方差齐性)。
3. chi_square:两个分类变量的独立性检验。适用:「性别与是否购买有关吗」「渠道和复购有关吗」。
   参数:row_col、col_col(两个分类列)。
4. anova:三组及以上均值差异(单因素方差分析)。
   参数:group_col、value_col。只有恰好两组时用 ttest。
5. correlation:两连续变量相关。适用:「收入和访问时长有关系吗」。
   参数:x、y(数值列),可选 method(pearson 默认 / spearman 用于单调非线线或含异常值)。
6. linear_regression:用若干自变量预测/解释一个连续因变量(OLS)。
   参数:y(连续因变量列)、xs(自变量列名列表)。
   注意:二分类因变量(0/1)的 logistic 回归不在本期范围,不要选它;
   若用户问二分类预测,选 linear_regression 并在 reason 中说明「为线性概率近似」。
7. ab_sample_size:实验前估算每组所需样本量。参数:baseline(基线转化率 0~1)、
   mde(绝对提升幅度,如 0.02)、可选 alpha(默认 0.05)、power(默认 0.8)。

选择原则:
- 先判断因变量类型与组数:两组比较→ttest;三组及以上→anova;两分类→chi_square;
  两连续→correlation;多自变量→linear_regression;单列→describe_stats;实验设计→ab_sample_size;
- 参数里的列名必须来自用户消息给出的列结构,禁止编造;
- 拿不准(列含义不明、分组取值不明、问题含糊)时 method 选 "clarify",params 留空,
  并在 reason 里写清需要用户补充什么。"""


STATS_INTERPRET_PROMPT = """你是一个严谨的统计结果解读助手。根据给定的统计方法、参数和计算结果,
写出一段面向非统计专业读者的解读。你没有计算能力,所有数字都来自给定结果,不得改写或编造。

必须做到:
1. 结论:说明差异/关系是否统计显著,同时区分「统计显著」与「实际重要」
   (给出效应量或均值差的实际幅度,不要只看 p 值);
2. 假设:明确说明假设是否满足——正态性(p<0.05 表示偏离正态)、方差齐性(Levene p<0.05
   表示方差不齐)、卡方期望频数(<5 时近似不可靠)、回归的线性/独立性假设是否可检验;
3. 注意事项:说明 p 值的实际含义(它是在原假设成立时观察到当前结果的概率,不是
   「假设为真的概率」),样本量、缺失值、观测性数据不能证明因果等局限;
4. 下一步:给出 2~4 条可执行的后续验证建议(如补采样、换非参数检验、做残差诊断)。
语气克制:不要使用「证明了」「必然」「显著提升」等夸大表述;结论不成立时直接说明。"""


def build_stats_plan_user_prompt(question: str, schema_info: dict) -> str:
    """组装方法选择的用户消息:问题 + 列结构 + 少量样本。

    参数:
        question: 用户的自然语言问题。
        schema_info: _describe_frame 的返回值,
            {columns: [{name, dtype, null_ratio}], sample: [前5行], row_count}。

    返回:
        格式化后的用户消息。只含 schema 与 5 行抽样,不含全量数据。
    """
    column_lines = [
        f"- {column['name']}({column['dtype']},缺失 {column['null_ratio']:.0%})"
        for column in schema_info.get("columns", [])
    ]
    columns_text = "\n".join(column_lines) if column_lines else "(无列)"
    return (
        f"用户问题:{question}\n\n"
        f"数据共 {schema_info.get('row_count', 0)} 行,列结构:\n{columns_text}\n\n"
        f"前 5 行样本(仅用于识别列含义与分组取值,不代表整体分布):\n"
        f"{schema_info.get('sample', [])}\n\n"
        f"请选择统计方法并填好参数。"
    )


def build_stats_interpret_user_prompt(
    question: str,
    method: str,
    params: dict,
    result: dict,
) -> str:
    """组装结果解读的用户消息:问题 + 方法 + 参数 + 确定性计算结果。

    参数:
        question: 用户原始问题。
        method: 选定的统计方法名。
        params: 实际传给统计函数的参数。
        result: stats 函数的返回值(已 JSON 序列化)。

    返回:
        格式化后的用户消息。只含统计结果,不含原始数据。
    """
    return (
        f"用户问题:{question}\n\n"
        f"统计方法:{method}\n"
        f"参数:{params}\n\n"
        f"计算结果(由确定性程序计算,请直接引用,不要重算):\n{result}\n\n"
        f"请按 system 提示的要求输出解读。"
    )


# ---------- code_analyst ----------

# 常驻规则只放"每一轮都必须遵守"的约束;图表与 A/B 规则按需拼接。
# 这样做的原因:无关规则会稀释模型注意力,同一件事在多处重复描述还会产生冲突。
CODEGEN_SYSTEM_PROMPT = """你是一个 pandas 数据分析代码生成器。根据用户问题和数据集 schema 生成可独立运行的 Python 脚本。

代码规则(必须遵守):
1. 只允许 pandas / numpy / matplotlib(标准库 math、json、re 可用);禁止 exec / eval / compile、联网,禁止读写 /data 以外的文件。
2. 用 pd.read_csv("/data/<文件名>", encoding="<encoding>") 读取,encoding 取用户消息中给出的值;/data 是沙箱内唯一数据目录。
3. 读入后必须 print(df.columns.tolist()) 与 print(df.dtypes.to_string());列名与 schema 不一致只 print 警告,禁止 assert / raise,按【已确认的分析计划】继续。
4. 最终结果赋值给 result(DataFrame 或 Series)并 print(result);标量包装成单行 DataFrame。
5. 脚本自包含:开头 import pandas as pd,不依赖任何外部变量;遵循 PEP 8。
6. 分析前 print【数据质量摘要】(总行数 / 重复行 / 各列缺失 / 非二值列 IQR),数字引用用户消息,禁止重算全量;二值列只报 0/1 分布,禁止 IQR。
7. 缺失值处理必须在注释中写明做法与原因,并报告影响行数;删除行必须报告删除比例;异常值只报告、不自动删除。
8. 若 result 是 DataFrame 且行数 ≤200000,print 后用 try/except 写 /outputs/result.csv(index=False, encoding="utf-8-sig");失败只 print,不抛出。
9. 每个函数写 docstring(≤5 行):用途 / Args(含义、单位、取值、默认) / Returns;无参数时省略 Args。
10. 非默认参数在调用处加行内注释;魔法数字提取为命名常量;注释用中文、英文标点,密度 ≤30%,禁止逐行翻译。
11. 脚本开头注释块写明:输入文件与编码、关键列假设、输出路径、复现方式。
12. 注释与示例中禁止出现真实数据(手机号 / 身份证号 / 姓名),一律用占位符。"""

CHART_RULES = """图表规则(仅在需要可视化时适用):
13. 先 matplotlib.use("Agg") 再 import pyplot;中文前设置 Noto Sans CJK JP 字体与 axes.unicode_minus=False。
14. 保存到 /outputs/chart_<英文短名>.png,savefig 后 close(),不要 show(),不必 print 路径。
15. 比例 / 转化率对比优先画「差值 + 95% CI」点图;柱图必须叠加 CI 误差棒并标注 Δ、CI、p;y 轴从 0 起时注明「差异视觉被压缩」。"""

AB_RULES = """A/B 实验规则(数据含二水平分组列与 0/1 结果列时适用):
16. 两比例 z 检验:输出每组 n、x、转化率(6 位小数)、绝对差(pp)、相对提升、RR、z、双侧 p、差值 95% CI。
    计算口径:p = math.erfc(abs(z) / math.sqrt(2));CI 用非合并标准误 diff ± 1.96 * se。
17. 双口径:未去重原始口径 + 按 user_id 首次曝光去重(主口径),两者都要给完整指标。
18. 存在实际曝光列时同时给 ITT 与 PP,并解释两者差异。
19. SRM:按分组样本量做卡方拟合优度(期望 = 总量 / 组数),p < 0.001 时标注「分流比例异常」。
20. 去重前先报告重复用户数、分组 / 曝光不匹配行数及两者是否重叠;重叠时合并为一个问题,不重复计数。
21. 含 timestamp 且跨度 ≥3 天:按「日期 × 分组」输出每日样本量与转化率并画折线;首日偏高时提示新鲜度效应。
22. 协变量平衡只用数据中真实存在的列;缺失维度写「数据未提供,无法检验」。
23. 所有关键指标汇总成一张表,放在脚本最后 print(stdout 只保留尾部,前面的数字会丢)。"""

DOMAIN_METRIC_RULES = """领域指标口径规则(仅当注入了领域指标时适用):
24. 凡注入的指标,必须在脚本末尾为每个指标 print 一行 METRIC <id>=<数值>(保留 6 位小数)。
25. 计算前必须先执行 metrics.yaml 的 filters;required_columns 含 users.* 时必须 join users
    并按 filters 排除内部/测试账号;所需表不存在时停止并说明缺什么,禁止自行换口径。"""


def _needs_chart(question: str, plan: list[str] | None) -> bool:
    """判断本轮是否需要图表规则。

    参数:
        question: 用户问题(str)。
        plan: 分析计划(list[str] | None)。

    返回:
        bool: 问题或计划里出现可视化意图词时为 True。
    """
    text = f"{question} {' '.join(plan or [])}"
    return any(word in text for word in ("图", "趋势", "分布", "对比", "占比", "可视化", "画"))


def _is_ab(schema_info: dict, question: str) -> bool:
    """判断是否为 A/B 实验场景。

    参数:
        schema_info: describe_dataset 的返回值(dict)。
        question: 用户问题(str)。

    返回:
        bool: 存在二值列 + 二水平列,或问题命中实验关键词时为 True。
    """
    columns = schema_info.get("columns") or []
    has_binary = any(column.get("is_binary") for column in columns)
    has_two_level = any(column.get("n_unique") == 2 for column in columns)
    keywords = ("A/B", "AB", "对照组", "实验组", "分流", "转化率")
    return (has_binary and has_two_level) or any(word in question for word in keywords)


def build_codegen_system_prompt(
    question: str,
    schema_info: dict,
    plan: list[str] | None = None,
    domain_metrics_injected: bool | None = None,
) -> str:
    """按场景拼接代码生成的 system 提示词:常驻规则 + 图表片段 + A/B 片段。

    参数:
        question: 用户问题(str, 用于识别可视化与 A/B 意图)。
        schema_info: describe_dataset 的返回值(dict, 用 columns 判定 A/B 结构)。
        plan: 已确认的分析计划(list[str] | None)。
        domain_metrics_injected: 是否注入了领域指标口径(bool | None);
                      None 时按 DOMAIN_PACK 是否启用惰性判定,调用方无需改签名。

    返回:
        str: 拼接后的 system 提示词;不需要的片段不出现,避免无关规则干扰;
             domain_metrics_injected 为 False 时输出与旧版逐字节一致。
    """
    fragments: list[str] = []
    if _needs_chart(question, plan):
        fragments.append(CHART_RULES)
    if _is_ab(schema_info, question):
        fragments.append(AB_RULES)
    if domain_metrics_injected is None:
        # 惰性判定:避免 prompts 模块在 import 时就依赖 domains.loader
        from domains.loader import domain_enabled

        domain_metrics_injected = domain_enabled()
    if domain_metrics_injected:
        fragments.append(DOMAIN_METRIC_RULES)
    if not fragments:
        return CODEGEN_SYSTEM_PROMPT
    return CODEGEN_SYSTEM_PROMPT + "\n\n" + "\n\n".join(fragments)




def build_codegen_user_prompt(
    question: str,
    dataset_name: str,
    schema_info: dict,
    previous_error: str = "",
    plan: list[str] | None = None,
    metric_block: str = "",
) -> str:
    """组装代码生成的用户消息:问题 + 已确认计划 + schema 摘要 + 样本(+ 上次错误) (+ 指标口径)。

    参数:
        question: 用户的自然语言问题(LLM 据此决定分析逻辑)。
        dataset_name: 要分析的数据集文件名(如 "orders.csv"),代码生成时写入
                      pd.read_csv("/data/<dataset_name>") 的路径。
        schema_info: describe_dataset 的返回值,包含 columns(列结构+类型+空值率)、
                     sample(前5行抽样)、row_count(总行数)、encoding(探测到的文件编码)。
        previous_error: 上一次沙箱执行失败的错误原因分析;非空时追加到消息末尾,
                        让 LLM「带着报错修复」而不是盲猜重写。
        plan: 经人工审批(可能被编辑)后的分析步骤清单。非空时作为强约束写入
              prompt,保证用户改过的计划真正驱动代码生成;为空时不影响原有行为。
        metric_block: 领域包插入的指标口径块,空串时不出现(旧行为逐字节不变)。
                      放在 schema 段之前,让 LLM 优先看「必须怎么算」再看「有什么列」。

    返回:
        格式化后的用户消息字符串送给 LLM。注意:送给 LLM 的只有 schema 摘要与
        抽样样本,绝不包含全量数据(遵守「全量数据禁止进入 LLM 上下文」红线)。
    """
    # 列结构带质量统计:让 LLM 能直接引用缺失数 / 异常值数 / 范围,
    # 满足「分析前先输出数据质量摘要」的强制规则,且无需自行重新统计
    def _column_line(c: dict) -> str:
        line = (
            f"- {c['name']} ({c['dtype']}, 空值率 {c['null_ratio']}, "
            f"唯一值 {c.get('n_unique', '?')}"
        )
        if c.get("is_constant"):
            line += ", 常数列"
        # 二值列(0/1 标记、bool、二分类标签)不做 IQR,只写「不适用」,绝不输出数字
        if c.get("is_binary"):
            line += ", 异常值(IQR) 不适用(二值列)"
            if c.get("negative_count") is not None:
                line += f", 负值 {c['negative_count']}"
            if c.get("min") is not None:
                line += f", 范围[{c['min']}, {c['max']}]"
        # 只有非二值数值列才有 outlier_count 字段;None 表示样本不足未计算
        elif "outlier_count" in c:
            oc = c.get("outlier_count")
            oc_text = "未计算(样本不足)" if oc is None else str(oc)
            line += f", 异常值(IQR) {oc_text}"
            if c.get("negative_count") is not None:
                line += f", 负值 {c['negative_count']}"
            if c.get("min") is not None:
                line += f", 范围[{c['min']}, {c['max']}]"
        line += ")"
        return line

    columns_text = "\n".join(_column_line(c) for c in schema_info["columns"])

    # 数据质量摘要单独成段:作为「分析前必须 print」的权威来源
    quality = schema_info.get("quality") or ""
    quality_block = f"\n\n【数据质量摘要】(分析前必须先 print 本段)\n{quality}" if quality else ""

    # 已确认计划放在问题之后、数据之前:优先约束分析逻辑,避免 LLM 自由发挥
    plan_text = ""
    if plan:
        steps = "\n".join(f"{index}. {step}" for index, step in enumerate(plan, 1))
        plan_text = f"【已确认的分析计划】(请严格按照该计划生成代码)\n{steps}\n\n"

    # 编码由 describe_dataset 探测得出:必须显式写进 prompt,
    # 否则 LLM 生成的代码会退回默认 UTF-8,GBK 文件再次触发 UnicodeDecodeError
    encoding = schema_info.get("encoding", "utf-8")

    # 无表头文件必须在【列结构】块首明确写出读取方式:
    # 否则模型会臆造中文表头,再用 schema 列名做校验,必然 KeyError。
    header_block = ""
    if schema_info.get("has_header") is False:
        names = ", ".join(
            f'"{column["name"]}"' for column in schema_info.get("columns", [])
        )
        header_block = (
            "本文件无表头。列名由系统按位置生成(col_1..col_n),读入必须 "
            f'pd.read_csv("/data/{dataset_name}", header=None, names=[{names}])\n'
        )

    # 领域包指标口径块:放在 plan 之后、schema 之前,让 LLM 先看「必须怎么算」
    # 再看「有什么列」;空串时整段不出现,旧行为逐字节不变
    metric_text = f"{metric_block}\n\n" if metric_block else ""

    base = (
        f"用户问题:{question}\n\n"
        f"{plan_text}"
        f"{metric_text}"
        f"数据集:{dataset_name}(共 {schema_info['row_count']} 行)\n\n"
        f'文件编码:{encoding}(生成代码必须显式传 encoding="{encoding}")\n\n'
        f"列结构(含数据质量统计):\n{header_block}{columns_text}\n"
        f"{quality_block}\n\n"
        f"抽样样本(最多 5 行,不是文件开头):\n{schema_info['sample']}\n\n"
        f"请生成分析代码。"
    )
    if previous_error:
        # 把上次失败的根因回喂给 LLM,实现「反思式重试」而非盲改
        base += (
            f"\n\n【上一次执行失败,错误原因如下,请针对性修复,不要重复同样的错误】\n"
            f"{previous_error}"
        )
    return base


# ---------- summarize(结果汇总) ----------

# stdout 送进汇总 prompt 的最大字数:超长输出先截断,避免上下文膨胀
SUMMARY_STDOUT_LIMIT = 2000

SUMMARY_SYSTEM_PROMPT = """你是一个数据分析报告的汇总撰写者。你的唯一职责是把「已经算好的执行结果」
归纳成结论先行的报告文字。你没有计算能力,也没有原始数据。

铁律(必须严格遵守):
1. 只能引用用户消息中「执行输出」里出现的数字;结果里没有的数字必须留空,
   禁止推算、禁止估算、禁止把 68.3 改写成 68、禁止编造百分比或增减幅度;
2. headline 必须包含一个来自执行输出的关键数字;找不到可用数字时留空字符串;
3. findings 写 2~4 条,每条都要带一个执行输出里的数字;
4. recommendations 写 2~3 条可执行建议,不要引入新数字;
5. caveats 至少 2 条,说明口径 / 局限(缺失值如何处理、样本是否代表性、p 值不能当
   「假设为真的概率」等);
6. method 说明口径与方法(分组怎么分、缺失怎么处理、统计量如何计算);
7. 语气克制,不使用「证明了」「必然」「显著提升」等夸大表述;
8. 只输出结构化结果,不要复述整段原始输出;
9. 结论必须分两句:第一句是统计结论(涉及组间差异时必须含 p 值与 95% 置信区间);
    「p 值」与「95% 置信区间」必须作为完整中文词出现,不得只写 p= 或 CI;
    第二句是业务含义,说明需要多大的提升才值得上线;用户未给阈值时写「需结合业务成本判断」;
10. 未做显著性检验时,结论必须写「未做显著性检验,不能判断差异是否真实」,
    不得用「显著 / 不显著」描述差异;
11. 禁止把二值列的 IQR 结果写成「异常值」(二值列没有 IQR 异常值);
    局限必须逐条对应本次实际缺失的检查项,不得用「口径偏保守」一类话术淡化问题。

followups 规则(推荐追问,必须严格遵守):
1. 必须恰好 3 条,每条不超过 25 个汉字;
2. 必须基于「用户问题 + 本次结论」推理,例如:
   - 结论提到两组差异 → 追问「按地区分层再看一次?」「差异的置信区间是多少?」
   - 结论提到异常值 → 追问「剔除异常值后结论会变吗?」
3. 禁止空话(如「还想了解什么」)、禁止重复原问题、禁止编号。"""


def build_summary_user_prompt(
    question: str,
    plan: list[str] | None,
    explanation: str,
    stdout: str,
    schema_info: dict,
    warnings: list[str] | None,
    charts: list[str] | None,
    fingerprint: str,
    quality_note: str = "",
) -> str:
    """组装汇总的用户消息:问题 + 计划 + 分析思路 + 执行输出 + schema 摘要 + 警告 + 图表 + 指纹 (+ 领域口径提示)。

    参数:
        question: 用户原始问题。
        plan: 已确认的分析计划(可为空)。
        explanation: code_analyst 的分析思路说明。
        stdout: 沙箱执行的原始输出(超过 SUMMARY_STDOUT_LIMIT 字先截断)。
        schema_info: describe_dataset 的返回值(只取列结构 / 行数 / 编码等摘要)。
        warnings: sanity check 产生的警告(可为空)。
        charts: 本次图表文件(相对 outputs/ 的路径)。
        fingerprint: 数据指纹短串(如 "测试数据.csv@a1b2c3d4e5f6")。
        quality_note: 领域包插入的 draft 口径提示,空串时不出现(旧行为逐字节不变)。

    返回:
        格式化后的用户消息。只含摘要级信息与执行输出,不含全量数据,
        遵守「全量数据不进 LLM 上下文」红线。
    """
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(plan or [], 1))
    plan_text = steps if steps else "(无)"

    column_lines = [
        f"- {column['name']} ({column['dtype']}, 空值率 {column['null_ratio']})"
        for column in schema_info.get("columns", [])
    ]
    schema_text = "\n".join(column_lines) if column_lines else "(无列)"

    # 超长 stdout 先截断:汇总只需要关键数字,不需要逐行明细
    raw_stdout = stdout or ""
    if len(raw_stdout) > SUMMARY_STDOUT_LIMIT:
        stdout_text = raw_stdout[:SUMMARY_STDOUT_LIMIT] + "\n…(输出过长,已截断)"
    else:
        stdout_text = raw_stdout

    warnings_text = "\n".join(f"- {item}" for item in (warnings or [])) or "(无)"
    charts_text = "\n".join(f"- {item}" for item in (charts or [])) or "(无)"

    # A/B 场景额外提醒:汇总模型容易只写主口径,漏掉原始口径与数据问题计数,
    # 这里在执行输出之后再明确要求一次(仍只允许引用执行输出里的数字)。
    ab_hint = ""
    if "转化" in question or "A/B" in question.upper() or "AB 实验" in question.upper():
        ab_hint = (
            "\n【A/B 汇总必填】findings 第一条写未去重原始口径两组转化率(4 位小数)"
            "与 p 值;第二条写去重主口径完整指标(含 p 值与 95% 置信区间);"
            "还要写 ITT 与 PP 双口径。caveats 必须写重复用户数、分组/落地页"
            "不匹配行数,以及两者是否重叠。统计结论中必须原样出现「p 值」、"
            "「95% 置信区间」「相对提升」这三个词,不得只写 p= 或 CI。\n"
        )

    # quality_note 只在领域包启用且有 draft 指标时传入,空串不追加
    quality_tail = f"\n{quality_note}" if quality_note else ""

    return (
        f"用户问题:{question}\n\n"
        f"【已确认的分析计划】\n{plan_text}\n\n"
        f"【分析思路】{explanation}\n\n"
        f"【数据版本】{fingerprint}\n"
        f"【数据规模】{schema_info.get('row_count', 0)} 行,"
        f"编码 {schema_info.get('encoding', '未知')}\n"
        f"【列结构】\n{schema_text}\n\n"
        f"【执行输出(唯一的数字来源,禁止改写)】\n{stdout_text}\n\n"
        f"【自动校验警告】\n{warnings_text}\n\n"
        f"【图表文件】\n{charts_text}\n"
        f"{ab_hint}\n"
        f"请按 system 提示输出结构化汇总。记住:只能引用上面执行输出里出现过的数字,"
        f"没有的数字留空,禁止推算。"
        f"{quality_tail}"
    )
