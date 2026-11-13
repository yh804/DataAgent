# 电商领域包

> 版本:v0.1(2026-11-06)
> 定位:**文档 + 合成数据**,不包含任何 Agent 运行时代码改动。
> 数据为虚构演示数据,不含真实公司、用户或交易信息。

## 1. 这个包解决什么问题

把「电商运营的一句自然语言问题」映射到固定的**指标口径、数据表、分析步骤与图表规范**,让不同人产出的报告可以横向比较。当前包只覆盖**离线描述性分析**,不覆盖预测、因果推断与实时看板。

包内所有需要业务决策的口径都标为 `draft`,并在 `metrics.yaml` 的 `open_questions` 里列出候选选项;本包**不替业务拍板**。

## 2. 目录结构

```text
domains/ecommerce/
├── README.md                    # 本文件:怎么用、哪些口径未确认、怎么接入
├── spec.md                      # 业务背景、读者、3 个决策问题、范围与非目标、术语表
├── metrics.yaml                 # 10 个指标的口径定义(9 draft + 1 confirmed)
├── schema.md                    # 订单/明细/用户/商品/流量 五张表的字段契约
├── playbooks.md                 # 8 个分析套路:触发问法 → 步骤 → 指标 → 图表 → 常见坑
├── frameworks.md                # 决策骨架:北极星候选、恒等式分解链、统一漏斗、AARRR、5W2H
├── eval/
│   └── cases.json               # 12 条电商回归问题(不修改根目录 eval/cases.json)
├── check_package.py             # 自检:yaml/json 可解析、指标 id 一致、列名一致
└── examples/
    ├── generate_demo_data.py    # 固定种子 20260101 的生成脚本
    ├── orders.csv               # 2000 行
    ├── order_items.csv          # 3786 行
    ├── users.csv                # 1500 行
    ├── products.csv             # 120 行
    └── traffic.csv              # 450 行
```

## 3. 怎么用

### 3.1 阅读顺序

1. 先读 [spec.md](spec.md) 了解业务边界与 3 个决策问题;
2. 再读 [schema.md](schema.md) 确认字段与脏数据规则;
3. 做分析前查 [metrics.yaml](metrics.yaml) 选中指标 id,并检查 `status`;
4. 按 [playbooks.md](playbooks.md) 对应套路的步骤执行,照抄「常见坑」做自检;
5. 需要回归时使用 `eval/cases.json`。

### 3.2 重新生成数据

```bash
python domains/ecommerce/examples/generate_demo_data.py
```

固定种子 `20260101`,重复运行会得到同样内容并覆盖同名 CSV。若要对比,请先提交或备份。

### 3.3 自检

```bash
python domains/ecommerce/check_package.py
```

检查项:YAML / JSON 可解析、`playbooks.md` 与 `eval/cases.json` 引用的指标 id 都存在、五张 CSV 能被 pandas 读入且列名与 `schema.md` 一致、关键指标参考值落在容差内。

### 3.4 在 Agent 里试跑

当前 Agent 一次只绑定一个数据集文件。建议把 `examples/` 里的 CSV 复制到 `data/processed/` 后逐表提问;涉及多表关联的问题(品类、价格带、RFM)需要等后续接入多表读取能力。本包**不修改** `prompts.py`、`nodes/`、`tools.py`。

## 4. 数据快照(用于回归对账)

| 表 | 行数 | 主键 | 粒度 |
|---|---|---|---|
| orders.csv | 2000 | 订单号 | 订单 |
| order_items.csv | 3786 | (订单号, 明细行号) | 订单行 |
| users.csv | 1500 | 用户ID | 用户 |
| products.csv | 120 | 商品ID | SKU |
| traffic.csv | 450 | (日期, 渠道, 实验组) | 日期×渠道×实验组 |

关键指标参考值(干净口径:排除测试单、刷单、内部账号、测试账号):

