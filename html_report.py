"""单文件 HTML 报告渲染:从 result.json 摘要 + 图表文件生成可离线打开的 HTML。

红线(安全第一,任何改动都不能违反):
    - LLM 绝不参与 HTML 生成;本模块只消费 result.json 里的结构化字段,
      哪怕 summary 是 LLM 写的,也只当纯文本消费,不当 HTML 渲染;
    - 所有文本一律 html.escape,禁止裸拼 <script>/<img> 等标签,
      防止把模型输出当 HTML 执行(XSS);
    - 不引用任何外部资源(无 CDN / http / @import / <link>),
      断网双击也能看;发给别人也不怕链接失效;
    - 图表按原图 base64 内嵌,总体积超过上限时只内嵌前若干张,
      并在警告里明确告诉用户「其余图去归档目录看」,不静默丢图。
"""

from __future__ import annotations

import base64
import html
from pathlib import Path
from typing import Any

# 单文件体积上限(字节):超过时只内嵌前 MAX_INLINE_CHARTS 张图
# 8MB 是经验值:邮件附件上限、企业微信/飞书文件预览都比较友好
MAX_HTML_BYTES = 8 * 1024 * 1024
# 超限时的内嵌图表数上限:防止一张报告塞 30 张图把浏览器卡死
MAX_INLINE_CHARTS = 6
# 原始输出 / 代码的截断长度(字符):
# 沙箱 stdout 可能几万行报错,全塞进去报告会被撑爆,截断到 20 万字符
MAX_PRE_CHARS = 200_000


def _escape(value: Any) -> str:
    """转义任意输入为空安全的 HTML 文本;None / 空白统一转成占位说明。

    设计要点:
    - 先 str(value) 把数字、None、dict 统一成字符串;
    - strip() 去掉前后空白,防止只有空格的脏数据占一行;
    - html.escape 把 < > & " 转成实体,杜绝 XSS;
    - 空字符串直接返回 "",让上层能用 `if text` 判断要不要渲染。
    """
    text = str(value or "").strip()
    return html.escape(text) if text else ""


def _bullet_items(values: list[Any] | None, empty_text: str) -> str:
    """把列表渲染成 <ul><li>;空列表输出一行灰色占位,保证章节始终存在。

    为什么空列表不直接返回 "":
        报告结构要稳定——「关键发现」一节即使没内容也要留个空壳,
        不然读者会以为漏了、或者渲染坏了。
    """
    items = [str(item).strip() for item in (values or []) if str(item).strip()]
    if not items:
        return f'<p class="empty">{html.escape(empty_text)}</p>'
    # 每一项都单独 escape,不能一次性 escape 整个字符串,
    # 否则换行/引号也会被转义,但 li 边界会丢
    lis = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"<ul>{lis}</ul>"


def _meta_row(label: str, value: Any) -> str:
    """渲染头部元信息的一行(<dt>/<dd>);值为空时不渲染该行。

    为什么空值不渲染:
        头部元信息(数据集、问题、运行 ID...)有的字段可能缺失,
        留一行空 dd 很丑;直接跳过这对 dt/dd 更干净。
    """
    text = _escape(value)
    if not text:
        return ""
    return f"<dt>{html.escape(label)}</dt><dd>{text}</dd>"


