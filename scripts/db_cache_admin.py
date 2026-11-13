"""数据库查询缓存的查看与清理。

用法:
    python scripts/db_cache_admin.py --stats
    python scripts/db_cache_admin.py --purge-expired            # dry-run
    python scripts/db_cache_admin.py --purge-expired --apply    # 真删
    python scripts/db_cache_admin.py --clear --db-id 2f79f5f5 --apply
    python scripts/db_cache_admin.py --clear --apply            # 清空全部

默认 dry-run,只打印将删除的条数与可释放空间;加 --apply 才真正删除。
清理顺序:先删过期条目,再按 created_at 从旧到新删到 DB_CACHE_MAX_MB 以内。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db_cache

DEFAULT_MAX_MB = 200.0  # 缓存目录总量上限,可由 DB_CACHE_MAX_MB 覆盖

_DB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class CacheEntry:
    """一条缓存:一个 CSV 与其同名 JSON 元数据。"""

    db_id: str
    key: str
    created_at: datetime | None
    size_bytes: int


def _mb(size_bytes: float) -> str:
    """把字节数格式化为 MB 文本。

    Args:
        size_bytes: 字节数。

    Returns:
        形如 "0.42 MB" 的文本。
    """
    return f"{float(size_bytes) / (1024 * 1024):.2f} MB"


def _parse_created_at(value: object) -> datetime | None:
    """解析缓存元数据里的 created_at。

    Args:
        value: JSON 中的 created_at 值。

    Returns:
        带时区的 datetime;缺失或格式非法时返回 None。
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _max_bytes() -> int:
    """读取缓存总大小上限。

    Returns:
        上限字节数,默认 200MB;非法或负值回退默认值。
    """
    try:
        value = float(os.getenv("DB_CACHE_MAX_MB", str(DEFAULT_MAX_MB)))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_MB
    if value <= 0:
        value = DEFAULT_MAX_MB
    return int(value * 1024 * 1024)


def _scan() -> list[CacheEntry]:
    """扫描缓存目录,返回 CSV 与 JSON 成对的条目。

    Returns:
        全部缓存条目;目录不存在或没有缓存时返回 []。
    """
    root = db_cache.CACHE_DIR
    if not root.is_dir():
        return []
    entries: list[CacheEntry] = []
    for db_dir in sorted(root.iterdir()):
        if not db_dir.is_dir():
            continue
        keys = {path.stem for path in list(db_dir.glob("*.csv")) + list(db_dir.glob("*.json"))}
        for key in sorted(keys):
            csv_path = db_dir / f"{key}.csv"
            metadata_path = db_dir / f"{key}.json"
            size = 0
            for path in (csv_path, metadata_path):
                if path.is_file():
                    size += path.stat().st_size
            created_at = None
            if metadata_path.is_file():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    created_at = _parse_created_at(metadata.get("created_at"))
                except (OSError, ValueError):
                    created_at = None
            entries.append(CacheEntry(db_dir.name, key, created_at, size))
    return entries


def _is_expired(entry: CacheEntry, ttl_seconds: float, now: datetime) -> bool:
    """判断条目是否过期;created_at 无法解析时按过期处理。

    Args:
        entry: 缓存条目。
        ttl_seconds: TTL 秒数。
        now: 当前时间(带时区)。

    Returns:
        True 表示应清理。
    """
    if entry.created_at is None:
        return True
    return (now - entry.created_at).total_seconds() > ttl_seconds


