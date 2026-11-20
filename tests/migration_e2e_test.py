"""历史轮次迁入端到端试跑：对运行中的网页服务打真实 HTTP 请求。

用法： python tests/migration_e2e_test.py [base_url]
覆盖：
- 新轮迁入（部分分钟补入、部分与现库共享且内容一致，顺序不限）
- 共享分钟内容/重复条数冲突 → 409 且原数据不变
- 开始唯一性冲突（文件内重复、与现库跨分钟重复）→ 409
- 轮次号重复、排班重叠、窗口外分钟、文件内重复包、坏结构 → 409/400
- 预览→确认；确认后再确认令牌失效
- 预览后无关补传不妨碍确认；预览后有关补传使计划过期 → 409 要求重新预览
- 失败时保留原数据（回滚）；迁入轮次与现行轮次同表展示（含来源标记与网页 HTML）
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

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


def form_post(path: str, fields: dict[str, str]):
    """multipart/form-data 表单提交，不自动跟随跳转，返回 (status, body, location)。"""
    boundary = "----e2eboundary7d2a"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        chunks.append(value.encode())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    # 不跟随 303：自定义 opener 拦截重定向响应
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(
        BASE + path, data=b"".join(chunks), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode(), resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Location", "")


def round_seg(rid: str, start: str):
    """生成一轮 2 分钟的排班：old [start, +1)、new [+1, +2)。"""
    d, hm = start.split("T")
    h, m = int(hm[:2]), int(hm[3:5])
    total = h * 60 + m
    s1 = f"{d}T{total // 60:02d}:{total % 60:02d}"
    s2 = f"{d}T{(total + 1) // 60:02d}:{(total + 1) % 60:02d}"
    s3 = f"{d}T{(total + 2) // 60:02d}:{(total + 2) % 60:02d}"
    return {
        "round_id": rid,
        "segments": [
            {"version": "old", "start": s1, "end": s2},
            {"version": "new", "start": s2, "end": s3},
        ],
    }


def migration_payload(rid: str, start: str, minutes: dict):
    return {"round": round_seg(rid, start),
            "minutes": [{"minute": k, "entries": v} for k, v in minutes.items()]}


def main() -> int:
    call("POST", "/api/reset")

    # ---------- 现行轮次 R1：09:00–09:02，窗口 09:00–09:12 ----------
    call("POST", "/api/schedule", {"rounds": [round_seg("R1", "2026-11-20T09:00")]}, 201)
    # 09:00 与 09:01 放现行轮次日志（后续迁入轮将共享 09:10/09:11）
    upload("2026-11-20T09:00", [
        {"txn_id": "T1", "ts": "2026-11-20T09:00:10", "type": "start"},
    ])
    upload("2026-11-20T09:01", [
        {"txn_id": "T2", "ts": "2026-11-20T09:01:10", "type": "start"},
        {"txn_id": "T1", "ts": "2026-11-20T09:01:30", "type": "success"},
    ])
    # 现行轮次窗口的共享分钟 09:10（空包）与 09:11（含 T3 成功），先入库
    upload("2026-11-20T09:10", [])
    upload("2026-11-20T09:11", [
        {"txn_id": "T3", "ts": "2026-11-20T09:11:05", "type": "success"},
    ])
    for m in range(2, 10):
        upload(f"2026-11-20T09:{m:02d}", [])

    rounds = call("GET", "/api/rounds")[1]
    r1_before = next(x for x in rounds["rounds"] if x["round_id"] == "R1")
    check("现行轮次 R1 初始未纳入（新版/旧版都有开始但无成功，仍纳入）",
          r1_before["status"] == "included", str(r1_before))
    check("现行轮次来源标记 current", r1_before["source"] == "current")

    # ---------- 1. 迁入历史轮 H1：08:50–08:52，窗口 08:50–09:02 ----------
    # 窗口与 R1 在 09:00/09:01 交叠：这两个分钟共享；其余分钟补入
    h1_minutes = {}
    h1_minutes["2026-11-20T08:50"] = [
        {"txn_id": "H1-A", "ts": "2026-11-20T08:50:20", "type": "start"},
    ]
    h1_minutes["2026-11-20T08:51"] = [
        {"txn_id": "H1-B", "ts": "2026-11-20T08:51:20", "type": "start"},
        {"txn_id": "H1-A", "ts": "2026-11-20T08:51:40", "type": "success"},
    ]
    for m in range(52, 60):
        h1_minutes[f"2026-11-20T08:{m:02d}"] = []
    # 共享分钟：条目顺序故意打乱，且 ts 用与库内等价的规范化写法
    h1_minutes["2026-11-20T09:00"] = [
        {"txn_id": "T1", "ts": "2026-11-20T09:00:10", "type": "start"},
    ]
    h1_minutes["2026-11-20T09:01"] = [
        {"txn_id": "T1", "ts": "2026-11-20T09:01:30", "type": "success"},
        {"txn_id": "T2", "ts": "2026-11-20T09:01:10", "type": "start"},
    ]
    payload_h1 = migration_payload("H1", "2026-11-20T08:50", h1_minutes)

    status, plan = call("POST", "/api/migration/preview", payload_h1, 200)
    check("H1 预览成功 200", status == 200 and "token" in plan)
    insert_mins = [x["minute"] for x in plan["to_insert"]]
    shared_mins = [x["minute"] for x in plan["shared"]]
    check("预览列出待补分钟（08:50–08:59 共 10 个）",
          insert_mins == [f"2026-11-20T08:{m:02d}" for m in range(50, 60)], str(insert_mins))
    check("预览列出共享分钟（09:00、09:01，顺序不限内容一致）",
          sorted(shared_mins) == ["2026-11-20T09:00", "2026-11-20T09:01"], str(shared_mins))
    check("预览给出新轮 H1 结果（纳入，旧版1/1 新版1/1）",
          plan["new_round"]["status"] == "included"
          and plan["new_round"]["old_starts"] == 1 and plan["new_round"]["new_starts"] == 1
          and plan["new_round"]["old_successes"] == 1,
          str(plan["new_round"]))
    check("预览列出受影响已有轮次 R1 及前后状态",
          [a["round_id"] for a in plan["affected_rounds"]] == ["R1"]
          and plan["affected_rounds"][0]["before"]["status"] == "included",
          str(plan["affected_rounds"]))
    # 预览不写业务数据
    rounds2 = call("GET", "/api/rounds")[1]
    check("预览不写库：仍只有 R1 一轮", [x["round_id"] for x in rounds2["rounds"]] == ["R1"])
    minutes_now = {b["minute"]: b["entry_count"] for b in call("GET", "/api/minutes")[1]["buckets"]}
    check("预览不写库：08:50 尚不存在", "2026-11-20T08:50" not in minutes_now)

    # ---------- 2. 确认 H1 迁入 ----------
    status, done = call("POST", "/api/migration/confirm", {"token": plan["token"]}, 201)
    check("确认迁入返回 201", status == 201 and done["imported_round"] == "H1")
    check("确认返回补入/共享/受影响清单",
          len(done["inserted_minutes"]) == 10
          and sorted(done["shared_minutes"]) == ["2026-11-20T09:00", "2026-11-20T09:01"]
          and done["affected_rounds"] == ["R1"], str(done))
    # 令牌一次性
    status, body = call("POST", "/api/migration/confirm", {"token": plan["token"]}, 409)
    check("令牌确认一次后失效（409）", status == 409 and "失效" in body["error"])
    # 同表展示
    rounds3 = call("GET", "/api/rounds")[1]
    ids = [x["round_id"] for x in rounds3["rounds"]]
    check("历史轮与现行轮在同一张报表", set(ids) == {"R1", "H1"})
    h1 = next(x for x in rounds3["rounds"] if x["round_id"] == "H1")
    check("H1 来源标记 migrated 且已纳入", h1["source"] == "migrated" and h1["status"] == "included")
    r1_after = next(x for x in rounds3["rounds"] if x["round_id"] == "R1")
    check("R1 迁入后被重算且仍纳入", r1_after["status"] == "included")
    # 共享分钟未被修改
    minutes_now = {b["minute"]: b["entry_count"] for b in call("GET", "/api/minutes")[1]["buckets"]}
    check("共享分钟 09:00/09:01 保持原条数", minutes_now["2026-11-20T09:00"] == 1
          and minutes_now["2026-11-20T09:01"] == 2)
    check("待补分钟已补入（08:50 一条、08:52 空包 0 条）",
          minutes_now["2026-11-20T08:50"] == 1 and minutes_now["2026-11-20T08:52"] == 0)

    # ---------- 3. 共享分钟内容冲突 → 409，列出冲突分钟，原数据不变 ----------
    # H2 窗口 08:55–09:07，其中分钟均已由 H1/R1 入库（08:55–08:59 为 H1 补入的空包）
    bad_shared = migration_payload("H2", "2026-11-20T08:55", {
        # 08:55 库内是空包，迁入非空内容 → 冲突
        "2026-11-20T08:55": [{"txn_id": "H2-C", "ts": "2026-11-20T08:55:10", "type": "start"}],
    })
    for m in list(range(56, 60)) + list(range(0, 7)):
        mm = f"2026-11-20T08:{m:02d}" if m >= 56 else f"2026-11-20T09:{m:02d}"
        bad_shared["minutes"].append({"minute": mm, "entries": []})
    status, body = call("POST", "/api/migration/preview", bad_shared, 409)
    check("共享分钟内容不一致 → 409 且列出冲突分钟",
          status == 409 and any(c["type"] == "minute_conflict" and c["minute"] == "2026-11-20T08:55"
                                for c in body["conflicts"]), str(body))
    check("冲突响应同时回带双方条目内容",
          all("incoming" in c and "existing" in c
              for c in body["conflicts"] if c["type"] == "minute_conflict"), str(body))
    rounds4 = call("GET", "/api/rounds")[1]
    check("冲突后 H2 未写入、原轮次不变",
          {x["round_id"] for x in rounds4["rounds"]} == {"R1", "H1"})
    minutes_now = {b["minute"]: b["entry_count"] for b in call("GET", "/api/minutes")[1]["buckets"]}
    check("冲突后 08:55 仍为原空包（0 条）", minutes_now.get("2026-11-20T08:55") == 0)

    # 重复条数不一致：先把 08:55 常规补传为 1 条，再用含 2 条相同开始的迁入包触发冲突
    upload("2026-11-20T08:55", [
        {"txn_id": "H2-A", "ts": "2026-11-20T08:55:10", "type": "start"},
    ])
    dup_count = migration_payload("H2", "2026-11-20T08:55", {
        "2026-11-20T08:55": [
            {"txn_id": "H2-A", "ts": "2026-11-20T08:55:10", "type": "start"},
            {"txn_id": "H2-A", "ts": "2026-11-20T08:55:10", "type": "start"},
        ],
    })
    for m in list(range(56, 60)) + list(range(0, 7)):
        mm = f"2026-11-20T08:{m:02d}" if m >= 56 else f"2026-11-20T09:{m:02d}"
        dup_count["minutes"].append({"minute": mm, "entries": []})
    status, body = call("POST", "/api/migration/preview", dup_count, 409)
    check("共享分钟重复条数不一致（2 vs 1）→ 409 并指明条数",
          status == 409
          and any(c["type"] == "minute_conflict" and "库内 1 条，迁入 2 条" in c["message"]
                  for c in body["conflicts"]), str(body))
    minutes_now = {b["minute"]: b["entry_count"] for b in call("GET", "/api/minutes")[1]["buckets"]}
    check("冲突后 08:55 仍是库内原 1 条", minutes_now["2026-11-20T08:55"] == 1)
    # 还原 08:55 空包，避免影响后续用例
    upload("2026-11-20T08:55", [])

    # ---------- 4. 开始唯一性冲突 ----------
    # 4a. 与现库跨分钟重复开始（T1 已在 09:00 开始；H3 在自己窗口的新分钟再开始一次）
    dup_start = migration_payload("H3", "2026-11-20T07:00", {
        "2026-11-20T07:00": [
            {"txn_id": "T1", "ts": "2026-11-20T07:00:10", "type": "start"},
        ],
        "2026-11-20T07:01": [
            {"txn_id": "H3-B", "ts": "2026-11-20T07:01:10", "type": "start"},
        ],
    })
    for m in range(2, 12):
        dup_start["minutes"].append({"minute": f"2026-11-20T07:{m:02d}", "entries": []})
    status, body = call("POST", "/api/migration/preview", dup_start, 409)
    check("与现库重复的办理开始 → 409",
          status == 409 and any(c["type"] == "duplicate_start" and c["txn_id"] == "T1"
                                for c in body["conflicts"]), str(body))
    # 4b. 文件内同一办理号两次开始
    dup_in_file = migration_payload("H3", "2026-11-20T07:00", {
        "2026-11-20T07:00": [
            {"txn_id": "Z9", "ts": "2026-11-20T07:00:10", "type": "start"},
            {"txn_id": "Z9", "ts": "2026-11-20T07:00:40", "type": "start"},
        ],
        "2026-11-20T07:01": [
            {"txn_id": "H3-B", "ts": "2026-11-20T07:01:10", "type": "start"},
        ],
    })
    for m in range(2, 12):
        dup_in_file["minutes"].append({"minute": f"2026-11-20T07:{m:02d}", "entries": []})
    status, body = call("POST", "/api/migration/preview", dup_in_file, 409)
    check("文件内重复开始 → 409",
          any(c["type"] == "duplicate_start" and c["txn_id"] == "Z9" for c in body["conflicts"]),
          str(body))
    check("开始冲突时 H3 未写入", all(x["round_id"] != "H3" for x in call("GET", "/api/rounds")[1]["rounds"]))

    # ---------- 5. 排班类冲突 ----------
    dup_round = migration_payload("H1", "2026-11-20T07:30", {})
    status, body = call("POST", "/api/migration/preview", dup_round, 409)
    check("轮次号重复 → 409",
          status == 409 and any(c["type"] == "duplicate_round" for c in body["conflicts"]), str(body))
    # 时段与 R1（09:00 起）重叠
    overlap = migration_payload("H4", "2026-11-20T08:59", {})
    status, body = call("POST", "/api/migration/preview", overlap, 409)
    check("排班时段重叠 → 409",
          status == 409 and any(c["type"] == "schedule_overlap" for c in body["conflicts"]), str(body))
    # 窗口外分钟 → 400
    out_window = migration_payload("H4", "2026-11-20T07:30", {
        "2026-11-20T07:30": [{"txn_id": "H4-A", "ts": "2026-11-20T07:30:10", "type": "start"}],
        # 窗口 07:30–07:42，07:42 已在窗口外
        "2026-11-20T07:42": [],
    })
    status, body = call("POST", "/api/migration/preview", out_window, 400)
    check("窗口外分钟 → 400", status == 400 and "观测窗口" in body["error"], str(body))
    # 文件内同一分钟两个包 → 400
    dup_pkt = migration_payload("H4", "2026-11-20T07:30", {
        "2026-11-20T07:30": [],
    })
    dup_pkt["minutes"].append({"minute": "2026-11-20T07:30", "entries": []})
    status, body = call("POST", "/api/migration/preview", dup_pkt, 400)
    check("文件内重复分钟包 → 400", status == 400 and "重复包" in body["error"], str(body))
    # 坏结构（缺 round）→ 422
    status, body = call("POST", "/api/migration/preview", {"minutes": []}, 422)
    check("缺字段 → 422", status == 422)
    # 非整分钟排班 → 400
    bad_sched = {"round": {
        "round_id": "H4",
        "segments": [
            {"version": "old", "start": "2026-11-20T07:30:30", "end": "2026-11-20T07:31"},
            {"version": "new", "start": "2026-11-20T07:31", "end": "2026-11-20T07:32"},
        ],
    }, "minutes": []}
    status, body = call("POST", "/api/migration/preview", bad_sched, 400)
    check("排班非整分钟 → 400", status == 400 and "整分钟" in body["error"], str(body))

    # ---------- 6. 预览后补传：无关补传不妨碍确认；有关补传使计划过期 ----------
    # H5 窗口 10:00–10:12，完全独立
    h5 = migration_payload("H5", "2026-11-20T10:00", {
        "2026-11-20T10:00": [{"txn_id": "H5-A", "ts": "2026-11-20T10:00:10", "type": "start"}],
        "2026-11-20T10:01": [{"txn_id": "H5-B", "ts": "2026-11-20T10:01:10", "type": "start"}],
    })
    for m in range(2, 12):
        h5["minutes"].append({"minute": f"2026-11-20T10:{m:02d}", "entries": []})
    plan5 = call("POST", "/api/migration/preview", h5, 200)[1]
    # 无关补传：上传一个不落在 H5 窗口、也不影响其受影响轮次集合的分钟（12:00）
    upload("2026-11-20T12:00", [])
    status, done5 = call("POST", "/api/migration/confirm", {"token": plan5["token"]}, 201)
    check("预览后无关补传不妨碍确认（201）", status == 201 and done5["imported_round"] == "H5")

    # 6b. 受影响轮次窗口内的等价补传（整分钟替换为相同内容）不改变迁入后结果，仍可确认
    h5b = migration_payload("H5b", "2026-11-20T10:20", {
        "2026-11-20T10:20": [{"txn_id": "H5b-A", "ts": "2026-11-20T10:20:10", "type": "start"}],
        "2026-11-20T10:21": [{"txn_id": "H5b-B", "ts": "2026-11-20T10:21:10", "type": "start"}],
    })
    for m in range(22, 32):
        h5b["minutes"].append({"minute": f"2026-11-20T10:{m:02d}", "entries": []})
    plan5b = call("POST", "/api/migration/preview", h5b, 200)[1]
    # R1 窗口内 09:05 做一次等价空包整分钟替换（内容不变，迁入后结果不变）
    upload("2026-11-20T09:05", [])
    status, done5b = call("POST", "/api/migration/confirm", {"token": plan5b["token"]}, 201)
    check("受影响轮次窗口内的等价补传不改变迁入后结果，仍可确认（201）",
          status == 201 and done5b["imported_round"] == "H5b", str(status))

    # H6：窗口 11:00–11:12；预览后向其待补分钟补传，使待补清单变化 → 409
    h6 = migration_payload("H6", "2026-11-20T11:00", {
        "2026-11-20T11:00": [{"txn_id": "H6-A", "ts": "2026-11-20T11:00:10", "type": "start"}],
        "2026-11-20T11:01": [{"txn_id": "H6-B", "ts": "2026-11-20T11:01:10", "type": "start"}],
    })
    for m in range(2, 12):
        h6["minutes"].append({"minute": f"2026-11-20T11:{m:02d}", "entries": []})
    plan6 = call("POST", "/api/migration/preview", h6, 200)[1]
    check("H6 预览列出 11:05 为待补",
          any(x["minute"] == "2026-11-20T11:05" for x in plan6["to_insert"]))
    # 运营员在确认前先通过常规上传补了 11:05（与迁入文件同内容的空包）
    upload("2026-11-20T11:05", [])
    status, body = call("POST", "/api/migration/confirm", {"token": plan6["token"]}, 409)
    check("预览后有关补传改变待补清单 → 409 要求重新预览",
          status == 409 and "重新预览" in body["error"], str(body))
    # 重新预览：11:05 现在是共享分钟，计划仍然可确认
    plan6b = call("POST", "/api/migration/preview", h6, 200)[1]
    check("重新预览后 11:05 变为共享分钟",
          any(x["minute"] == "2026-11-20T11:05" for x in plan6b["shared"])
          and not any(x["minute"] == "2026-11-20T11:05" for x in plan6b["to_insert"]))
    status, done6 = call("POST", "/api/migration/confirm", {"token": plan6b["token"]}, 201)
    check("重新预览后确认成功", status == 201 and done6["imported_round"] == "H6")
    # 旧令牌未被消费，但它指向的旧计划已过期
    status, body = call("POST", "/api/migration/confirm", {"token": plan6["token"]}, 409)
    check("旧令牌确认过期计划仍被 409 拒绝", status == 409 and "重新预览" in body["error"])

    # ---------- 7. 失败整体回滚：构造确认期冲突（预览后制造开始唯一冲突）----------
    h7 = migration_payload("H7", "2026-11-20T13:00", {
        "2026-11-20T13:00": [{"txn_id": "H7-A", "ts": "2026-11-20T13:00:10", "type": "start"}],
        "2026-11-20T13:01": [{"txn_id": "H7-B", "ts": "2026-11-20T13:01:10", "type": "start"}],
    })
    for m in range(2, 12):
        h7["minutes"].append({"minute": f"2026-11-20T13:{m:02d}", "entries": []})
    plan7 = call("POST", "/api/migration/preview", h7, 200)[1]
    # 预览后、确认前，让 H7-A 在其他分钟先行开始（13:30 包，落在 H7 窗口内但用不同 txn 时间）
    upload("2026-11-20T13:30", [
        {"txn_id": "H7-A", "ts": "2026-11-20T13:30:00", "type": "start"},
    ])
    before_ids = [x["round_id"] for x in call("GET", "/api/rounds")[1]["rounds"]]
    status, body = call("POST", "/api/migration/confirm", {"token": plan7["token"]}, 409)
    check("确认期新冲突 → 409", status == 409 and "冲突" in body["error"], str(body))
    after_ids = [x["round_id"] for x in call("GET", "/api/rounds")[1]["rounds"]]
    check("失败整体回滚：H7 未写入、原轮次不变", before_ids == after_ids and "H7" not in after_ids,
          str(after_ids))

    # ---------- 8. 网页表单（multipart）：预览页渲染 + 确认表单 + 冲突页 ----------
    h8 = migration_payload("H8", "2026-11-20T14:00", {
        "2026-11-20T14:00": [{"txn_id": "H8-A", "ts": "2026-11-20T14:00:10", "type": "start"}],
        "2026-11-20T14:01": [{"txn_id": "H8-B", "ts": "2026-11-20T14:01:10", "type": "start"}],
    })
    for m in range(2, 12):
        h8["minutes"].append({"minute": f"2026-11-20T14:{m:02d}", "entries": []})
    html_prev = form_post("/migration/preview", {"migration_json": json.dumps(h8)})
    check("网页预览返回 200 且含待补/共享/新轮结果/确认表单",
          html_prev[0] == 200 and "待补分钟" in html_prev[1] and "新轮 H8 预览结果" in html_prev[1]
          and "/migration/confirm" in html_prev[1], html_prev[0])
    token8 = html_prev[1].split('name="token" value="')[1].split('"')[0]
    html_done = form_post("/migration/confirm",
                          {"token": token8, "migration_json": json.dumps(h8)})
    check("网页确认后 303 跳回总览页", html_done[0] == 303 and html_done[2].startswith("/?ok="),
          str(html_done[0]))
    html_index = urllib.request.urlopen(BASE + "/", timeout=10).read().decode()
    check("总览页同表标注历史迁入/现行轮次且含 H1、H8",
          "历史迁入" in html_index and "现行轮次" in html_index
          and "H1" in html_index and "H8" in html_index)
    # 冲突时网页渲染冲突明细页（409）
    conflict_payload = migration_payload("H1", "2026-11-20T15:00", {})  # 轮次号重复
    html_conflict = form_post("/migration/preview",
                              {"migration_json": json.dumps(conflict_payload)})
    check("网页冲突页返回 409 且展示冲突明细",
          html_conflict[0] == 409 and "冲突明细" in html_conflict[1]
          and "duplicate_round" in html_conflict[1], str(html_conflict[0]))
    # 坏 JSON 走 400 错误页
    html_bad = form_post("/migration/preview", {"migration_json": "{not json"})
    check("网页坏 JSON 返回 400 错误页", html_bad[0] == 400 and "迁入被拒绝" in html_bad[1],
          str(html_bad[0]))

    # ---------- 9. reset 清理预览与迁入数据 ----------
    call("POST", "/api/reset")

    # ---------- 10. 受影响轮次状态翻转与成功率差变化 ----------
    # 现行轮 R9（09:20–09:22，窗口 09:20–09:32）：只传 09:20/09:21 两个有事件的包，
    # 中段缺失；尾巴 09:28–09:31 用常规上传补空，使 R9 仍未纳入（缺 09:22–09:27）。
    call("POST", "/api/schedule", {"rounds": [round_seg("R9", "2026-11-20T09:20")]}, 201)
    upload("2026-11-20T09:20", [
        {"txn_id": "R9-A", "ts": "2026-11-20T09:20:10", "type": "start"},
        {"txn_id": "R9-B", "ts": "2026-11-20T09:20:40", "type": "start"},
    ])
    upload("2026-11-20T09:21", [
        {"txn_id": "R9-C", "ts": "2026-11-20T09:21:10", "type": "start"},
        {"txn_id": "R9-A", "ts": "2026-11-20T09:21:30", "type": "success"},
    ])
    for m in range(28, 32):
        upload(f"2026-11-20T09:{m:02d}", [])
    r9_before = next(x for x in call("GET", "/api/rounds")[1]["rounds"] if x["round_id"] == "R9")
    check("迁入前 R9 未纳入（缺分钟）", r9_before["status"] == "excluded" and "未齐" in (r9_before["reason"] or ""))
    # H9 排班 09:18–09:20（与 R9 排班 09:20–09:22 首尾相接、互不重叠），窗口 09:18–09:30；
    # 与 R9 在 09:20/09:21 共享（内容一致、顺序打乱），补入 R9 缺失的 09:22–09:27。
    h9 = migration_payload("H9", "2026-11-20T09:18", {
        # H9 自己时段内的开始（与 R9 无关的新办理号），保证 H9 两版都有人开始
        "2026-11-20T09:18": [{"txn_id": "H9-X", "ts": "2026-11-20T09:18:10", "type": "start"}],
        "2026-11-20T09:19": [{"txn_id": "H9-Y", "ts": "2026-11-20T09:19:10", "type": "start"}],
        "2026-11-20T09:20": [
            {"txn_id": "R9-B", "ts": "2026-11-20T09:20:40", "type": "start"},
            {"txn_id": "R9-A", "ts": "2026-11-20T09:20:10", "type": "start"},
        ],
        "2026-11-20T09:21": [
            {"txn_id": "R9-A", "ts": "2026-11-20T09:21:30", "type": "success"},
            {"txn_id": "R9-C", "ts": "2026-11-20T09:21:10", "type": "start"},
        ],
    })
    for m in list(range(22, 30)):
        h9["minutes"].append({"minute": f"2026-11-20T09:{m:02d}", "entries": []})
    plan9 = call("POST", "/api/migration/preview", h9, 200)[1]
    aff9 = next(a for a in plan9["affected_rounds"] if a["round_id"] == "R9")
    check("预览显示 R9 纳入状态翻转 excluded → included",
          aff9["before"]["status"] == "excluded" and aff9["after"]["status"] == "included"
          and aff9["status_changed"] is True, str(aff9))
    check("预览显示 R9 迁入后差值（新版0% − 旧版50% = −50pp）",
          abs(aff9["after"]["diff_pp"] + 50.0) < 1e-9
          and aff9["before"]["diff_pp"] is None and aff9["diff_pp_delta"] is None, str(aff9))
    check("预览新轮 H9 自身纳入", plan9["new_round"]["status"] == "included", str(plan9["new_round"]))
    call("POST", "/api/migration/confirm", {"token": plan9["token"]}, 201)
    r9_after = next(x for x in call("GET", "/api/rounds")[1]["rounds"] if x["round_id"] == "R9")
    check("确认后 R9 确实纳入且差值 −50pp",
          r9_after["status"] == "included" and abs(r9_after["diff_pp"] + 50.0) < 1e-9, str(r9_after))
    h9_row = next(x for x in call("GET", "/api/rounds")[1]["rounds"] if x["round_id"] == "H9")
    check("H9 标记为历史迁入并与 R9 同表", h9_row["source"] == "migrated")

    # ---------- 11. reset 清理 ----------
    call("POST", "/api/reset")
    rounds = call("GET", "/api/rounds")[1]
    check("reset 后轮次清空", rounds["rounds"] == [])

    print("\n".join(DETAIL))
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