| 指标 | 参考值 | 说明 |
|---|---|---|
| GMV | 1,193,284.77 元 | 含 2 笔异常金额单,默认不扣退款 |
| 支付订单数 | 1,690 | 含全额退款与 0 元单 |
| 客单价 | 706.09 元 | GMV / 支付订单数 |
| 退款率(订单口径) | 7.34% | 退款订单数 / 支付订单数 |
| 复购率(90 天) | 45.83% | ≥2 单用户 / ≥1 单用户 |
| 新客占比(订单口径) | 59.53% | 新客支付订单 / 支付订单 |
| 连带率(不含赠品) | 2.47 件/单 | 明细件数 / 支付订单数 |
| 整体支付转化率 | 4.4015% | traffic 支付人数 / 访问人数 |
| AB 相对提升 | 20.65% | 实验组 vs 对照组,参考 p ≈ 0.0032 |

> 数值由 `check_package.py` 从 CSV 重新计算并校验,不手写。数据重新生成后若数值变化,自检会失败,提示更新 `eval/cases.json` 的 `tolerance`。

## 5. 尚未确认的口径

| 指标 id | 名称 | 状态 | 待确认的核心问题 |
|---|---|---|---|
| `gmv` | GMV | draft | 是否扣退款;按下单日还是支付日;是否含运费 |
| `aov` | 客单价 | draft | 毛 GMV 还是净 GMV;订单口径还是用户口径 |
| `pay_conversion_rate` | 支付转化率 | draft | 分母用访问还是曝光;是否按用户去重 |
| `refund_rate` | 退款率 | draft | 订单口径还是金额口径;退款中是否计入 |
| `repurchase_rate` | 复购率 | draft | 窗口 30/90 天;退款单是否剔除 |
| `repurchase_interval` | 复购间隔 | draft | 首单→二单还是全部间隔;中位数还是均值 |
| `new_customer_ratio` | 新客占比 | draft | 订单口径还是用户口径;新客定义 |
| `attach_rate` | 连带率 | draft | 件数 / 品类数 / SKU 数;赠品是否计入 |
| `cart_add_rate` | 加购转化率 | draft | 分母口径;是否按用户去重 |
| `order_count` | 订单数 | confirmed | 已明确:发生过支付即计 1 单,含全额退款与 0 元单 |

**未纳入的指标**:客诉率。当前五张表没有客服工单表,无法计算;需要后续补充数据源后再定义。

## 6. 怎么接入 Agent(后续步骤,本轮不做)

1. **数据接入**:把 `examples/*.csv` 复制到 `data/processed/`,沿用现有的发现 / 描述 / 沙箱执行链路,不需要改代码;
2. **多表问题**:为多表读取增加领域适配(如表名→文件映射、关联键声明),需要改运行时代码,属于后续任务;
3. **指标注入**:在确认口径后,把 `metrics.yaml` 中 `confirmed` 的指标编译进提示词或校验规则;仍属后续任务;
4. **评估接入**:`eval/cases.json` 目前只是领域用例集合,根目录 `eval/run_eval.py` 只读根目录用例;要跑本包用例需要增加用例路径参数,属于后续任务;
5. **口径确认闭环**:业务确认一个 `open_questions` 后,把该指标 `status` 改为 `confirmed`,同步更新 `spec.md` 与 `README.md` 的待确认清单。

## 7. 维护约定

- 改字段:先改 `schema.md`,再改生成脚本与 `check_package.py`,最后重新生成数据;
- 改指标:只改 `metrics.yaml`,并跑自检确认 `playbooks.md` 与 `eval/cases.json` 的引用仍然有效;
- 改数据规模:必须保持固定种子与可复现,并在本文件「数据快照」更新行数与参考值;
- 不允许把 draft 指标在报告中写成确定口径;报告必须提示「口径待确认」;
- 不允许在文档或数据中出现真实公司、真实用户、真实手机号、真实交易。

## 8. 边界

本包只回答「怎么定义、怎么查、怎么讲」,不回答「业务上应该选哪个口径」。后者需要业务方在 `open_questions` 中做选择。