def _delete(entry: CacheEntry) -> int:
    """删除一条缓存的两个文件。

    Args:
        entry: 缓存条目。

    Returns:
        实际释放的字节数。
    """
    db_dir = db_cache.CACHE_DIR / entry.db_id
    freed = 0
    for path in (db_dir / f"{entry.key}.csv", db_dir / f"{entry.key}.json"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as exc:
            print(f"警告: 删除 {path} 失败: {exc}")
            continue
        freed += size
    return freed


def _report(targets: list[CacheEntry], apply: bool, label: str) -> None:
    """按 dry-run / apply 输出并执行删除。

    Args:
        targets: 待删除条目。
        apply: True 时真正删除。
        label: 输出前缀(如「过期条目」)。
    """
    planned = sum(entry.size_bytes for entry in targets)
    if not apply:
        print(f"[dry-run] {label}: 将删除 {len(targets)} 条 / 释放 {_mb(planned)}")
        return
    deleted = 0
    freed = 0
    for entry in targets:
        freed += _delete(entry)
        deleted += 1
    print(f"{label}: 删除 {deleted} 条 / 释放 {_mb(freed)}")


def _remove_empty_dirs() -> None:
    """删除清理后变空的连接目录。"""
    root = db_cache.CACHE_DIR
    if not root.is_dir():
        return
    for db_dir in sorted(root.iterdir(), reverse=True):
        if db_dir.is_dir() and not any(db_dir.iterdir()):
            try:
                db_dir.rmdir()
            except OSError:
                pass


def _print_stats(entries: list[CacheEntry], ttl_seconds: float, max_bytes: int) -> None:
    """打印缓存统计。

    Args:
        entries: 全部缓存条目。
        ttl_seconds: 当前 TTL 秒数。
        max_bytes: 总量上限字节数。
    """
    total = sum(entry.size_bytes for entry in entries)
    print(f"缓存目录: {db_cache.CACHE_DIR}")
    print(f"条目数: {len(entries)}")
    print(f"总大小: {_mb(total)}")
    times = sorted(entry.created_at for entry in entries if entry.created_at)
    if times:
        print(f"最早 created_at: {times[0].isoformat()}")
        print(f"最晚 created_at: {times[-1].isoformat()}")
    else:
        print("最早/最晚 created_at: 无")
    print(f"TTL: {ttl_seconds:g} 秒")
    print("按连接:")
    grouped: dict[str, list[CacheEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.db_id, []).append(entry)
    for db_id in sorted(grouped):
        group = grouped[db_id]
        print(f"  - {db_id}: {len(group)} 条 / {_mb(sum(item.size_bytes for item in group))}")
    if total > max_bytes:
        print(
            f"警告: 总大小超过上限 {_mb(max_bytes)},"
            "执行 --purge-expired --apply 会按 created_at 从旧到新清理"
        )


def _purge(entries: list[CacheEntry], ttl_seconds: float, max_bytes: int, apply: bool) -> None:
    """删除过期条目,并在超限时按时间从旧到新清理。

    Args:
        entries: 扫描到的全部条目。
        ttl_seconds: TTL 秒数。
        max_bytes: 总量上限字节数。
        apply: True 时真正删除。
    """
    now = datetime.now(UTC)
    expired = [entry for entry in entries if _is_expired(entry, ttl_seconds, now)]
    _report(expired, apply, "过期条目")
    removed = {(entry.db_id, entry.key) for entry in expired}
    remaining = [entry for entry in entries if (entry.db_id, entry.key) not in removed]

    total = sum(entry.size_bytes for entry in remaining)
    if total <= max_bytes:
        if apply:
            _remove_empty_dirs()
        return
    remaining.sort(
        key=lambda entry: (entry.created_at or datetime.min.replace(tzinfo=UTC), entry.key)
    )
    overflow = total - max_bytes
    targets: list[CacheEntry] = []
    planned = 0
    for entry in remaining:
        if planned >= overflow:
            break
        targets.append(entry)
        planned += entry.size_bytes
    _report(targets, apply, "超限条目")
    if apply:
        _remove_empty_dirs()


def _clear(entries: list[CacheEntry], db_id: str | None, apply: bool) -> None:
    """清空指定连接或全部缓存。

    Args:
        entries: 扫描到的全部条目。
        db_id: 只清理该连接;None 表示全部。
        apply: True 时真正删除。
    """
    targets = [entry for entry in entries if db_id is None or entry.db_id == db_id]
    if not targets:
        print(f"连接 {db_id} 没有缓存条目" if db_id else "缓存目录为空")
        return
    _report(targets, apply, f"清空 {db_id}" if db_id else "清空全部")
    if apply:
        _remove_empty_dirs()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表;None 时取 sys.argv[1:]。

    Returns:
        含 stats / purge_expired / clear / db_id / apply 的命名空间。
    """
    parser = argparse.ArgumentParser(description="查看与清理数据库查询缓存")
    parser.add_argument("--stats", action="store_true", help="打印条数、大小与按连接分组")
    parser.add_argument("--purge-expired", action="store_true", help="删除超过 TTL 的条目")
    parser.add_argument("--clear", action="store_true", help="清空缓存(可配合 --db-id)")
    parser.add_argument("--db-id", default=None, help="只处理指定连接 id")
    parser.add_argument("--apply", action="store_true", help="真正删除;不加则 dry-run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """脚本入口。

    Args:
        argv: 参数列表;None 时取 sys.argv[1:]。

    Returns:
        0 = 正常;1 = 参数非法。
    """
    args = _parse_args(argv)
    if args.db_id and not _DB_ID_RE.fullmatch(args.db_id):
        print(f"错误: 非法连接 id: {args.db_id}")
        return 1

    entries = _scan()
    ttl_seconds = db_cache._cache_ttl_seconds()
    max_bytes = _max_bytes()

    if args.stats or not (args.purge_expired or args.clear):
        _print_stats(entries, ttl_seconds, max_bytes)
    if args.purge_expired:
        _purge(entries, ttl_seconds, max_bytes, args.apply)
    if args.clear:
        _clear(entries, args.db_id, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