def _load_chart_b64(chart_path: Path) -> tuple[str | None, str | None]:
    """读取图表文件并转成 data URI;文件缺失 / 不是 PNG 时返回 (None, 警告)。

    返回二元组约定:
        - 成功: (data_uri, None)
        - 失败: (None, "警告文字")
    这样上层 for 循环里可以 `data_uri, warning = ...; if warning: ...`。

    为什么要 base64 内嵌而不是 <img src="charts/x.png">:
        data URI 把图片直接塞进 HTML,单文件自包含;
        不内嵌的话,把 report.html 单独发给别人,图片全裂。
    """
    # 文件可能在生成中途被删、或旧运行没图,缺了直接跳过
    if not chart_path.is_file():
        return None, f"图表缺失,已跳过: {chart_path.name}"
    # 只允许常见位图格式;svg 虽然能内嵌但有 XSS 风险(可嵌 <script>),
    # 这里干脆不支持,避免引入新攻击面
    suffix = chart_path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return None, f"图表格式不支持内嵌({suffix}),已跳过: {chart_path.name}"
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }[suffix]
    try:
        raw = chart_path.read_bytes()
    except OSError:
        # 权限问题 / 文件被占 / 磁盘错误,不阻断整个报告
        return None, f"图表读取失败,已跳过: {chart_path.name}"
    # base64 编码:图片二进制 → ASCII 字符串,才能塞进 HTML
    # 体积会变大 33%(每 3 字节变 4 字符),但换来自包含
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}", None


