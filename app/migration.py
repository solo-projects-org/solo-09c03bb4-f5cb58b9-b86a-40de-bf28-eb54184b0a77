"""历史轮次迁入：一轮原格式排班 + 观测窗口内原格式分钟包，预览/确认两阶段。

规则口径：
- 库中没有的分钟包补入；已有的包照用（不覆盖、不修改）。
- 两边都有同一分钟：事件内容与重复条数必须一致（顺序不限），不一致即冲突，拒绝迁入。
- 沿用排班校验（整分钟、相邻、版本各一、轮次号唯一、排班时段互不重叠）与
  办理开始全局唯一校验，冲突全部收集后一次性报出。
- 预览列出：待补分钟、共享分钟、新轮结果、受补入影响的已有轮次迁入前后纳入状态与成功率差。
- 确认时按当前库重新核对；补入清单、受影响轮次或预览结果有变即拒绝并要求重新预览；
  无关补传不改变计划时不影响确认。
- 排班、日志、统计在同一个 SQLite 事务内生效，任何失败整体回滚。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections import Counter
from datetime import datetime

from . import database as db
from .models import MigrationIn
from .stats import compute_round, recompute_rounds
from .timeutil import fmt_minute, from_minute, minute_floor, parse_ts, to_minute

# round_stats 中参与对比/展示的字段（updated_at、round_id 除外）
STAT_FIELDS = (
    "status", "reason", "old_starts", "old_successes", "old_rate",
    "new_starts", "new_successes", "new_rate", "diff_pp",
)


class MigrationError(Exception):
    """迁入失败。code：400 结构性错误 / 409 与现库冲突或计划过期。"""

    def __init__(self, message: str, code: int = 400, conflicts: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.conflicts = conflicts or []


def _canonical_entry(txn_id: str, ts: str, typ: str) -> dict:
    # 库内 ts 以 dt.isoformat() 落库，这里统一规范化，保证“同内容不同写法”能对上
    return {"txn_id": txn_id, "ts": parse_ts(ts).isoformat(), "type": typ}


def _entry_key(entry: dict) -> tuple[str, str, str]:
    return entry["txn_id"], entry["ts"], entry["type"]


def _structural_parse(payload: MigrationIn) -> tuple[list, dict[int, list[dict]], int, int]:
    """解析排班与分钟包的结构性问题（400），返回 (ordered_segments, 分钟->规范化条目, 窗口)。"""
    try:
        ordered = db._validate_round(payload.round)  # 整分钟/相邻/版本校验，失败为 400
    except db.ValidationError as exc:
        raise MigrationError(str(exc), code=400)
    window_start = ordered[0][1]
    window_end = ordered[-1][2] + 10

    packets: dict[int, list[dict]] = {}
    for pkt in payload.minutes:
        try:
            dt = parse_ts(pkt.minute)
        except ValueError as exc:
            raise MigrationError(f"分钟时间格式错误：{exc}", code=400)
        if fmt_minute(minute_floor(dt)) != pkt.minute:
            raise MigrationError(f"minute 必须是整分钟 YYYY-MM-DDTHH:MM：{pkt.minute}", code=400)
        m = to_minute(dt)
        if m in packets:
            raise MigrationError(f"迁入文件中分钟 {pkt.minute} 出现重复包", code=400)
        if not (window_start <= m < window_end):
            raise MigrationError(
                f"分钟 {pkt.minute} 不在轮次 {payload.round.round_id} 的观测窗口 "
                f"{fmt_minute(from_minute(window_start))} ~ "
                f"{fmt_minute(from_minute(window_end))}（左闭右开）内",
                code=400,
            )
        entries: list[dict] = []
        for e in pkt.entries:
            try:
                edt = parse_ts(e.ts)
            except ValueError as exc:
                raise MigrationError(f"日志时间格式错误：{exc}", code=400)
            if to_minute(minute_floor(edt)) != m:
                raise MigrationError(
                    f"日志时间 {e.ts} 不属于分钟包 {pkt.minute}", code=400
                )
            entries.append(_canonical_entry(e.txn_id, e.ts, e.type))
        packets[m] = entries
    return ordered, packets, window_start, window_end


def _payload_canonical(payload: MigrationIn, packets: dict[int, list[dict]]) -> dict:
    return {
        "round": payload.round.model_dump(),
        "minutes": [
            {"minute": fmt_minute(from_minute(m)),
             "entries": sorted(packets[m], key=_entry_key)}
            for m in sorted(packets)
        ],
    }


def _snapshot_stats(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        stats = compute_round(conn, row)
        out[row["round_id"]] = {k: stats[k] for k in STAT_FIELDS}
    return out


def _apply(conn: sqlite3.Connection, payload: MigrationIn) -> dict:
    """在调用方已开启的事务内执行迁入写入并返回计划；调用方自行提交或回滚。"""
    ordered, packets, window_start, window_end = _structural_parse(payload)
    rid = payload.round.round_id
    conflicts: list[dict] = []

    # ---- 1) 轮次号唯一 + 排班时段互不重叠（沿用排班校验，报冲突）----
    if conn.execute("SELECT 1 FROM rounds WHERE round_id = ?", (rid,)).fetchone():
        conflicts.append({"type": "duplicate_round", "round_id": rid,
                          "message": f"轮次号 {rid} 已存在，轮次号必须唯一"})
    seg_intervals = db._seg_intervals([(rid, ordered)])
    for s, e, _ in seg_intervals:
        hit = conn.execute(
            "SELECT round_id FROM segments WHERE start_min < ? AND ? < end_min LIMIT 1",
            (e, s),
        ).fetchone()
        if hit:
            conflicts.append({
                "type": "schedule_overlap",
                "round_id": rid, "other_round_id": hit["round_id"],
                "message": f"轮次 {rid} 与已存在轮次 {hit['round_id']} 的排班时段重叠",
            })
            break
    # 新轮内部时段已由 _validate_round 保证不重叠

    # ---- 2) 分钟包分类：补入 / 共享，共享者逐条比对（顺序不限、重复条数须一致）----
    to_insert: dict[int, list[dict]] = {}
    shared: list[dict] = []
    for m in sorted(packets):
        incoming = sorted(packets[m], key=lambda e: (e["txn_id"], e["ts"], e["type"]))
        existing = db.get_bucket(conn, m)
        if existing is None:
            to_insert[m] = packets[m]
            continue
        existing_canon = sorted(
            (_canonical_entry(t, ts, ty) for t, ts, ty in existing),
            key=lambda e: (e["txn_id"], e["ts"], e["type"]),
        )
        if Counter(map(_entry_key, incoming)) != Counter(map(_entry_key, existing_canon)):
            conflicts.append({
                "type": "minute_conflict",
                "minute": fmt_minute(from_minute(m)),
                "message": (
                    f"分钟 {fmt_minute(from_minute(m))} 库中已有包，事件内容或重复条数不一致"
                    f"（库内 {len(existing_canon)} 条，迁入 {len(incoming)} 条），顺序不限"
                ),
                "incoming": incoming,
                "existing": existing_canon,
            })
        else:
            shared.append({"minute": fmt_minute(from_minute(m)),
                           "entry_count": len(incoming)})

    # ---- 3) 办理开始全局唯一（文件内 + 与现库，共享分钟除外）----
    file_starts: dict[str, int] = {}
    for m, entries in packets.items():
        for e in entries:
            if e["type"] == "start":
                file_starts.setdefault(e["txn_id"], 0)
                file_starts[e["txn_id"]] += 1
    for txn_id, cnt in file_starts.items():
        if cnt > 1:
            conflicts.append({
                "type": "duplicate_start", "txn_id": txn_id,
                "message": f"迁入文件中办理号 {txn_id} 出现 {cnt} 次开始，每个办理号只能开始一次",
            })
        elif db.has_start_elsewhere(conn, txn_id, packets.keys()):
            conflicts.append({
                "type": "duplicate_start", "txn_id": txn_id,
                "message": f"办理号 {txn_id} 已在其他分钟有开始记录，每个办理号只能开始一次",
            })

    # 受影响的已有轮次：观测窗口覆盖任一迁入文件分钟的轮次（先于写入做迁入前快照）
    affected_rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    for m in sorted(packets):
        for row in db.rounds_covering_minute(conn, m):
            if row["round_id"] not in seen:
                seen.add(row["round_id"])
                affected_rows.append(row)
    affected_rows.sort(key=lambda r: (r["window_start_min"], r["round_id"]))
    before_snap = _snapshot_stats(conn, affected_rows)

    if conflicts:
        raise MigrationError(
            "迁入校验未通过，发现 " + str(len(conflicts)) + " 项冲突，已拒绝迁入",
            code=409, conflicts=conflicts,
        )

    # ---- 4) 写入：新轮排班 + 待补分钟包 ----
    db._insert_round_rows(conn, rid, ordered, source="migrated")
    for m, entries in to_insert.items():
        parsed = [(e["txn_id"], datetime.fromisoformat(e["ts"]), e["type"], m) for e in entries]
        db.replace_minute(conn, m, parsed)

    # ---- 5) 重算：新轮 + 受影响的已有轮次 ----
    all_rows = conn.execute(
        "SELECT * FROM rounds ORDER BY window_start_min, round_id"
    ).fetchall()
    target_ids = {rid} | set(before_snap)
    recompute_targets = [r for r in all_rows if r["round_id"] in target_ids]
    results = recompute_rounds(conn, recompute_targets)
    result_by_id = {r["round_id"]: r for r in results}

    new_round_stats = {k: result_by_id[rid][k] for k in STAT_FIELDS}
    affected_plan = []
    for row in affected_rows:
        arid = row["round_id"]
        after = {k: result_by_id[arid][k] for k in STAT_FIELDS}
        affected_plan.append({
            "round_id": arid,
            "before": before_snap[arid],
            "after": after,
            "status_changed": before_snap[arid]["status"] != after["status"],
            "diff_pp_delta": _diff_delta(before_snap[arid]["diff_pp"], after["diff_pp"]),
        })

    plan = {
        "round_id": rid,
        "window_start": fmt_minute(from_minute(window_start)),
        "window_end": fmt_minute(from_minute(window_end)),
        "payload_canonical": _payload_canonical(payload, packets),
        "to_insert": [
            {"minute": fmt_minute(from_minute(m)), "entry_count": len(to_insert[m])}
            for m in sorted(to_insert)
        ],
        "shared": shared,
        "new_round": new_round_stats,
        "affected_rounds": affected_plan,
    }
    plan["fingerprint"] = _fingerprint(plan)
    return plan


def _diff_delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return after - before


def _fingerprint(plan: dict) -> str:
    """计划指纹：只覆盖迁入文件、补入/共享清单与迁入后的最终结果。

    刻意不含“迁入前快照”：预览后发生的无关补传若不改变迁入后的结果，不应使计划过期。
    """
    material = {
        "payload": plan["payload_canonical"],
        "to_insert": plan["to_insert"],
        "shared": plan["shared"],
        "affected_round_ids": [a["round_id"] for a in plan["affected_rounds"]],
        "new_round": plan["new_round"],
        "affected_after": [
            {"round_id": a["round_id"], "after": a["after"]}
            for a in plan["affected_rounds"]
        ],
    }
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def create_preview(conn: sqlite3.Connection, payload: MigrationIn) -> dict:
    """干跑生成预览并落库令牌；干跑写入整体回滚，仅保留预览行。"""
    # 先在事务外做结构解析，坏数据不触碰库
    _structural_parse(payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = _apply(conn, payload)
    except MigrationError:
        conn.execute("ROLLBACK")
        raise
    except db.ValidationError as exc:
        conn.execute("ROLLBACK")
        raise MigrationError(str(exc), code=getattr(exc, "code", 400))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK")  # 干跑内容不落库

    token = secrets.token_hex(16)
    plan_out = dict(plan)
    plan_out["token"] = token
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.save_preview(conn, token, json.dumps(plan, ensure_ascii=False), plan["fingerprint"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return plan_out


def _plan_diff(stored: dict, fresh: dict) -> list[str]:
    """只汇报会影响迁入结果的实质变化；无关补传（迁入后结果不变）不在此列。"""
    changes: list[str] = []
    old_ins = [x["minute"] for x in stored["to_insert"]]
    new_ins = [x["minute"] for x in fresh["to_insert"]]
    if old_ins != new_ins:
        changes.append(
            f"待补分钟变化：{', '.join(old_ins) or '无'} → {', '.join(new_ins) or '无'}"
        )
    old_sh = [x["minute"] for x in stored["shared"]]
    new_sh = [x["minute"] for x in fresh["shared"]]
    if old_sh != new_sh:
        changes.append(
            f"共享分钟变化：{', '.join(old_sh) or '无'} → {', '.join(new_sh) or '无'}"
        )
    old_aff = [a["round_id"] for a in stored["affected_rounds"]]
    new_aff = [a["round_id"] for a in fresh["affected_rounds"]]
    if old_aff != new_aff:
        changes.append(
            f"受影响轮次变化：{', '.join(old_aff) or '无'} → {', '.join(new_aff) or '无'}"
        )
    if stored["new_round"] != fresh["new_round"]:
        changes.append(
            f"新轮 {fresh['round_id']} 预览结果变化："
            f"{_short_stat(stored['new_round'])} → {_short_stat(fresh['new_round'])}"
        )
    old_after = {a["round_id"]: a["after"] for a in stored["affected_rounds"]}
    for a in fresh["affected_rounds"]:
        if old_after.get(a["round_id"]) != a["after"]:
            changes.append(
                f"已有轮次 {a['round_id']} 迁入后的纳入状态/成功率发生变化，需要重新预览"
            )
    return changes


def _short_stat(s: dict) -> str:
    diff = "—" if s["diff_pp"] is None else f"{s['diff_pp']:+.2f}pp"
    return f"{s['status']}（差值 {diff}）"


def confirm_preview(conn: sqlite3.Connection, token: str) -> dict:
    """按当前库重新核对并正式迁入；计划过期或冲突即整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = db.get_preview(conn, token)
        if row is None:
            raise MigrationError("预览令牌不存在或已失效，请重新预览后再确认", code=409)
        stored = json.loads(row["plan_json"])
        payload = MigrationIn.model_validate(stored["payload_canonical"])
        try:
            fresh = _apply(conn, payload)
        except MigrationError as exc:
            raise MigrationError(
                "当前库已出现迁入冲突，确认被拒绝，请重新预览：" + "; ".join(
                    c["message"] for c in exc.conflicts
                ),
                code=409, conflicts=exc.conflicts,
            )
        if fresh["fingerprint"] != stored["fingerprint"]:
            changes = _plan_diff(stored, fresh)
            raise MigrationError(
                "预览后数据库发生影响本次迁入的变化，请重新预览后再确认："
                + "；".join(changes),
                code=409,
            )
        # fresh 已经在本事务内完成写入并完成重算，删除令牌后提交
        db.delete_preview(conn, token)
        conn.execute("COMMIT")
    except MigrationError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return fresh
