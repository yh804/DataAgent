"""把电商领域包的 CSV 装载成 SQLite 演示库,供「连接数据库」功能直接选用。

数据源为 domains/ecommerce/examples/ 下的五张 CSV(utf-8-sig 带 BOM),
目标库默认 examples/ecommerce.db。表名用英文,列名保持 CSV 中的中文,
以便与 domains/ecommerce/metrics.yaml 的口径(如 orders.支付金额)对齐。

用法:
    python scripts/make_demo_db.py --out examples/ecommerce.db
    python scripts/make_demo_db.py --force
    python scripts/make_demo_db.py --examples-dir domains/ecommerce/examples --out examples/ecommerce.db

安全与幂等:
    1. 先在目标目录写临时文件,成功后再原子替换,失败只删除临时文件,
       已存在的目标库不会被半成品覆盖;
    2. 同一份 CSV 重复执行,行数与内容校验和一致;
    3. 源 CSV 只读,不修改、不移动。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXAMPLES_DIR = PROJECT_ROOT / "domains" / "ecommerce" / "examples"
DEFAULT_OUT = PROJECT_ROOT / "examples" / "ecommerce.db"

# 装载顺序必须满足外键依赖:先字典表 users / products,再 orders,最后明细与流量
TABLE_ORDER = ("users", "products", "orders", "order_items", "traffic")

# 与 metrics.yaml 一致的口径:发生过支付的订单状态
PAID_STATUSES = ("已支付", "已发货", "已完成", "退款中", "已退款")

# 日期 / 时间格式校验;SQLite 没有原生日期类型,统一按 ISO8601 文本存储
DATE_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
DATETIME_GLOB = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'"
YES_NO_CHECK = "IN ('是', '否')"

# 各表在 CSV / 数据库中的中文列,用于「只保留 CSV 里实际存在的列」
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": (
        "用户ID", "注册日期", "注册渠道", "地区", "会员等级", "性别", "年龄", "手机号",
        "是否内部账号", "是否测试账号",
    ),
    "products": (
        "商品ID", "商品名称", "品类", "子品类", "品牌", "单价", "成本价", "上架日期", "是否在售",
    ),
    "orders": (
        "订单号", "用户ID", "订单日期", "支付时间", "发货时间", "订单状态", "渠道", "地区",
        "商品金额", "运费", "优惠金额", "订单金额", "支付金额", "退款金额", "是否退款",
        "新客", "是否测试单", "是否刷单", "活动ID",
    ),
    "order_items": (
        "订单号", "明细行号", "商品ID", "数量", "单价", "明细金额", "优惠分摊", "是否赠品",
    ),
    "traffic": (
        "日期", "渠道", "实验组", "曝光人数", "访问人数", "加购人数", "下单人数", "支付人数",
    ),
}

# CSV 中以文本读入、需要再转成数值的列
REAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": (),
    "products": ("单价", "成本价"),
    "orders": ("商品金额", "运费", "优惠金额", "订单金额", "支付金额", "退款金额"),
    "order_items": ("单价", "明细金额", "优惠分摊"),
    "traffic": (),
}
INTEGER_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("年龄",),
    "products": (),
    "orders": (),
    "order_items": ("明细行号", "数量"),
    "traffic": ("曝光人数", "访问人数", "加购人数", "下单人数", "支付人数"),
}


# 建表 + 索引 + 视图。中文标识符在 SQL 里全部用双引号包裹。
SCHEMA_SQL = f"""
CREATE TABLE "users" (
    "用户ID" TEXT NOT NULL PRIMARY KEY,
    "注册日期" TEXT NOT NULL CHECK ("注册日期" GLOB {DATE_GLOB}),
    "注册渠道" TEXT NOT NULL,
    "地区" TEXT NOT NULL,
    "会员等级" TEXT NOT NULL,
    "性别" TEXT NOT NULL,
    "年龄" INTEGER CHECK ("年龄" IS NULL OR "年龄" >= 0),
    "手机号" TEXT NOT NULL,
    "是否内部账号" TEXT NOT NULL CHECK ("是否内部账号" {YES_NO_CHECK}),
    "是否测试账号" TEXT NOT NULL CHECK ("是否测试账号" {YES_NO_CHECK})
);

