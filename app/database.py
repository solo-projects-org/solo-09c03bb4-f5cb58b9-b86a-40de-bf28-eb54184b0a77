"""SQLite 访问层：建表、排班导入、分钟日志写入。所有写操作均在单事务内完成。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable

from .models import NEW, OLD, RoundIn, SegmentIn, VERSION_VALUES
from .timeutil import (
    fmt_minute,
    minute_floor,
    minutes_between,
    parse_ts,
    plus_minutes,
    to_minute,
)

DB_PATH = os.environ.get("APP_DB_PATH", "/data/app.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    round_id        TEXT PRIMARY KEY,
    window_start_min INTEGER NOT NULL,  -- 首段开头（epoch 分钟）
    window_end_min   INTEGER NOT NULL,  -- 末段结束 + 10 分钟（左闭右开，epoch 分钟）
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segments (
    round_id   TEXT NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    version    TEXT NOT NULL CHECK (version IN ('old','new')),
    start_min  INTEGER NOT NULL,  -- 含
    end_min    INTEGER NOT NULL,  -- 不含
    PRIMARY KEY (round_id, version)
);

CREATE TABLE IF NOT EXISTS minute_buckets (
    minute       INTEGER PRIMARY KEY,  -- epoch 分钟
    received_at  TEXT NOT NULL DEFAULT (datetime('now')),
    entry_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS log_entries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    minute   INTEGER NOT NULL REFERENCES minute_buckets(minute) ON DELETE CASCADE,
    txn_id   TEXT NOT NULL,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL CHECK (type IN ('start','success'))
);

CREATE INDEX IF NOT EXISTS idx_log_txn ON log_entries(txn_id);
CREATE INDEX IF NOT EXISTS idx_log_minute ON log_entries(minute);

CREATE TABLE IF NOT EXISTS round_stats (
    round_id          TEXT PRIMARY KEY REFERENCES rounds(round_id) ON DELETE CASCADE,
    status            TEXT NOT NULL CHECK (status IN ('included','excluded')),
    reason            TEXT,
    old_starts        INTEGER NOT NULL DEFAULT 0,
    old_successes     INTEGER NOT NULL DEFAULT 0,
    old_rate          REAL,
    new_starts        INTEGER NOT NULL DEFAULT 0,
    new_successes     INTEGER NOT NULL DEFAULT 0,
    new_rate          REAL,
    diff_pp           REAL,  -- 新版减旧版，百分点
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


class ValidationError(Exception):
    """输入数据不合法（对应 HTTP 400/409）。"""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _validate_round(round_in: RoundIn) -> list[tuple[str, int, int]]:
    if len(round_in.segments) != 2:
        raise ValidationError(f"轮次 {round_in.round_id} 必须恰好包含两个时段")
    versions = {s.version for s in round_in.segments}
    if set(VERSION_VALUES) - versions or len(versions) != 2:
        raise ValidationError(f"轮次 {round_in.round_id} 两个时段必须分别为 old 与 new")
    parsed: list[tuple[str, int, int]] = []
    for seg in round_in.segments:
        start = minute_floor(parse_ts(seg.start))
        end = minute_floor(parse_ts(seg.end))
        if fmt_minute(start) != seg.start or fmt_minute(end) != seg.end:
            raise ValidationError(
                f"轮次 {round_in.round_id} 时段边界必须是整分钟: {seg.start} ~ {seg.end}"
            )
        if end <= start:
            raise ValidationError(f"轮次 {round_in.round_id} 时段结束必须晚于开始")
        parsed.append((seg.version, to_minute(start), to_minute(end)))
    # 相邻：一个的 end == 另一个的 start
    ordered = sorted(parsed, key=lambda x: x[1])
    if ordered[0][2] != ordered[1][1]:
        raise ValidationError(f"轮次 {round_in.round_id} 两个时段必须相邻（首尾相接、无重叠无空隙）")
    if ordered[0][1] == ordered[1][1] or ordered[0][2] > ordered[1][1]:
        raise ValidationError(f"轮次 {round_in.round_id} 时段互不相邻或重叠")
    return ordered


def insert_schedule(conn: sqlite3.Connection, rounds: Iterable[RoundIn]) -> list[str]:
    rounds = list(rounds)
    if not rounds:
        raise ValidationError("排班为空")
    existing = conn.execute("SELECT 1 FROM rounds LIMIT 1").fetchone()
    if existing:
        raise ValidationError("排班已存在：固定排班只允许导入一次；如需重导请先重置数据", code=409)
    seen_ids: set[str] = set()
    all_intervals: list[tuple[int, int, str]] = []
    normalized: list[tuple[str, list[tuple[str, int, int]]]] = []
    for round_in in rounds:
        if round_in.round_id in seen_ids:
            raise ValidationError(f"轮次 id 重复: {round_in.round_id}")
        seen_ids.add(round_in.round_id)
        ordered = _validate_round(round_in)
        window_start = ordered[0][1]
        window_end = ordered[-1][2] + 10  # 末段结束后十分钟，左闭右开
        normalized.append((round_in.round_id, ordered))
        all_intervals.append((window_start, window_end, round_in.round_id))
    # 各轮排班互不重叠（观测窗口可以重叠吗？要求只说排班互不重叠；
    # 观测窗口是排班的延伸，相邻轮次间隔不足 10 分钟时窗口可能交叠——
    # 这里校验的是排班本身：用时段区间）
    seg_intervals: list[tuple[int, int, str]] = []
    for rid, ordered in normalized:
        for version, s, e in ordered:
            seg_intervals.append((s, e, rid))
    seg_intervals.sort()
    for (s1, e1, r1), (s2, e2, r2) in zip(seg_intervals, seg_intervals[1:]):
        if s2 < e1:
            raise ValidationError(f"轮次 {r1} 与 {r2} 的排班时段重叠")
    for rid, ordered in normalized:
        window_start = ordered[0][1]
        window_end = ordered[-1][2] + 10
        conn.execute(
            "INSERT INTO rounds(round_id, window_start_min, window_end_min) VALUES (?,?,?)",
            (rid, window_start, window_end),
        )
        for version, s, e in ordered:
            conn.execute(
                "INSERT INTO segments(round_id, version, start_min, end_min) VALUES (?,?,?,?)",
                (rid, version, s, e),
            )
    return [rid for rid, _ in normalized]


def validate_log_entries(entries) -> list[tuple[str, datetime, str, int]]:
    """校验并解析一个分钟包。返回 (txn_id, dt, type, minute) 列表。"""
    parsed: list[tuple[str, datetime, str, int]] = []
    start_ids: set[str] = set()
    for e in entries:
        dt = parse_ts(e.ts)
        parsed.append((e.txn_id, dt, e.type, to_minute(minute_floor(dt))))
        if e.type == "start":
            if e.txn_id in start_ids:
                raise ValidationError(f"同一包内办理号 {e.txn_id} 出现多次开始")
            start_ids.add(e.txn_id)
    return parsed


def replace_minute(conn: sqlite3.Connection, minute: int, entries) -> int:
    """整分钟补传替换：删旧包写新包，全部在调用方事务内。"""
    conn.execute("DELETE FROM minute_buckets WHERE minute = ?", (minute,))
    conn.execute(
        "INSERT INTO minute_buckets(minute, entry_count) VALUES (?, ?)",
        (minute, len(entries)),
    )
    conn.executemany(
        "INSERT INTO log_entries(minute, txn_id, ts, type) VALUES (?,?,?,?)",
        [(minute, txn_id, dt.isoformat(), typ) for txn_id, dt, typ, _ in entries],
    )
    return len(entries)


def rounds_covering_minute(conn: sqlite3.Connection, minute: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rounds WHERE window_start_min <= ? AND ? < window_end_min "
        "ORDER BY window_start_min, round_id",
        (minute, minute),
    ).fetchall()


def all_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rounds ORDER BY window_start_min, round_id"
    ).fetchall()


def get_segment_map(conn: sqlite3.Connection, round_id: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM segments WHERE round_id = ?", (round_id,)
    ).fetchall()
    return {r["version"]: r for r in rows}
