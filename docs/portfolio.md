# 作品集:自然语言数据分析 Agent

## 项目定位

一个本地单用户的 Web 工具:输入一句中文问题,Agent 自动发现数据、生成分析计划、在 Docker 沙箱里
执行 pandas 代码,最后产出带图表、数据指纹与执行代码的报告。核心约束是「AI 代码只在沙箱执行,
数字必须可回溯」,不是又一个聊天式 BI。

## 我的角色

独立完成从需求拆解到交付:LangGraph 状态图与检查点、沙箱封装、FastAPI + SSE 单页、审批风险分级、
会话删除与审计、离线测试与 8 条端到端回归用例,以及这套演示 / 作品集文档。

## 技术栈

LangGraph · LangChain · DeepSeek(OpenAI 兼容)· pandas / NumPy / Matplotlib · FastAPI · Docker ·
SQLite · pytest。

## 三个亮点

**1. 问题:模型爱编数字。做法:代码只在沙箱跑,报告写数据指纹。**
执行结果先落 `outputs/runs/run_id/`,报告标注 `文件名@sha256 前 12 位`、行数与编码;
汇总节点只允许引用执行输出里的数字,越界数字进警告。结果:111 条离线测试全绿,8 条端到端用例 8/8。

**2. 问题:每轮审批三次点击会让人放弃审批。做法:确定性风险分级。**
按「信任 + 高风险词 + PII 列 + 文件 > 50MB」判定 auto / confirm,auto 不弹卡但写审计;
策略异常一律回退人工。结果:31 次决策中 auto 占 16.1%,高风险问题仍被拦回确认。

**3. 问题:分析过程不可观测、数据删不干净。做法:运行索引 + trace + 级联删除。**
每次运行写 `index.jsonl` 与节点耗时;删除会话时检查点、会话索引、trace、run 归档一起删,
审批审计只脱敏问题文本。结果:已归档 78 次运行,单轮沙箱耗时中位 2.7 秒,删除可从审计表复查。

## 最难的一个取舍

安全与流畅的矛盾。全自动最顺,但代码由模型生成,一旦误删或外发就无法收场;全人工最稳,
但每轮三次点击会让审批形同虚设。我选择「规则先判、人工兜底」:只读聚合默认放行,
数据出口、PII、大文件必须人工确认,并把每次判定写进审计。代价是规则偏白名单、会误判,
所以保留 `APPROVAL_POLICY=confirm` 一键回到全人工。

## 可演示项

`scripts/bootstrap.ps1` 一键起服务;`docs/demo-script.md` 是 3 分钟逐字稿;
`examples/demo_sales.csv` 固定种子 1200 行,含缺失值与异常值;`demo_users_pii.csv` 演示 PII 审批理由。

## 链接

- 代码仓库:`https://github.com/your-name/data-analysis-agent`(占位,发布时替换)
- 指标快照:[docs/metrics.md](metrics.md)
- 演示剧本:[docs/demo-script.md](demo-script.md)

## English Summary

Built a local, single-user data-analysis agent that turns a Chinese question into a reproducible report.
The LLM only plans and writes pandas code; all generated code runs in a network-isolated Docker sandbox.
Every report carries a data fingerprint, execution code, and raw stdout, so each number can be traced back.
A deterministic approval policy keeps risky operations human-gated while auto-passing routine read-only queries.
It ships with 111 offline tests, an 8/8 end-to-end eval, a one-command bootstrap, and a three-minute demo.