def render_html_report(run: dict[str, Any], charts: list[str], outputs_dir: Path) -> str:
    """把一次运行的归档数据渲染成单文件 HTML 报告。

    参数:
        run: 与 reporting.render_report 同构的运行上下文字典;
             只用 summary / question / dataset / run_id / now / warnings /
             fingerprint / schema / stdout / code 这些字段。
        charts: 归档后的图表相对路径列表(形如 "runs/<id>/charts/x.png",
                相对 outputs_dir)。
        outputs_dir: outputs/ 目录的绝对路径;图表文件从这里读取。

    返回:
        完整 HTML5 字符串;不含任何外部引用,所有文本均已转义。

    失败策略:
        图表缺失 / 读取失败只追加警告并跳过,绝不抛出;
        调用方负责把返回的 HTML 写盘失败时的异常兜底。
    """
    # 取出结构化字段,统一用 .get(..., 空) 防止 KeyError
    summary = run.get("summary") or {}
    schema = run.get("schema") or {}
    warnings = [str(item) for item in (run.get("warnings") or []) if str(item).strip()]

    # 一句话结论:优先用 LLM 写的 headline,没有就用兜底文案,
    # 最后再退到「(无结论)」保证永远有内容
    headline = (
        _escape(summary.get("headline"))
        or _escape(run.get("fallback_headline"))
        or "(无结论)"
    )
    # 关键发现 / 建议 / 局限:同样支持 fallback,LLM 没生成时用兜底
    findings = summary.get("findings") or run.get("fallback_findings") or []
    recommendations = summary.get("recommendations") or []
    caveats = summary.get("caveats") or []
    method = _escape(summary.get("method") or run.get("method")) or "(未提供)"

    # ---------- 图表:base64 内嵌,超限只留前 MAX_INLINE_CHARTS 张 ----------
    chart_html: list[str] = []       # 已成功内嵌的 <figure> 片段
    chart_warnings: list[str] = []   # 跳过原因(会进 warnings 节展示)
    inlined_bytes = 0                # 已内嵌图片累计字节数(含 base64 膨胀)
    for rel_path in charts:
        # rel_path 形如 runs/<id>/charts/x.png;outputs_dir 是 outputs/ 的绝对路径
        # 用 Path 拼接,不手拼字符串,避免路径穿越
        chart_file = outputs_dir / rel_path
        data_uri, warning = _load_chart_b64(chart_file)
        if warning:
            chart_warnings.append(warning)
            continue
        # 注意:这里算的是 base64 编码后字符数,不是原 PNG 字节数,
        # 这样 inlined_bytes 才真实反映 HTML 文件会多大
        size = len(data_uri.encode("ascii"))
        # 双重上限:数量到 6 张 OR 累计体积到 8MB,任一触发就停止内嵌
        if len(chart_html) >= MAX_INLINE_CHARTS or inlined_bytes + size > MAX_HTML_BYTES:
            chart_warnings.append(
                f"图表数量/体积超出单文件上限,其余图表见归档目录: {rel_path}"
            )
            continue
        # <figure> 自带 figcaption,图注直接用文件名,简单明了
        chart_html.append(
            f'<figure><img src="{data_uri}" alt="{html.escape(chart_file.name)}">'
            f"<figcaption>{html.escape(chart_file.name)}</figcaption></figure>"
        )
        inlined_bytes += size
    # 把图表警告合并进全局 warnings,统一在「数据质量警告」节展示
    warnings.extend(chart_warnings)

    # ---------- 原始输出与代码:截断 + 转义 ----------
    # stdout 是沙箱 print 出来的全部输出,可能很长,先截断
    stdout = str(run.get("stdout") or "")
    if len(stdout) > MAX_PRE_CHARS:
        stdout = stdout[:MAX_PRE_CHARS] + "\n…(输出过长,已截断)"
    # 代码只去掉尾部空行,不动其他内容,保留原始缩进
    code = str(run.get("code") or "").rstrip()

    # ---------- 数据版本行(数据指纹、行数、编码、大小、mtime) ----------
    # 这些字段是「可复现性」的关键:别人拿到这份报告,能核对是不是同一份数据
    version_rows = [
        _meta_row("数据指纹", run.get("fingerprint")),
        _meta_row("行数", schema.get("row_count")),
        _meta_row("编码", schema.get("encoding")),
        _meta_row("文件大小(字节)", schema.get("size_bytes")),
        _meta_row("文件更新时间", schema.get("mtime_iso")),
        f"<dt>口径与方法</dt><dd>{method}</dd>",
    ]

    # ---------- 数据质量警告(有警告才显示这一节) ----------
    # 没警告就不渲染这一节,避免空黄色框;warning_html 保持空字符串
    warning_section = ""
    if warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
        warning_heading = html.escape("数据质量警告")
        warning_block = (
            f'<section class="warnings"><h2>{warning_heading}</h2><ul>{items}</ul></section>'
        )
        warning_html = warning_block
    else:
        warning_html = ""

    # ---------- 固定五节标题(与 Markdown 报告一字不差) ----------
    # 所有标题字面量都过一遍 html.escape,虽然这里是写死的中文,
    # 但保持统一风格,以后改标题也不会忘转义
    sec_headline = html.escape("一句话结论")
    sec_findings = html.escape("关键发现")
    sec_recommend = html.escape("建议")
    sec_limits = html.escape("局限与数据质量")
    sec_version = html.escape("口径与数据版本")
    sec_charts = html.escape("图表")
    sec_stdout = html.escape("原始输出")
    sec_code = html.escape("生成代码")

    # 头部元信息块:数据集、问题、运行 ID、时间、数据指纹
    # 用生成器 + if row 过滤空行,空字段直接不占位置
    meta_rows = "".join(
        row
        for row in [
            _meta_row("数据集", run.get("dataset")),
            _meta_row("问题", run.get("question")),
            _meta_row("运行 ID", run.get("run_id")),
            _meta_row("时间", run.get("now")),
            _meta_row("数据指纹", run.get("fingerprint")),
        ]
        if row
    )

    # 图表区如果一张都没内嵌成功,显示「(无)」占位
    chart_block = "\n".join(chart_html) if chart_html else '<p class="empty">(无)</p>'

    # ---------- 最终 HTML 模板 ----------
    # CSS 用 <style> 内嵌,不引外部 CSS 文件,保持单文件离线
    # 颜色用 CSS 变量定义在 :root,以后改主题只改一处
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>分析报告 · {html.escape(str(run.get("run_id") or ""))}</title>
<style>
:root {{
  /* 颜色变量:主前景/弱化文字/边框/背景/卡片/主色/警告色 */
  --fg: #1e293b; --muted: #64748b; --border: #e2e8f0;
  --bg: #f8fafc; --card: #ffffff; --accent: #4f46e5; --warn: #b45309;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  line-height: 1.65;
}}
/* 报告容器:最大宽度 56rem,居中,窄屏自适应 */
.report {{ max-width: 56rem; margin: 0 auto; }}
/* 头部卡片:数据集/问题/运行ID 等元信息 */
header {{
  background: var(--card); border: 1px solid var(--border); border-radius: 0.75rem;
  padding: 1.25rem 1.5rem; margin-bottom: 1.25rem; position: relative;
}}
header h1 {{ margin: 0 0 0.75rem; font-size: 1.35rem; }}
/* 元信息用两列网格:左 label 右 value */
dl.meta {{
  display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 1rem;
  margin: 0; font-size: 0.85rem;
}}
dl.meta dt {{ color: var(--muted); }}
dl.meta dd {{ margin: 0; word-break: break-all; }}
/* 右上角小徽章:标明「模板渲染」,和 LLM 自由输出区分 */
.badge {{
  position: absolute; top: 1.1rem; right: 1.25rem;
  background: #eef2ff; color: var(--accent); border: 1px solid #c7d2fe;
  border-radius: 9999px; padding: 0.15rem 0.6rem; font-size: 0.72rem;
}}
/* 每个章节都是一张卡片 */
section {{
  background: var(--card); border: 1px solid var(--border); border-radius: 0.75rem;
  padding: 1.1rem 1.5rem; margin-bottom: 1rem;
}}
section h2 {{
  margin: 0 0 0.6rem; font-size: 1.02rem; color: var(--accent);
  border-bottom: 1px solid var(--border); padding-bottom: 0.4rem;
}}
ul {{ margin: 0.4rem 0 0; padding-left: 1.3rem; }}
li {{ margin: 0.25rem 0; }}
/* 空状态的灰色小字 */
.empty {{ color: var(--muted); font-size: 0.85rem; }}
/* 警告节:黄色底,视觉上和普通节区分 */
.warnings h2 {{ color: var(--warn); }}
.warnings {{ background: #fffbeb; border-color: #fde68a; }}
/* 图表卡片:白底居中,图片自适应宽度 */
figure {{
  margin: 0.9rem 0; padding: 0.6rem; border: 1px solid var(--border);
  border-radius: 0.5rem; background: #fff; text-align: center;
}}
figure img {{ max-width: 100%; height: auto; }}
figcaption {{ margin-top: 0.4rem; font-size: 0.78rem; color: var(--muted); }}
/* 代码块:深色底,横向可滚动,长行自动换行 */
pre {{
  margin: 0.5rem 0 0; padding: 0.8rem; background: #0f172a; color: #e2e8f0;
  border-radius: 0.5rem; overflow-x: auto; font-size: 0.78rem;
  white-space: pre-wrap; word-break: break-all;
}}
footer {{ text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 1.5rem; }}
</style>
</head>
<body>
<div class="report">
<!-- 头部:徽章 + 标题 + 元信息 -->
<header>
  <span class="badge">模板渲染</span>
  <h1>分析报告</h1>
  <dl class="meta">{meta_rows}</dl>
</header>

<!-- 固定五节:一句话结论 / 关键发现 / 建议 / 局限 -->
<section><h2>{sec_headline}</h2><p>{headline}</p></section>
<section><h2>{sec_findings}</h2>{_bullet_items(findings, "(无)")}</section>
<section><h2>{sec_recommend}</h2>{_bullet_items(recommendations, "(无)")}</section>
<section><h2>{sec_limits}</h2>{_bullet_items(caveats, "(未提供)")}</section>

<!-- 条件渲染:有警告才插入 -->
{warning_html}

<section><h2>{sec_version}</h2><dl class="meta">{"".join(version_rows)}</dl></section>
<section><h2>{sec_charts}</h2>{chart_block}</section>
<!-- pre 里的内容必须 escape,否则 stdout 里的 < 会被当成标签 -->
<section><h2>{sec_stdout}</h2><pre>{html.escape(stdout) if stdout else "(无)"}</pre></section>
<section><h2>{sec_code}</h2><pre>{html.escape(code) if code else "(未生成代码)"}</pre></section>

<footer>本报告由数据分析 Agent 模板自动生成,可直接离线保存与分享。</footer>
</div>
</body>
</html>
"""