CREATE TABLE "products" (
    "商品ID" TEXT NOT NULL PRIMARY KEY,
    "商品名称" TEXT NOT NULL,
    "品类" TEXT NOT NULL,
    "子品类" TEXT NOT NULL,
    "品牌" TEXT NOT NULL,
    "单价" REAL NOT NULL CHECK ("单价" >= 0),
    "成本价" REAL NOT NULL CHECK ("成本价" >= 0),
    "上架日期" TEXT NOT NULL CHECK ("上架日期" GLOB {DATE_GLOB}),
    "是否在售" TEXT NOT NULL CHECK ("是否在售" {YES_NO_CHECK})
);

CREATE TABLE "orders" (
    "订单号" TEXT NOT NULL PRIMARY KEY,
    "用户ID" TEXT NOT NULL,
    "订单日期" TEXT NOT NULL CHECK ("订单日期" GLOB {DATE_GLOB}),
    "支付时间" TEXT CHECK ("支付时间" IS NULL OR "支付时间" GLOB {DATETIME_GLOB}),
    "发货时间" TEXT CHECK ("发货时间" IS NULL OR "发货时间" GLOB {DATETIME_GLOB}),
    "订单状态" TEXT NOT NULL,
    "渠道" TEXT,
    "地区" TEXT NOT NULL,
    "商品金额" REAL NOT NULL CHECK ("商品金额" >= 0),
    "运费" REAL NOT NULL CHECK ("运费" >= 0),
    "优惠金额" REAL CHECK ("优惠金额" IS NULL OR "优惠金额" >= 0),
    "订单金额" REAL NOT NULL CHECK ("订单金额" >= 0),
    "支付金额" REAL NOT NULL CHECK ("支付金额" >= 0),
    "退款金额" REAL NOT NULL CHECK ("退款金额" >= 0),
    "是否退款" TEXT NOT NULL CHECK ("是否退款" {YES_NO_CHECK}),
    "新客" TEXT NOT NULL CHECK ("新客" {YES_NO_CHECK}),
    "是否测试单" TEXT NOT NULL CHECK ("是否测试单" {YES_NO_CHECK}),
    "是否刷单" TEXT NOT NULL CHECK ("是否刷单" {YES_NO_CHECK}),
    "活动ID" TEXT,
    FOREIGN KEY ("用户ID") REFERENCES "users" ("用户ID")
);

CREATE TABLE "order_items" (
    "订单号" TEXT NOT NULL,
    "明细行号" INTEGER NOT NULL,
    "商品ID" TEXT NOT NULL,
    "数量" INTEGER NOT NULL CHECK ("数量" >= 1),
    "单价" REAL NOT NULL CHECK ("单价" >= 0),
    "明细金额" REAL NOT NULL CHECK ("明细金额" >= 0),
    "优惠分摊" REAL NOT NULL CHECK ("优惠分摊" >= 0),
    "是否赠品" TEXT NOT NULL CHECK ("是否赠品" {YES_NO_CHECK}),
    PRIMARY KEY ("订单号", "明细行号"),
    FOREIGN KEY ("订单号") REFERENCES "orders" ("订单号"),
    FOREIGN KEY ("商品ID") REFERENCES "products" ("商品ID")
);

CREATE TABLE "traffic" (
    "日期" TEXT NOT NULL CHECK ("日期" GLOB {DATE_GLOB}),
    "渠道" TEXT NOT NULL,
    "实验组" TEXT NOT NULL,
    "曝光人数" INTEGER NOT NULL CHECK ("曝光人数" >= 0),
    "访问人数" INTEGER NOT NULL CHECK ("访问人数" >= 0),
    "加购人数" INTEGER NOT NULL CHECK ("加购人数" >= 0),
    "下单人数" INTEGER NOT NULL CHECK ("下单人数" >= 0),
    "支付人数" INTEGER NOT NULL CHECK ("支付人数" >= 0),
    PRIMARY KEY ("日期", "渠道", "实验组")
);

CREATE INDEX "idx_orders_order_date" ON "orders" ("订单日期");
CREATE INDEX "idx_orders_user_id" ON "orders" ("用户ID");
CREATE INDEX "idx_orders_status" ON "orders" ("订单状态");
CREATE INDEX "idx_order_items_product_id" ON "order_items" ("商品ID");
CREATE INDEX "idx_traffic_date" ON "traffic" ("日期");

CREATE VIEW "v_daily_gmv" AS
SELECT
    o."订单日期" AS "订单日期",
    SUM(o."支付金额") AS "支付金额"
