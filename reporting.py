"""统一报告渲染:analyze / ask_db / summarize 共用一份 Markdown 模板。

为什么统一:
    三个入口此前各自拼字符串,章节顺序、图表路径、数据版本口径各不相同,
    结论无法横向对比;集中到一处后,格式调整只改本文件,三个入口自动一致。
"""

from __future__ import annotations

from typing import Any


def _bullet_list(values: list[Any] | None, empty_text: str) -> list[str]:
    """把字符串列表渲染成 Markdown 列表;空列表输出一行占位说明。

    为什么保留占位:章节标题必须始终存在(eval 与人工都按标题定位),
    内容为空时给出明确的「(无)」而不是让章节消失。
    """
    items = [str(value).strip() for value in (values or []) if str(value).strip()]
    return [f"- {item}" for item in items] if items else [empty_text]


def render_report(run: dict[str, Any]) -> str:
    """把一次运行的元信息 + 摘要 + 代码渲染成固定章节的 Markdown 报告。

    参数:
        run: 运行上下文字典,支持的键(缺失即按占位处理):
            run_id / question / dataset / meta(list[str]) / now /
            summary(dict|None) / fallback_headline / fallback_findings /
            warnings / charts(相对 outputs/ 的路径) / schema / fingerprint /
            method(摘要缺失时的口径兜底) / sql / code / duration_ms / iteration。

    返回:
        完整 Markdown 文本。章节顺序固定,五个核心章节标题一字不差:
        "## 一句话结论" "## 关键发现" "## 建议" "## 局限与数据质量" "## 口径与数据版本"。

    为什么用 fallback_*:失败 / 未启用汇总时没有 summary,但仍要有结论,
    否则报告会留空,违反「禁止静默失败」。
    """
    summary = run.get("summary") or {}
    schema = run.get("schema") or {}
    warnings = [str(item) for item in (run.get("warnings") or []) if str(item).strip()]
    charts = [str(item) for item in (run.get("charts") or []) if str(item).strip()]

    headline = (
        str(summary.get("headline") or "").strip()
        or str(run.get("fallback_headline") or "").strip()
        or "(无结论)"
    )
    findings = summary.get("findings") or run.get("fallback_findings") or []
    recommendations = summary.get("recommendations") or []
    caveats = summary.get("caveats") or []
    method = (
        str(summary.get("method") or "").strip()
        or str(run.get("method") or "").strip()
        or "(未提供)"
    )

    # ---------- 头部元信息:谁 / 何时 / 哪次运行 ----------
    header: list[str] = ["# 分析报告", ""]
    header.extend(str(line) for line in (run.get("meta") or []))
    if run.get("dataset"):
        header.append(f"- 数据集：{run['dataset']}")
    if run.get("question"):
        header.append(f"- 问题：{run['question']}")
    if run.get("run_id"):
        header.append(f"- 运行 ID：{run['run_id']}")
    if run.get("now"):
        header.append(f"- 时间：{run['now']}")
    header.append("")

    # ---------- 执行 SQL:仅数据库入口传入,位置固定在生成代码之前 ----------
    sql_section: list[str] = []
    sql = str(run.get("sql") or "").strip()
    if sql:
        sql_section = ["## 执行的 SQL", "", "```sql", sql, "```", ""]

    # ---------- 数据版本:指纹 + 规模 + 编码,保证结论可回溯 ----------
    version_lines: list[str] = []
    if run.get("fingerprint"):
        version_lines.append(f"- 数据指纹：{run['fingerprint']}")
    if schema.get("row_count") is not None:
        version_lines.append(f"- 行数：{schema.get('row_count')}")
    if schema.get("encoding"):
        version_lines.append(f"- 编码：{schema.get('encoding')}")
    if schema.get("size_bytes") is not None:
        version_lines.append(f"- 文件大小：{schema.get('size_bytes')} 字节")
    if schema.get("mtime_iso"):
        version_lines.append(f"- 文件更新时间：{schema.get('mtime_iso')}")
    version_lines.append(f"- 口径与方法：{method}")

    # ---------- 耗时与尝试次数 ----------
    cost_lines: list[str] = []
    if run.get("duration_ms") is not None:
        cost_lines.append(f"沙箱执行耗时：{run['duration_ms']} ms")
    if run.get("token_used") is not None:
        cost_lines.append(f"token 用量：{run['token_used']} tokens")
    if run.get("iteration") is not None:
        cost_lines.append(f"尝试次数：{run['iteration']}")

    code = str(run.get("code") or "").rstrip()

    body: list[str] = [
        *header,
        "## 一句话结论",
        "",
        headline,
        "",
        "## 关键发现",
        "",
        *_bullet_list(findings, "(无)"),
        "",
        "## 建议",
        "",
        *_bullet_list(recommendations, "(无)"),
        "",
        "## 局限与数据质量",
        "",
        *_bullet_list(caveats, "(未提供)"),
        "",
    ]

    # 自动校验警告单列一节:只在有警告时出现,避免空章节干扰阅读
    if warnings:
        body.extend(["## 数据质量警告", "", *_bullet_list(warnings, "(无)"), ""])

    body.extend(
        [
            "## 图表",
            "",
            *_bullet_list([f"outputs/{chart}" for chart in charts], "(无)"),
            "",
            "## 口径与数据版本",
            "",
            *version_lines,
            "",
            *sql_section,
            "## 生成代码",
            "",
            "```python",
            code if code else "(未生成代码)",
            "```",
            "",
            "## 耗时与尝试次数",
            "",
            *(_bullet_list(cost_lines, "(无)")),
            "",
        ]
    )
    return "\n".join(body)
