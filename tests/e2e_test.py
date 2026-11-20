"""端到端试跑：对运行中的网页服务打真实 HTTP 请求，覆盖全部业务规则。

用法： python tests/e2e_test.py [base_url]
退出码 0 表示全部断言通过。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASS, FAIL = 0, 0
DETAIL: list[str] = []


def call(method: str, path: str, payload=None, expect_status: int | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    parsed = json.loads(body) if body else {}
    if expect_status is not None and status != expect_status:
        raise AssertionError(f"{method} {path} 期望状态 {expect_status}，实际 {status}：{parsed}")
    return status, parsed


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        DETAIL.append(f"  PASS  {name}")
    else:
        FAIL += 1
        DETAIL.append(f"  FAIL  {name} {extra}")


def upload(minute: str, entries):
    return call("POST", "/api/logs/minute", {"minute": minute, "entries": entries}, 200)[1]


def minute(dt: str) -> str:
    return dt[:16]


def main() -> int:
    # ---------- 重置，保证可重复运行 ----------
    call("POST", "/api/reset")

    # ---------- 1. 导入排班 ----------
    schedule = json.load(open("/workspace/sample/schedule.json", encoding="utf-8"))
    status, body = call("POST", "/api/schedule", schedule, 201)
    check("排班导入成功返回 201", sorted(body["imported"]) == ["R1", "R2", "R3"])
    status, body = call("POST", "/api/schedule", schedule, 409)
    check("重复导入排班被拒（409）", status == 409)

    bad_schedule = {
        "rounds": [{
            "round_id": "BAD",
            "segments": [
                {"version": "old", "start": "2026-11-20T10:00:30", "end": "2026-11-20T10:01"},
                {"version": "new", "start": "2026-11-20T10:01", "end": "2026-11-20T10:02"},
            ],
        }]
    }
    # 已导入排班，任何新导入都会先撞 409；reset 后再测整分钟/相邻校验
    call("POST", "/api/reset")
    status, body = call("POST", "/api/schedule", bad_schedule, 400)
    check("非整分钟边界被拒", status == 400 and "整分钟" in body["error"])
    not_adjacent = {
        "rounds": [{
            "round_id": "BAD",
            "segments": [
                {"version": "old", "start": "2026-11-20T10:00", "end": "2026-11-20T10:01"},
                {"version": "new", "start": "2026-11-20T10:02", "end": "2026-11-20T10:03"},
            ],
        }]
    }
    status, body = call("POST", "/api/schedule", not_adjacent, 400)
    check("不相邻时段被拒", "相邻" in body["error"])
    call("POST", "/api/reset")
    call("POST", "/api/schedule", schedule, 201)

    # ---------- 2. R1（窗口 09:00–09:12）----------
    # 旧版 09:00：T1、T2 开始；新版 09:01：T3 开始
    upload("2026-11-20T09:00", [
        {"txn_id": "T1", "ts": "2026-11-20T09:00:10", "type": "start"},
        {"txn_id": "T2", "ts": "2026-11-20T09:00:40", "type": "start"},
    ])
    upload("2026-11-20T09:01", [
        {"txn_id": "T3", "ts": "2026-11-20T09:01:05", "type": "start"},
    ])
    # 跨时段缴费：T1 在新版分钟 09:01 成功，仍归旧版；T2 不缴费
    r = upload("2026-11-20T09:02", [
        {"txn_id": "T1", "ts": "2026-11-20T09:02:00", "type": "success"},
    ])
    check("上传分钟返回受影响轮次 R1", r["recomputed_rounds"] == ["R1"])
    # 空分钟包也算收齐：把 R1 窗口其余分钟（09:03–09:11）补空
    for m in range(3, 12):
        upload(f"2026-11-20T09:{m:02d}", [])
    # T3 在 09:11 成功（开始后 10 分钟内）
    upload("2026-11-20T09:11", [
        {"txn_id": "T3", "ts": "2026-11-20T09:11:00", "type": "success"},
    ])
    rounds = call("GET", "/api/rounds")[1]
    r1 = next(x for x in rounds["rounds"] if x["round_id"] == "R1")
    check("R1 纳入", r1["status"] == "included", str(r1))
    check("R1 旧版 1/2 = 50%（跨时段成功归开始版本）",
          r1["old_starts"] == 2 and r1["old_successes"] == 1 and abs(r1["old_rate"] - 0.5) < 1e-9)
    check("R1 新版 1/1 = 100%", r1["new_starts"] == 1 and r1["new_successes"] == 1)
    check("R1 差值 +50pp", abs(r1["diff_pp"] - 50.0) < 1e-9)

    # ---------- 3. R2（窗口 09:10–09:22，顺序为 new 再 old）----------
    upload("2026-11-20T09:10", [
        {"txn_id": "T4", "ts": "2026-11-20T09:10:00", "type": "start"},
        {"txn_id": "T5", "ts": "2026-11-20T09:10:30", "type": "start"},
    ])
    # 该分钟同时属于 R1 观测窗口尾巴与 R2 旧版时段：合并上传，避免整分钟替换冲掉对方数据
    upload("2026-11-20T09:11", [
        {"txn_id": "T3", "ts": "2026-11-20T09:11:00", "type": "success"},
        {"txn_id": "T6", "ts": "2026-11-20T09:11:20", "type": "start"},
    ])
    # T5 在 09:20 成功：距开始 9m30s，10 分钟内，计入新版
    upload("2026-11-20T09:20", [
        {"txn_id": "T5", "ts": "2026-11-20T09:20:00", "type": "success"},
    ])
    # T4 在 09:20:01 成功：距开始 10m01s，超出 10 分钟，不计
    upload("2026-11-20T09:21", [
        {"txn_id": "T4", "ts": "2026-11-20T09:21:00", "type": "success"},
        # T6 边界：09:11:20 + 10 分钟 = 09:21:20，恰好等于边界，计入旧版
        {"txn_id": "T6", "ts": "2026-11-20T09:21:20", "type": "success"},
    ])
    # 09:12–09:19 空包（09:20/09:21 已传，09:22 在 R2 窗口之外）
    for m in range(12, 20):
        upload(f"2026-11-20T09:{m:02d}", [])
    rounds = call("GET", "/api/rounds")[1]
    r2 = next(x for x in rounds["rounds"] if x["round_id"] == "R2")
    check("R2 纳入（new/old 顺序互换也正确归位）", r2["status"] == "included", str(r2))
    check("R2 新版 1/2（超时成功不计）", r2["new_starts"] == 2 and r2["new_successes"] == 1)
    check("R2 旧版 1/1（恰 10 分钟边界计入）", r2["old_starts"] == 1 and r2["old_successes"] == 1)
    check("R2 差值 50%−100% = −50pp", abs(r2["diff_pp"] + 50.0) < 1e-9)
    check("平均差值 = (50 + -50)/2 = 0", abs(rounds["avg_diff_pp"]) < 1e-9, str(rounds["avg_diff_pp"]))

    # ---------- 4. R3（窗口 09:20–09:32）：新版无人开始 → 未纳入 ----------
    upload("2026-11-20T09:20", [  # 补传替换 09:20：保留 T5 成功并加入 T7 开始
        {"txn_id": "T5", "ts": "2026-11-20T09:20:00", "type": "success"},
        {"txn_id": "T7", "ts": "2026-11-20T09:20:10", "type": "start"},
    ])
    # 09:22–09:31 空包（R3 窗口为 09:20–09:32 左闭右开；09:20/09:21 已传）
    for m in range(22, 32):
        upload(f"2026-11-20T09:{m:02d}", [])
    rounds = call("GET", "/api/rounds")[1]
    r3 = next(x for x in rounds["rounds"] if x["round_id"] == "R3")
    check("R3 未纳入：新版无人开始", r3["status"] == "excluded" and "新版" in (r3["reason"] or ""))
    check("未纳入轮次注明原因", bool(r3["reason"]))
    check("平均值仍只基于 R1/R2 = 0", abs(rounds["avg_diff_pp"]) < 1e-9)
    # R2 窗口也覆盖 09:20，应被同步重算且结果不变
    r2b = next(x for x in rounds["rounds"] if x["round_id"] == "R2")
    check("补传 09:20 时 R2 同步重算且结果不变", abs(r2b["diff_pp"] + 50.0) < 1e-9)

    # ---------- 5. 日志补正：T3 成功从 09:11 撤掉（09:11 分钟包整体替换）----------
    # 注意：09:11 包内只能放属于该分钟的日志；T6 的开始记录原样保留
    upload("2026-11-20T09:11", [
        {"txn_id": "T6", "ts": "2026-11-20T09:11:20", "type": "start"},
    ])
    rounds = call("GET", "/api/rounds")[1]
    r1c = next(x for x in rounds["rounds"] if x["round_id"] == "R1")
    check("补正后 R1 新版成功降为 0、差值变为 −50pp",
          r1c["new_successes"] == 0 and abs(r1c["diff_pp"] + 50.0) < 1e-9, str(r1c))
    # 再改回来
    upload("2026-11-20T09:11", [
        {"txn_id": "T6", "ts": "2026-11-20T09:11:20", "type": "start"},
        {"txn_id": "T3", "ts": "2026-11-20T09:11:00", "type": "success"},
    ])
    rounds = call("GET", "/api/rounds")[1]
    r1d = next(x for x in rounds["rounds"] if x["round_id"] == "R1")
    check("再次补正后 R1 恢复 +50pp", abs(r1d["diff_pp"] - 50.0) < 1e-9)

    # ---------- 6. 失败时保留原日志与结果 ----------
    # 6a. 同一办理号第二次开始（跨分钟）→ 400，原分钟数据不变
    status, body = call("POST", "/api/logs/minute", {
        "minute": "2026-11-20T09:05",
        "entries": [{"txn_id": "T1", "ts": "2026-11-20T09:05:00", "type": "start"}],
    }, 400)
    check("重复开始被拒（400）", status == 400 and "一次" in body["error"])
    minutes = call("GET", "/api/minutes")[1]["buckets"]
    m0905 = next((m for m in minutes if m["minute"] == "2026-11-20T09:05"), None)
    check("失败后原空包保留（未被污染）", m0905 is not None and m0905["entry_count"] == 0)
    # 6b. 日志时间不属于该分钟包 → 400
    status, body = call("POST", "/api/logs/minute", {
        "minute": "2026-11-20T09:06",
        "entries": [{"txn_id": "T9", "ts": "2026-11-20T09:07:00", "type": "start"}],
    }, 400)
    check("跨分钟错包被拒（400）", status == 400)
    # 6c. 坏 JSON 结构（pydantic 422）
    status, body = call("POST", "/api/logs/minute", {"minute": "2026-11-20T09:06"}, 422)
    check("缺字段请求返回 422 且不影响数据", status == 422)
    rounds_after = call("GET", "/api/rounds")[1]
    r1e = next(x for x in rounds_after["rounds"] if x["round_id"] == "R1")
    check("一系列失败后 R1 结果保持 +50pp", abs(r1e["diff_pp"] - 50.0) < 1e-9)

    # ---------- 7. 未齐窗口：新建场景验证缺分钟原因 ----------
    call("POST", "/api/reset")
    call("POST", "/api/schedule", {
        "rounds": [{
            "round_id": "X1",
            "segments": [
                {"version": "old", "start": "2026-11-20T11:00", "end": "2026-11-20T11:01"},
                {"version": "new", "start": "2026-11-20T11:01", "end": "2026-11-20T11:02"},
            ],
        }]
    }, 201)
    upload("2026-11-20T11:00", [{"txn_id": "A", "ts": "2026-11-20T11:00:00", "type": "start"}])
    upload("2026-11-20T11:01", [{"txn_id": "B", "ts": "2026-11-20T11:01:00", "type": "start"}])
    x1 = next(x for x in call("GET", "/api/rounds")[1]["rounds"] if x["round_id"] == "X1")
    check("窗口缺分钟时未纳入并注明缺失", x1["status"] == "excluded" and "未齐" in (x1["reason"] or ""))
    check("缺分钟时不输出差值", x1["diff_pp"] is None)
    check("无纳入轮次时平均差值为空", call("GET", "/api/rounds")[1]["avg_diff_pp"] is None)
    for m in range(2, 12):
        upload(f"2026-11-20T11:{m:02d}", [])
    x1 = next(x for x in call("GET", "/api/rounds")[1]["rounds"] if x["round_id"] == "X1")
    check("补齐空包后自动纳入", x1["status"] == "included" and abs(x1["diff_pp"]) < 1e-9)

    # ---------- 8. 网页 HTML 可打开 ----------
    req = urllib.request.Request(BASE + "/")
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode()
    check("网页返回 200 且含轮次表格", resp.status == 200 and "X1" in html and "算术平均" in html)

    print("\n".join(DETAIL))
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
