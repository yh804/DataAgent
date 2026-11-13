# 演示数据库 `examples/ecommerce.db`

> 由 `scripts/make_demo_db.py` 从 `domains/ecommerce/examples/*.csv` 装载生成。
> 表名用英文,列名保持 CSV 中的中文,便于与 `domains/ecommerce/metrics.yaml`
> 的口径(如 `orders.支付金额`)直接对齐。

## 1. 生成与覆盖

在项目根目录执行:

```bash
python scripts/make_demo_db.py --out examples/ecommerce.db
```

参数:

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--examples-dir` | `domains/ecommerce/examples` | CSV 目录 |
| `--out` | `examples/ecommerce.db` | 输出库路径 |
| `--force` | 关闭 | 目标已存在时覆盖;不加且文件存在则报错退出(退出码 2) |

脚本先在目标目录写临时文件,成功后原子替换。装载失败时只删除临时文件,
已存在的旧库不会被破坏。同一份 CSV 重复执行,行数与内容校验和一致。

自检:

```bash
python -m pytest tests/test_make_demo_db.py -q
```

## 2. 表结构

所有日期 / 时间列以 TEXT 存 ISO8601(`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`),
金额列为 REAL,计数列为 INTEGER;`是/否` 列用 CHECK 约束限定枚举值。

### `users` 用户表(1500 行)

| 列名 | 类型 | 可空 | 键 / 约束 |
|---|---|---|---|
| `用户ID` | TEXT | 否 | 主键 |
| `注册日期` | TEXT | 否 | 日期格式 CHECK |
| `注册渠道` | TEXT | 否 | |
| `地区` | TEXT | 否 | |
| `会员等级` | TEXT | 否 | |
| `性别` | TEXT | 否 | |
| `年龄` | INTEGER | 是 | `>= 0` |
| `手机号` | TEXT | 否 | 脱敏假值,不得输出完整号码 |
| `是否内部账号` | TEXT | 否 | `是/否` CHECK |
| `是否测试账号` | TEXT | 否 | `是/否` CHECK |

### `products` 商品表(120 行)

| 列名 | 类型 | 可空 | 键 / 约束 |
|---|---|---|---|
| `商品ID` | TEXT | 否 | 主键 |
| `商品名称` | TEXT | 否 | |
| `品类` | TEXT | 否 | |
| `子品类` | TEXT | 否 | |
| `品牌` | TEXT | 否 | |
| `单价` | REAL | 否 | `>= 0` |
| `成本价` | REAL | 否 | `>= 0` |
| `上架日期` | TEXT | 否 | 日期格式 CHECK |
| `是否在售` | TEXT | 否 | `是/否` CHECK |

### `orders` 订单表(2000 行)

| 列名 | 类型 | 可空 | 键 / 约束 |
|---|---|---|---|
| `订单号` | TEXT | 否 | 主键 |
| `用户ID` | TEXT | 否 | 外键 → `users.用户ID` |
| `订单日期` | TEXT | 否 | 日期格式 CHECK |
| `支付时间` | TEXT | 是 | 时间格式 CHECK;待支付 / 已取消为空 |
| `发货时间` | TEXT | 是 | 时间格式 CHECK |
| `订单状态` | TEXT | 否 | 已完成 / 已发货 / 已支付 / 待支付 / 已取消 / 已退款 / 退款中 |
| `渠道` | TEXT | 是 | 少量缺失 |
| `地区` | TEXT | 否 | |
| `商品金额` | REAL | 否 | `>= 0` |
| `运费` | REAL | 否 | `>= 0` |
| `优惠金额` | REAL | 是 | `>= 0`;少量缺失 |
| `订单金额` | REAL | 否 | `>= 0` |
| `支付金额` | REAL | 否 | `>= 0` |
| `退款金额` | REAL | 否 | `>= 0` |
| `是否退款` | TEXT | 否 | `是/否` CHECK |
| `新客` | TEXT | 否 | `是/否` CHECK |
| `是否测试单` | TEXT | 否 | `是/否` CHECK |
| `是否刷单` | TEXT | 否 | `是/否` CHECK |
| `活动ID` | TEXT | 是 | 大部分为空 |

### `order_items` 订单明细表(3786 行)

| 列名 | 类型 | 可空 | 键 / 约束 |
|---|---|---|---|
| `订单号` | TEXT | 否 | 复合主键之一,外键 → `orders.订单号` |
| `明细行号` | INTEGER | 否 | 复合主键之一,订单内从 1 开始 |
| `商品ID` | TEXT | 否 | 外键 → `products.商品ID` |
| `数量` | INTEGER | 否 | `>= 1` |
| `单价` | REAL | 否 | `>= 0` |
| `明细金额` | REAL | 否 | `>= 0` |
| `优惠分摊` | REAL | 否 | `>= 0` |
| `是否赠品` | TEXT | 否 | `是/否` CHECK |

主键为 `("订单号", "明细行号")`。CSV 中存在 29 处重复的 `(订单号, 商品ID)`
(同一商品分多行、含赠品行),因此不能用 `(订单号, 商品ID)` 做主键;
这与 `domains/ecommerce/schema.md` 的字段契约一致。

### `traffic` 流量表(450 行)

| 列名 | 类型 | 可空 | 键 / 约束 |
|---|---|---|---|
| `日期` | TEXT | 否 | 复合主键之一,日期格式 CHECK |
| `渠道` | TEXT | 否 | 复合主键之一 |
| `实验组` | TEXT | 否 | 复合主键之一;未分流 / 对照组 / 实验组 |
| `曝光人数` | INTEGER | 否 | `>= 0` |
| `访问人数` | INTEGER | 否 | `>= 0` |
| `加购人数` | INTEGER | 否 | `>= 0` |
| `下单人数` | INTEGER | 否 | `>= 0` |
| `支付人数` | INTEGER | 否 | `>= 0` |

### 索引

| 索引名 | 表 | 列 |
|---|---|---|
| `idx_orders_order_date` | `orders` | `订单日期` |
| `idx_orders_user_id` | `orders` | `用户ID` |
| `idx_orders_status` | `orders` | `订单状态` |
| `idx_order_items_product_id` | `order_items` | `商品ID` |
| `idx_traffic_date` | `traffic` | `日期` |

## 3. 视图口径

口径与 `domains/ecommerce/metrics.yaml` 保持一致。四个视图:

| 视图 | 粒度 | 返回列 |
|---|---|---|
| `v_daily_gmv` | 订单日期 | `订单日期`、`支付金额`(当日 GMV 合计) |
| `v_refund_rate` | 全量单行 | `支付订单数`、`退款订单数`、`订单口径退款率`、`支付金额`、`退款金额`、`金额口径退款率` |
| `v_repurchase` | 用户 | `用户ID`、`n`(干净支付订单数) |
| `v_order_detail` | 明细 | 订单 + 明细 + 商品字段 |

### `v_daily_gmv`

按 `订单日期` 汇总 `SUM(支付金额)`。过滤条件:

- `支付时间` 非空;
- `订单状态` 属于 `已支付 / 已发货 / 已完成 / 退款中 / 已退款`;
- `是否测试单 = 否`、`是否刷单 = 否`;
- 关联 `users` 并排除 `是否内部账号 = 是`、`是否测试账号 = 是` 的用户。

当前数据合计 `1193284.77` 元,覆盖 90 个日期。

### `v_refund_rate`

单行视图,分母只含发生过支付的干净订单(排除测试单、刷单,状态口径同上)。
同时给出两种口径:

- 订单口径退款率 = `退款订单数 / 支付订单数`;
- 金额口径退款率 = `SUM(退款金额) / SUM(支付金额)`(分母为 0 时返回 NULL)。

当前数据:支付订单 1690 单,退款订单 124 单,订单口径约 `0.0734`,
金额口径约 `0.0510`。

### `v_repurchase`

用户级视图,每行一个发生过干净支付订单的用户,`n` 为该用户的
`COUNT(DISTINCT 订单号)`(排除测试单、刷单、内部账号、测试账号,状态口径同上)。
复购率:

```sql
SELECT 1.0 * SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END) / COUNT(*) FROM v_repurchase;
```

当前数据结果为 `0.4583`(1006 个支付用户)。

### `v_order_detail`

`orders LEFT JOIN order_items LEFT JOIN products`,保持 3786 行明细。
明细单价与商品单价因重名分别别名为 `明细单价`、`商品单价`;
未支付订单没有明细时,明细与商品列为 NULL。

## 4. 示例 SQL

按日 GMV:

```sql
SELECT "订单日期", ROUND("支付金额", 2) AS "GMV"
FROM v_daily_gmv
ORDER BY "订单日期"
LIMIT 10;
```

按渠道退款率(同时给订单口径与金额口径):

```sql
SELECT
    COALESCE("渠道", '未知渠道') AS "渠道",
    COUNT(*) AS "支付订单数",
    ROUND(SUM(CASE WHEN "是否退款" = '是' THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4)
        AS "订单口径退款率",
    ROUND(SUM("退款金额") / NULLIF(SUM("支付金额"), 0), 4) AS "金额口径退款率"
FROM "orders"
WHERE "支付时间" IS NOT NULL
  AND "订单状态" IN ('已支付', '已发货', '已完成', '退款中', '已退款')
  AND "是否测试单" = '否'
  AND "是否刷单" = '否'
GROUP BY COALESCE("渠道", '未知渠道')
ORDER BY "支付订单数" DESC;
```

## 5. 注意事项

1. **中文列名在 SQL 里必须用双引号包裹**,例如 `"支付金额"`、`"订单日期"`。
   不加引号或只用反引号在 SQLite 中会报错或按别名解析。
2. 表名是英文,列名是中文;`SELECT *` 返回的列名同样是中文。
3. 源 CSV 只读,产物统一写到 `examples/ecommerce.db`;重新生成请加 `--force`。
4. `orders.支付时间` 为空表示未支付,不能当 0 元订单计入 GMV;
   订单数口径与 GMV 口径必须使用同一组过滤条件。
5. `users.手机号` 是脱敏假值,任何查询结果都不得输出完整号码。

## 6. MySQL 连接(主进程)

侧栏「数据库」分组支持添加 MySQL 只读连接。浏览器只提交主机/端口/库/账号/密码
与连接 id,不提交 SQL;SQL 由主进程按自然语言生成,经 `db.validate_select`
校验后执行,结果导出 CSV 再交给沙箱分析。

### 6.1 依赖

`requirements.txt` 已锁定 `pymysql==1.1.1`。这是**主进程依赖**,
只用于建立 MySQL 连接与读取结果;沙箱镜像不安装 pymysql、也不配置数据库网络,
沙箱仍然只消费导出的 CSV。

### 6.2 只读账号(第二道闸)

应用层已限制单条 SELECT / WITH 并禁用写关键字;仍强烈建议数据库侧只给
`SELECT` 权限。示例:

```sql
CREATE USER 'agent_ro'@'%' IDENTIFIED BY '请替换成随机强密码';
GRANT SELECT ON ecommerce.* TO 'agent_ro'@'%';
FLUSH PRIVILEGES;
```

为什么不直接用高权限账号:

- 应用层黑名单可能被新语法绕过,数据库权限是最后一道闸;
- 即使应用出 bug,也不会发生 `UPDATE` / `DELETE` / `DROP`;
- 审计时能明确区分「分析与只读账号」。

建连后适配器还会执行 `SET SESSION TRANSACTION READ ONLY`,让当前会话
即使账号有写权限也只能读。

### 6.3 字符集、时区与 SSL

- 连接固定 `charset="utf8mb4"`,中文列名与 emoji 不会乱码;
- 日期/时间列以 SQL 结果原样导出,建议库与会话时区统一为 `Asia/Shanghai`,
  避免 `DATE_FORMAT` 聚合跨时区偏移;
- 表单可勾选 SSL。生产环境建议启用并配置受信任 CA;当前实现为演示级
  TLS 连接(不校验主机名),不要把公网数据库裸连暴露在无 TLS 环境;
- 密码只保存在服务端 `outputs/db_credentials.json`(权限 600),
  `outputs/databases.jsonl`、日志、trace、answer、报告、SSE 均不含密码;
  接口只返回 `password_set`。

### 6.4 连接与查询限制

| 项目 | 限制 |
|---|---|
| 连接超时 | 5 秒 |
| 读超时 | 15 秒 |
| 单次查询执行上限 | 10 秒(`MAX_EXECUTION_TIME`,旧版 MySQL 自动忽略) |
| 单次返回行数 | 最多 10000 行,超出截断 |
| 允许语句 | 单条 `SELECT` / `WITH`,禁用写关键字 |
| 建连串行 | 服务端 `DB_LOCK`,同一时刻只处理一个真实建连 |

查询失败(密码错误 1045、连不上 2003、库不存在 1049、未装 pymysql)
都会返回中文原因并以 400 响应,不会出现 500。

### 6.5 管理接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/databases` | 连接列表(不含密码) |
| POST | `/api/databases` | 新建连接 |
| PUT | `/api/databases/{id}` | 更新连接;密码留空不修改 |
| POST | `/api/databases/test` | 测试连接,返回版本与耗时 |
| DELETE | `/api/databases/{id}` | 删除连接与密码 |
| GET | `/api/databases/{id}/tables` | 表/视图及列元数据 |
| GET | `/api/databases/{id}/tables/{name}` | 单表字段详情 |

### 6.6 提问链路

1. 主进程读取 schema(仅表名/列名/类型,不含数据);
2. 按方言生成 SQL(MySQL 使用反引号、`DATE_FORMAT`、`CONCAT`);
3. `db.validate_select` 校验单条只读查询;
4. 适配器执行,最多 10000 行,`Decimal`/`date`/`datetime`/`bytes` 统一转字符串;
5. 结果写 `data/processed/db_<label>_<ts>.csv`(utf-8-sig);
6. 以 `分析 <csv>：<原问题>` 交给 `run_agent`,复用现有分析、图表与报告链路;
7. 审计写 `outputs/db_queries.jsonl`(问题、SQL、行数、成功与否,不含密码)。

## 7. 存储引擎迁移(MyISAM → InnoDB)

`scripts/migrate_to_innodb.py` 用于把大表从 MyISAM 迁到 InnoDB。默认只打印计划,
加 `--apply` 才会备份并改表;脚本可重复执行,已是 InnoDB 时直接退出 0。

### 7.1 为什么迁

- MyISAM **没有数据页缓存**,每次全表聚合都要读磁盘;InnoDB 通过
  `innodb_buffer_pool_size` 缓存热数据,第二次查询通常从内存返回;
- MyISAM 是**表级锁**,一个长聚合会阻塞整张表的读写;
- InnoDB 支持事务、崩溃恢复与行级锁,后续加索引也不锁全表。

以 `bizdata.customer_purchase`(约 49 万行 / 69MB / 无索引)为例:迁移前
12 列聚合约 14 秒,迁移后第一次明显变快,第二次通常在 1 秒内。

### 7.2 备份

`--apply` 会自动调用 phpstudy 自带 mysqldump:

```text
D:\download_res\phpstudy_pro\Extensions\MySQL8.0.12\bin\mysqldump.exe
  -h {host} -P {port} -u {user}
  --databases {database} --tables {table}
  --lock-tables --default-character-set=utf8mb4
```

- 密码只通过环境变量 `MYSQL_PWD` 传给子进程,不出现在命令行、日志与审计里;
- 备份文件:`outputs/backups/{table}_{YYYYmmdd_HHMMSS}.sql`,写完后校验非空;
- 迁移期间新旧表共存,额外空间按表大小 ×1.2 估算;dry-run 会检查 datadir
  所在磁盘的剩余空间。

### 7.3 执行

先看计划(dry-run,不改任何东西):

```bash
python scripts/migrate_to_innodb.py --db-id 2f79f5f5 --table customer_purchase
```

输出包含当前引擎、精确行数、`Data_length+Index_length`、datadir 剩余空间、
预计耗时(3~5 MB/s)、额外空间需求与 FULLTEXT 索引提示。

确认后执行:

```bash
python scripts/migrate_to_innodb.py --db-id 2f79f5f5 --table customer_purchase --apply
```

脚本按顺序执行:备份 → `ALTER TABLE ... ENGINE=InnoDB, ALGORITHM=COPY`
(读写超时 600 秒,不设 `MAX_EXECUTION_TIME`)→ `ANALYZE TABLE` → 校验 →
性能对比(同一条 12 列聚合跑两次)→ 追加 `outputs/migrations.jsonl`。

任一步失败立即停止并打印回滚命令;迁移本身是原子的,失败时原表仍是 MyISAM。

### 7.4 回滚

脚本在已产生备份后失败,或在迁移前手工备份时,按提示执行:

```bash
mysql -h 127.0.0.1 -P 3306 -u root -p bizdata < outputs/backups/xxx.sql
```

备份用 `--databases` 导出,含 `CREATE DATABASE` 与 `USE`,可直接恢复整库。
恢复前建议先停掉正在使用该库的查询,恢复后重新执行 `ANALYZE TABLE`。

### 7.5 迁移后验证

```sql
SHOW TABLE STATUS FROM bizdata LIKE 'customer_purchase';
-- Engine 应为 InnoDB;Rows 为估算值,精确值用 COUNT(*)

SELECT COUNT(*) FROM customer_purchase;
SELECT ROUND(SUM(Age), 2), ROUND(SUM(AnnualIncome), 2) FROM customer_purchase;
```

在应用内对同一张表连问两次「分析一下这个数据」:第一次是真实查询,第二次命中
`outputs/db_cache/` 的文件缓存,通常 1 秒内返回;`outputs/db_queries.jsonl`
中第二次的 `"cached"` 为 `true`。

### 7.6 my.ini 建议

```ini
[mysqld]
default-storage-engine=InnoDB
innodb_buffer_pool_size=256M
```

- `innodb_buffer_pool_size` 建议为可用内存的 50%~70%;本机 phpstudy 默认值
  偏小,69MB 的表至少给到 256M 才能长期缓存整表;
- 改完需要重启 MySQL 服务;
- 修改引擎与缓冲池参数都属于运维操作,应用侧无需改动。

## 8. 查询缓存与治理

数据库问答的只读结果会缓存到 `outputs/db_cache/<连接 id>/`:

- 键为 `sha1(db_id + 归一化 SQL)`,仅空白 / 换行不同的同一查询共用缓存;
- 每条缓存是一对文件:`<sha1>.csv`(结果)与 `<sha1>.json`(sql / row_count / created_at);
- 只缓存 SELECT / SHOW / DESCRIBE / EXPLAIN,写语句永不缓存;
- TTL 默认 600 秒,`DB_CACHE_TTL_SECONDS` 可改;`DB_CACHE=0` 时读写全部关闭;
- 单条缓存超过 50MB 不落盘,避免大结果集占用磁盘。

命中缓存时,Web 结果卡的「执行的 SQL」旁会出现灰色徽章
「缓存 · 10 分钟内有效」,同时 `outputs/db_queries.jsonl` 中该次记录
`"cached": true`;纯 CSV 分析没有 SQL 区块,不会出现该徽章。

### 8.1 清理

```bash
python scripts/db_cache_admin.py --stats                          # 条数 / 总大小 / 按连接分组
python scripts/db_cache_admin.py --purge-expired                   # dry-run:只看将删除多少
python scripts/db_cache_admin.py --purge-expired --apply           # 真删过期条目
python scripts/db_cache_admin.py --clear --db-id 2f79f5f5 --apply  # 只清空指定连接
python scripts/db_cache_admin.py --clear --apply                   # 清空全部
```

- 默认 dry-run,输出「将删除 N 条 / 释放 X MB」;加 `--apply` 才真正删除;
- 总大小超过 `DB_CACHE_MAX_MB`(默认 200)时,purge 会按 `created_at`
  从旧到新删到限额内;
- `created_at` 无法解析的损坏条目按过期处理,便于一并清理。

