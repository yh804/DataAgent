# 指标快照

> 本文件由 `scripts/collect_metrics.py` 生成,请勿手改;数字全部来自真实产物。
> 生成时间: 2026-11-05T15:15:24

## 指标

| 指标 | 数值 | 口径 |
|---|---|---|
| 单元测试 | 暂无数据 | `pytest --collect-only -q`(--with-tests) |
| 回归评估 | 9/9 (100.0%) | 最新 eval_report_20261104_221402.json(2026-11-04T22:14:02) |
| 运行总数 / 成功率 | 9 / 100.0% | `outputs/index.jsonl` 的 status(只记录完成归档的运行) |
| 单轮沙箱耗时 | p50 6.4 s / 均值 5.2 s | index.jsonl 的 duration_ms(非空样本 9 条) |
| token 均值 | 8,604 | index.jsonl 的 token_used(跳过 null,样本 9 条) |
| 审批决策 | 84 次:auto 7 (8.3%) / confirm 77 (91.7%) | `outputs/approvals.jsonl` 的 level(auto / confirm)占比 |
| 人工拒绝 | 4 | decided_by=human 且 action=reject |
| 会话数 / 已删除 | 7 / 19 | sessions.jsonl / deletions.jsonl 去重 thread_id |

## 测量口径

- 机器: Windows 11 (AMD64);Python 3.13.9
- Docker: 29.8.2
- 模型: deepseek-chat(通过 OpenAI 兼容接口调用)
- 样本量: runs 9 条 / approvals 84 条 / sessions 7 条 / eval 9 条 / tests 0 条
- 生成方式: `python scripts/collect_metrics.py --with-tests`(不调 LLM、不起容器、不联网)

## 复现命令

```bash
python -m pytest -q                     # 离线单元测试
python eval/run_eval.py                 # 端到端回归(需 Docker + LLM)
python scripts/smoke.py                 # 冒烟自检(无需 Docker / LLM)
python scripts/collect_metrics.py --with-tests
```
