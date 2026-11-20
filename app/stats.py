"""轮次统计：按开始版本归组、10 分钟内成功计一次、观测窗口齐套判定、增量重算。"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime

from .models import NEW, OLD
from .timeutil import from_minute

SUCCESS_WINDOW_MIN = 10
WITHIN_MS = SUCCESS_WINDOW_MIN * 60  # 开始后 10 分钟内（含等于 10 分钟边界）


def _load_entries(conn: sqlite3.Connection, w_start: int, w_end: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT le.txn_id AS txn_id, le.ts AS ts, le.type AS type, le.minute AS minute "
        "FROM log_entries le "
        "JOIN minute_buckets mb ON mb.minute = le.minute "
        "WHERE le.minute >= ? AND le.minute < ? ORDER BY le.id",
        (w_start, w_end),
    ).fetchall()


def _received_minutes(conn: sqlite3.Connection, w_start: int, w_end: int) -> set[int]:
    rows = conn.execute(
        "SELECT minute FROM minute_buckets WHERE minute >= ? AND minute < ?",
        (w_start, w_end),
    ).fetchall()
    return {r["minute"] for r in rows}


def compute_round(conn: sqlite3.Connection, round_row: sqlite3.Row) -> dict:
    """计算单个轮次。返回可直接写入 round_stats 的字典（不含 round_id）。"""
    rid = round_row["round_id"]
    w_start = round_row["window_start_min"]
    w_end = round_row["window_end_min"]

    # 1) 观测窗口内每个整分钟都必须收齐（空包也算收齐）
    expected = set(range(w_start, w_end))
    received = _received_minutes(conn, w_start, w_end)
    missing = sorted(expected - received)
    if missing:
        first = from_minute(missing[0]).strftime("%Y-%m-%dT%H:%M")
        return {
            "status": "excluded",
            "reason": f"观测窗口日志未齐：缺 {len(missing)} 个整分钟（首个缺失 {first}）",
            "old_starts": 0, "old_successes": 0, "old_rate": None,
            "new_starts": 0, "new_successes": 0, "new_rate": None,
            "diff_pp": None,
        }

    seg_rows = conn.execute(
        "SELECT version, start_min, end_min FROM segments WHERE round_id = ?", (rid,)
    ).fetchall()
    segs = {r["version"]: (r["start_min"], r["end_min"]) for r in seg_rows}

    # 2) 收集开始事件：必须落在本版本时段内（整分钟桶属于该时段）
    starts: dict[str, tuple[datetime, str]] = {}  # txn_id -> (开始时间, 版本)
    dup_starts: set[str] = set()
    entries = _load_entries(conn, w_start, w_end)
    for row in entries:
        if row["type"] != "start":
            continue
        version = None
        for ver, (s, e) in segs.items():
            if s <= row["minute"] < e:
                version = ver
                break
        if version is None:
            # 窗口内但不在任一时段（末段结束后的 10 分钟尾巴）的开始：忽略
            continue
        if row["txn_id"] in starts:
            dup_starts.add(row["txn_id"])
            continue
        starts[row["txn_id"]] = (datetime.fromisoformat(row["ts"]), version)

    # 3) 成功事件：同一办理号只计一次，取最早一次成功
    first_success: dict[str, datetime] = {}
    for row in entries:
        if row["type"] != "success":
            continue
        ts = datetime.fromisoformat(row["ts"])
        prev = first_success.get(row["txn_id"])
        if prev is None or ts < prev:
            first_success[row["txn_id"]] = ts

    # 4) 按开始版本归组，开始后 10 分钟内成功算成功；每个办理号只计一次
    grouped: dict[str, set[str]] = {OLD: set(), NEW: set()}
    success_ids: dict[str, set[str]] = {OLD: set(), NEW: set()}
    for txn_id, (start_ts, version) in starts.items():
        grouped[version].add(txn_id)
        succ_ts = first_success.get(txn_id)
        if succ_ts is not None and 0 <= (succ_ts - start_ts).total_seconds() <= WITHIN_MS:
            success_ids[version].add(txn_id)

    old_n, old_s = len(grouped[OLD]), len(success_ids[OLD])
    new_n, new_s = len(grouped[NEW]), len(success_ids[NEW])

    # 5) 两版都有人开始办理才纳入
    if old_n == 0 or new_n == 0:
        empty_ver = "旧版" if old_n == 0 else "新版"
        return {
            "status": "excluded",
            "reason": f"{empty_ver}无人开始办理，无法比较两版成功率",
            "old_starts": old_n, "old_successes": old_s,
            "old_rate": (old_s / old_n) if old_n else None,
            "new_starts": new_n, "new_successes": new_s,
            "new_rate": (new_s / new_n) if new_n else None,
            "diff_pp": None,
        }

    old_rate = old_s / old_n
    new_rate = new_s / new_n
    note = None
    if dup_starts:
        note = f"注意：{len(dup_starts)} 个办理号存在重复开始记录，已按首次开始归组"
    return {
        "status": "included",
        "reason": note,
        "old_starts": old_n, "old_successes": old_s, "old_rate": old_rate,
        "new_starts": new_n, "new_successes": new_s, "new_rate": new_rate,
        "diff_pp": (new_rate - old_rate) * 100.0,
    }


UPSERT_SQL = """
INSERT INTO round_stats(
    round_id, status, reason, old_starts, old_successes, old_rate,
    new_starts, new_successes, new_rate, diff_pp, updated_at)
VALUES (:round_id, :status, :reason, :old_starts, :old_successes, :old_rate,
        :new_starts, :new_successes, :new_rate, :diff_pp, datetime('now'))
ON CONFLICT(round_id) DO UPDATE SET
    status=excluded.status, reason=excluded.reason,
    old_starts=excluded.old_starts, old_successes=excluded.old_successes, old_rate=excluded.old_rate,
    new_starts=excluded.new_starts, new_successes=excluded.new_successes, new_rate=excluded.new_rate,
    diff_pp=excluded.diff_pp, updated_at=datetime('now')
"""


def recompute_rounds(conn: sqlite3.Connection, round_rows: list[sqlite3.Row]) -> list[dict]:
    results = []
    for row in round_rows:
        stats = compute_round(conn, row)
        stats["round_id"] = row["round_id"]
        conn.execute(UPSERT_SQL, stats)
        results.append(stats)
    return results


def fetch_dashboard(conn: sqlite3.Connection) -> dict:
    rounds = conn.execute(
        """
        SELECT r.round_id, r.window_start_min, r.window_end_min,
               rs.status, rs.reason, rs.old_starts, rs.old_successes, rs.old_rate,
               rs.new_starts, rs.new_successes, rs.new_rate, rs.diff_pp, rs.updated_at
        FROM rounds r LEFT JOIN round_stats rs ON rs.round_id = r.round_id
        ORDER BY r.window_start_min, r.round_id
        """
    ).fetchall()
    items = []
    diffs = []
    for r in rounds:
        item = dict(r)
        item["window_start"] = from_minute(r["window_start_min"]).strftime("%Y-%m-%dT%H:%M")
        item["window_end"] = from_minute(r["window_end_min"]).strftime("%Y-%m-%dT%H:%M")
        if r["status"] is None:
            item["status"] = "pending"
            item["reason"] = "尚未计算"
        if r["diff_pp"] is not None:
            diffs.append(r["diff_pp"])
        items.append(item)
    avg = (sum(diffs) / len(diffs)) if diffs else None
    return {
        "rounds": items,
        "included_count": len(diffs),
        "excluded_count": sum(1 for i in items if i["status"] == "excluded"),
        "avg_diff_pp": avg,
    }
