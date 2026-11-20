"""FastAPI 应用：排班导入、分钟日志上传（补传替换）、轮次结果网页。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import database as db
from .migration import MigrationError, confirm_preview, create_preview
from .models import MigrationCommitIn, MigrationIn, MinuteUpload, ScheduleIn
from .stats import fetch_dashboard, recompute_rounds
from .timeutil import minute_floor, parse_ts, to_minute

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="自助缴费机提示版本轮换对比")


@app.on_event("startup")
def on_startup() -> None:
    Path(os.environ.get("APP_DB_PATH", "/data/app.db")).parent.mkdir(parents=True, exist_ok=True)
    db.init_db()


def _redirect(msg: str, ok: bool = True) -> RedirectResponse:
    kind = "ok" if ok else "err"
    return RedirectResponse(url=f"/?{kind}={_q(msg)}", status_code=303)


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db.get_conn()
    try:
        data = fetch_dashboard(conn)
    finally:
        conn.close()
    msg_ok = request.query_params.get("ok")
    msg_err = request.query_params.get("err")
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"data": data, "msg_ok": msg_ok, "msg_err": msg_err},
    )


@app.get("/api/rounds")
def api_rounds():
    conn = db.get_conn()
    try:
        return fetch_dashboard(conn)
    finally:
        conn.close()


@app.get("/api/minutes")
def api_minutes():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT minute, entry_count, received_at FROM minute_buckets ORDER BY minute"
        ).fetchall()
        from .timeutil import from_minute
        return {
            "buckets": [
                {
                    "minute": from_minute(r["minute"]).strftime("%Y-%m-%dT%H:%M"),
                    "entry_count": r["entry_count"],
                    "received_at": r["received_at"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.post("/api/schedule")
def post_schedule(payload: ScheduleIn):
    conn = db.get_conn()
    try:
        with db.transaction(conn):
            ids = db.insert_schedule(conn, payload.rounds)
            # 导入后立刻算一次（此时无日志，全部为未齐）
            rows = db.all_rounds(conn)
            recompute_rounds(conn, rows)
        return JSONResponse({"imported": ids}, status_code=201)
    except db.ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "code", 400))
    finally:
        conn.close()


@app.post("/schedule")
async def schedule_form(request: Request):
    form = await request.form()
    raw = form.get("schedule_json", "")
    try:
        payload = ScheduleIn.model_validate(json.loads(raw))
    except Exception as exc:  # JSON 或 pydantic 校验失败
        return _redirect(f"排班导入失败：{exc}", ok=False)
    conn = db.get_conn()
    try:
        try:
            with db.transaction(conn):
                ids = db.insert_schedule(conn, payload.rounds)
                recompute_rounds(conn, db.all_rounds(conn))
        except db.ValidationError as exc:
            return _redirect(f"排班导入失败：{exc}", ok=False)
        return _redirect(f"已导入排班 {len(ids)} 轮：{', '.join(ids)}")
    finally:
        conn.close()


@app.post("/api/logs/minute")
def post_minute(payload: MinuteUpload):
    """上传（或补传替换）一个整分钟的日志；空包 entries=[] 也算收齐。"""
    conn = db.get_conn()
    try:
        # 先在事务外做解析，保证坏数据不触碰库
        bucket_min = to_minute(minute_floor(parse_ts(payload.minute)))
        if minute_floor(parse_ts(payload.minute)).strftime("%Y-%m-%dT%H:%M") != payload.minute:
            return JSONResponse({"error": "minute 必须是整分钟 YYYY-MM-DDTHH:MM"}, status_code=400)
        parsed = db.validate_log_entries(payload.entries)
        for _, dt, _, m in parsed:
            if m != bucket_min:
                return JSONResponse(
                    {"error": f"日志时间 {dt.isoformat()} 不属于分钟包 {payload.minute}"},
                    status_code=400,
                )
        with db.transaction(conn):
            # 全局校验：每个办理号只能有一次开始（跨分钟同样适用）
            for txn_id, _, typ, _ in parsed:
                if typ != "start":
                    continue
                existed = conn.execute(
                    "SELECT 1 FROM log_entries WHERE txn_id = ? AND type = 'start' "
                    "AND minute <> ? LIMIT 1",
                    (txn_id, bucket_min),
                ).fetchone()
                if existed:
                    raise db.ValidationError(
                        f"办理号 {txn_id} 已在其他分钟有开始记录，每个办理号只能开始一次"
                    )
            db.replace_minute(conn, bucket_min, parsed)
            affected = db.rounds_covering_minute(conn, bucket_min)
            results = recompute_rounds(conn, affected)
        return JSONResponse(
            {
                "minute": payload.minute,
                "stored_entries": len(parsed),
                "recomputed_rounds": [r["round_id"] for r in results],
            },
            status_code=200,
        )
    except db.ValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "code", 400))
    except ValueError as exc:
        return JSONResponse({"error": f"时间格式错误：{exc}"}, status_code=400)
    finally:
        conn.close()


@app.post("/logs")
async def logs_form(request: Request):
    form = await request.form()
    raw = form.get("minute_json", "")
    try:
        payload = MinuteUpload.model_validate(json.loads(raw))
    except Exception as exc:
        return _redirect(f"日志上传失败：{exc}", ok=False)
    # 复用 JSON 接口逻辑
    resp = post_minute(payload)
    if isinstance(resp, JSONResponse) and resp.status_code >= 400:
        body = json.loads(resp.body)
        return _redirect(f"日志上传失败：{body.get('error')}", ok=False)
    body = json.loads(resp.body)
    return _redirect(
        f"分钟 {body['minute']} 已保存 {body['stored_entries']} 条，"
        f"重算轮次：{', '.join(body['recomputed_rounds']) or '无（不在任何观测窗口）'}"
    )


@app.post("/api/migration/preview")
def post_migration_preview(payload: MigrationIn):
    """历史轮次迁入预览：不写业务数据，返回待补分钟、新轮结果、受影响轮次前后差。"""
    conn = db.get_conn()
    try:
        plan = create_preview(conn, payload)
        return JSONResponse(plan, status_code=200)
    except MigrationError as exc:
        return JSONResponse(
            {"error": str(exc), "conflicts": exc.conflicts},
            status_code=getattr(exc, "code", 400),
        )
    finally:
        conn.close()


@app.post("/api/migration/confirm")
def post_migration_confirm(payload: MigrationCommitIn):
    """凭预览令牌确认迁入；按当前库重新核对，计划过期或冲突则拒绝（409）。"""
    conn = db.get_conn()
    try:
        plan = confirm_preview(conn, payload.token)
        return JSONResponse(
            {
                "imported_round": plan["round_id"],
                "inserted_minutes": [x["minute"] for x in plan["to_insert"]],
                "shared_minutes": [x["minute"] for x in plan["shared"]],
                "affected_rounds": [a["round_id"] for a in plan["affected_rounds"]],
            },
            status_code=201,
        )
    except MigrationError as exc:
        return JSONResponse(
            {"error": str(exc), "conflicts": exc.conflicts},
            status_code=getattr(exc, "code", 409),
        )
    finally:
        conn.close()


@app.post("/migration/preview")
async def migration_preview_form(request: Request):
    form = await request.form()
    raw = form.get("migration_json", "")
    try:
        payload = MigrationIn.model_validate(json.loads(raw))
    except Exception as exc:
        return _render_migration_error(request, f"迁入文件解析失败：{exc}")
    conn = db.get_conn()
    try:
        try:
            plan = create_preview(conn, payload)
        except MigrationError as exc:
            return _render_migration_error(
                request, str(exc), conflicts=exc.conflicts, raw_json=raw
            )
        return templates.TemplateResponse(
            request=request,
            name="migration_preview.html",
            context={
                "plan": plan,
                "raw_json": raw,
                "msg_err": None,
                "conflicts": None,
            },
        )
    finally:
        conn.close()


@app.post("/migration/confirm")
async def migration_confirm_form(request: Request):
    form = await request.form()
    token = (form.get("token", "") or "").strip()
    raw = form.get("migration_json", "")
    if not token:
        return _render_migration_error(request, "缺少预览令牌，请重新预览后再确认", raw_json=raw)
    conn = db.get_conn()
    try:
        try:
            plan = confirm_preview(conn, token)
        except MigrationError as exc:
            return _render_migration_error(
                request, str(exc), conflicts=exc.conflicts, raw_json=raw
            )
        return _redirect(
            f"历史轮次 {plan['round_id']} 已迁入：补入 {len(plan['to_insert'])} 个分钟包，"
            f"共享 {len(plan['shared'])} 个，重算轮次 "
            f"{', '.join([plan['round_id']] + [a['round_id'] for a in plan['affected_rounds']])}"
        )
    finally:
        conn.close()


def _render_migration_error(request: Request, message: str, conflicts=None, raw_json: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="migration_preview.html",
        context={
            "plan": None,
            "raw_json": raw_json,
            "msg_err": message,
            "conflicts": conflicts,
        },
        status_code=409 if conflicts else 400,
    )


@app.post("/api/reset")
def reset_all():
    """危险操作：清空全部日志与排班，便于重新演示。"""
    conn = db.get_conn()
    try:
        with db.transaction(conn):
            conn.execute("DELETE FROM log_entries")
            conn.execute("DELETE FROM minute_buckets")
            conn.execute("DELETE FROM round_stats")
            conn.execute("DELETE FROM segments")
            conn.execute("DELETE FROM rounds")
            conn.execute("DELETE FROM migration_previews")
        return {"reset": True}
    finally:
        conn.close()