FROM "orders" AS o
JOIN "users" AS u ON o."用户ID" = u."用户ID"
WHERE o."支付时间" IS NOT NULL
  AND o."订单状态" IN ('已支付', '已发货', '已完成', '退款中', '已退款')
  AND o."是否测试单" = '否'
  AND o."是否刷单" = '否'
  AND u."是否内部账号" = '否'
  AND u."是否测试账号" = '否'
GROUP BY o."订单日期";

CREATE VIEW "v_refund_rate" AS
SELECT
    COUNT(*) AS "支付订单数",
    SUM(CASE WHEN o."是否退款" = '是' THEN 1 ELSE 0 END) AS "退款订单数",
    SUM(CASE WHEN o."是否退款" = '是' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS "订单口径退款率",
    SUM(o."支付金额") AS "支付金额",
    SUM(o."退款金额") AS "退款金额",
    CASE
        WHEN SUM(o."支付金额") = 0 THEN NULL
        ELSE SUM(o."退款金额") / SUM(o."支付金额")
    END AS "金额口径退款率"
FROM "orders" AS o
WHERE o."支付时间" IS NOT NULL
  AND o."订单状态" IN ('已支付', '已发货', '已完成', '退款中', '已退款')
  AND o."是否测试单" = '否'
  AND o."是否刷单" = '否';

CREATE VIEW "v_repurchase" AS
SELECT
    o."用户ID" AS "用户ID",
    COUNT(DISTINCT o."订单号") AS n
FROM "orders" AS o
JOIN "users" AS u ON o."用户ID" = u."用户ID"
WHERE o."支付时间" IS NOT NULL
  AND o."订单状态" IN ('已支付', '已发货', '已完成', '退款中', '已退款')
  AND o."是否测试单" = '否'
  AND o."是否刷单" = '否'
  AND u."是否内部账号" = '否'
  AND u."是否测试账号" = '否'
GROUP BY o."用户ID";

CREATE VIEW "v_order_detail" AS
SELECT
    o."订单号" AS "订单号",
    o."用户ID" AS "用户ID",
    o."订单日期" AS "订单日期",
    o."支付时间" AS "支付时间",
    o."发货时间" AS "发货时间",
    o."订单状态" AS "订单状态",
    o."渠道" AS "渠道",
    o."地区" AS "地区",
    o."商品金额" AS "商品金额",
    o."运费" AS "运费",
    o."优惠金额" AS "优惠金额",
    o."订单金额" AS "订单金额",
    o."支付金额" AS "支付金额",
    o."退款金额" AS "退款金额",
    o."是否退款" AS "是否退款",
    o."新客" AS "新客",
    o."是否测试单" AS "是否测试单",
    o."是否刷单" AS "是否刷单",
    o."活动ID" AS "活动ID",
    i."明细行号" AS "明细行号",
    i."商品ID" AS "商品ID",
    i."数量" AS "数量",
    i."单价" AS "明细单价",
    i."明细金额" AS "明细金额",
    i."优惠分摊" AS "优惠分摊",
    i."是否赠品" AS "是否赠品",
    p."商品名称" AS "商品名称",
    p."品类" AS "品类",
    p."子品类" AS "子品类",
    p."品牌" AS "品牌",
    p."单价" AS "商品单价",
    p."成本价" AS "成本价",
    p."上架日期" AS "上架日期",
    p."是否在售" AS "是否在售"
FROM "orders" AS o
LEFT JOIN "order_items" AS i ON o."订单号" = i."订单号"
LEFT JOIN "products" AS p ON i."商品ID" = p."商品ID";
"""


def _load_frame(examples_dir: Path, table: str) -> pd.DataFrame:
    """读取单张 CSV,只保留表结构中声明过的列,并把数值列转成对应类型。

    Args:
        examples_dir: CSV 所在目录。
        table: 表名(即 CSV 文件名,不含扩展名)。

    Returns:
        列顺序与 TABLE_COLUMNS 一致的 DataFrame。
    """
    path = examples_dir / f"{table}.csv"
    if not path.is_file():
        raise FileNotFoundError(f"缺少数据文件: {path}")

    frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    keep = [column for column in TABLE_COLUMNS[table] if column in frame.columns]
    if not keep:
        raise ValueError(f"{path.name} 没有任何预期列")
    frame = frame.loc[:, keep].copy()

    for column in REAL_COLUMNS[table]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in INTEGER_COLUMNS[table]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
    return frame


def _to_sql_value(value: object) -> object:
    """把 pandas / numpy 标量转成 sqlite3 可绑定的原生 Python 值,缺失值转 None。"""
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _insert_frame(conn: sqlite3.Connection, table: str, frame: pd.DataFrame) -> int:
    """用 executemany 把整张表写入数据库,返回插入行数。"""
    columns = list(frame.columns)
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join(f'"{column}"' for column in columns)
    statement = f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'
    rows = [tuple(_to_sql_value(value) for value in row) for row in frame.itertuples(index=False, name=None)]
    conn.executemany(statement, rows)
    return len(rows)


def _content_checksum(conn: sqlite3.Connection) -> str:
    """按固定顺序遍历五张表,计算与文件布局无关的内容校验和。

    为什么不用文件 sha256:SQLite 页布局可能随工具链版本变化,
    按行内容哈希更能证明「同一份 CSV 装载结果一致」。
    """
    digest = hashlib.sha256()
    for table in TABLE_ORDER:
        digest.update(f"[{table}]".encode("utf-8"))
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY 1, 2').fetchall()
        for row in rows:
            digest.update(repr(row).encode("utf-8"))
    return digest.hexdigest()


def build_database(examples_dir: Path, out: Path, force: bool = False) -> dict[str, object]:
    """把 CSV 装载成 SQLite 库并返回统计信息。

    Args:
        examples_dir: CSV 目录。
        out: 目标库路径。
        force: 目标已存在时是否覆盖;False 且存在则抛 FileExistsError。

    Returns:
        包含 tables / indexes / views / size_bytes / checksum 的统计字典。

    Raises:
        FileExistsError: 目标已存在且 force=False。
        FileNotFoundError: 缺少 CSV 文件。
        sqlite3.Error: 建表或写入失败。
    """
    if out.exists() and not force:
        raise FileExistsError(f"目标库已存在: {out}(如需覆盖请加 --force)")

    out.parent.mkdir(parents=True, exist_ok=True)
    # 先写同目录临时文件,成功后原子替换,避免失败时留下半成品或破坏旧库
    handle, temp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    os.close(handle)
    temp_path = Path(temp_name)

    try:
        conn = sqlite3.connect(temp_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(SCHEMA_SQL)

            inserted: dict[str, int] = {}
            for table in TABLE_ORDER:
                frame = _load_frame(examples_dir, table)
                inserted[table] = _insert_frame(conn, table, frame)
            conn.commit()

            indexes = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            views = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'view'"
            ).fetchone()[0]
            checksum = _content_checksum(conn)
        finally:
            conn.close()

        os.replace(temp_path, out)
    except BaseException:
        if temp_path.exists():
            temp_path.unlink()
        raise

    return {
        "tables": inserted,
        "indexes": indexes,
        "views": views,
        "size_bytes": out.stat().st_size,
        "checksum": checksum,
    }


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并执行装载,打印结果摘要。

    Args:
        argv: 命令行参数列表;None 表示使用 sys.argv。

    Returns:
        进程退出码:0 成功,2 目标已存在且未加 --force,1 其他失败。
    """
    parser = argparse.ArgumentParser(description="把电商领域包 CSV 装载成 SQLite 演示库")
    parser.add_argument(
        "--examples-dir",
        type=Path,
        default=DEFAULT_EXAMPLES_DIR,
        help="CSV 目录,默认 domains/ecommerce/examples",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="输出库路径,默认 examples/ecommerce.db",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="目标已存在时覆盖;不加且文件存在则报错退出",
    )
    args = parser.parse_args(argv)

    try:
        stats = build_database(args.examples_dir, args.out, force=args.force)
    except FileExistsError as exc:
        print(f"[make_demo_db] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 命令行入口需要把失败原因原样报出
        print(f"[make_demo_db] 装载失败: {exc}")
        return 1

    print(f"[make_demo_db] 输出: {args.out}")
    for table in TABLE_ORDER:
        print(f"[make_demo_db] 表 {table}: {stats['tables'][table]} 行")
    print(f"[make_demo_db] 索引: {stats['indexes']} 个")
    print(f"[make_demo_db] 视图: {stats['views']} 个")
    print(f"[make_demo_db] 库大小: {stats['size_bytes'] / (1024 * 1024):.2f} MB")
    print(f"[make_demo_db] 内容校验和: {stats['checksum']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
