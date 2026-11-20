"""Trang quản trị web /admin: người dùng, quyền cấp riêng, nguồn dữ liệu, đồng bộ, báo cáo, audit.

- Role trong `grant_roles` vào được trang (cấp/thu hồi quyền trong phạm vi nguồn mình xem được).
- Role trong `admin_roles` thêm/xoá người dùng, chạy đồng bộ, gửi báo cáo, xem audit.
Form POST kiểm tra Origin cùng máy chủ (chống CSRF), cookie phiên SameSite=Lax.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Callable
from urllib.parse import parse_qs, quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bizagent import access
from bizagent.state import _now as utc_now
from bizagent.webauth import hash_password

e = html.escape


def mount_admin(app: FastAPI, service, get_principal: Callable, page: Callable[[str, str], str]) -> None:
    ws, store = service.ws, service.store

    def guard(request: Request, need_admin: bool = False):
        p = get_principal(request)
        if p is None:
            return None, RedirectResponse("/login?next=/admin", 303)
        if not access.can_grant(ws, p.role) or (need_admin and not access.is_admin(ws, p.role)):
            raise HTTPException(status_code=403, detail="Không có quyền quản trị")
        return p, None

    def same_origin(request: Request) -> None:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="Yêu cầu khác nguồn gốc")

    async def form(request: Request) -> dict[str, str]:
        same_origin(request)
        return {k: v[0].strip() for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}

    def done(msg: str, anchor: str = "") -> RedirectResponse:
        return RedirectResponse(f"/admin?msg={quote(msg)}{anchor}", 303)

    def table(headers: list[str], rows: list[list[str]]) -> str:
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        return f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{body or ""}</tbody></table></div>'

    def btn(action: str, label: str, fields: dict[str, str]) -> str:
        hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in fields.items())
        return (f'<form method="post" action="{e(action)}" style="display:inline">{hidden}'
                f'<button type="submit">{e(label)}</button></form>')

    def card(title: str, body: str, anchor: str = "") -> str:
        return f'<section class="card" id="{anchor}" style="margin-bottom:14px"><h2>{e(title)}</h2>{body}</section>'

    # ------------------------------------------------------------------ trang chính
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_home(request: Request, msg: str = ""):
        p, redirect = guard(request)
        if redirect:
            return redirect
        is_admin = access.is_admin(ws, p.role)
        parts: list[str] = []
        if msg:
            parts.append(f'<div class="card" style="margin-bottom:14px"><b>{e(msg)}</b></div>')

        # nguồn + đồng bộ
        if is_admin:
            rows = []
            for name, cfg in ws.sources.items():
                if name in service.registry.errors:
                    status = f'<span class="err">lỗi: {e(service.registry.errors[name])}</span>'
                elif name in service.mcp_errors:
                    status = f'<span class="err">{e(service.mcp_errors[name])}</span>'
                else:
                    status = "đang bật" if cfg.enabled else "tắt"
                rows.append([e(name), e(cfg.type), e(cfg.description), status])
            sync_rows = []
            runner = getattr(service, "sync_runner", None)
            if runner:
                state = await asyncio.to_thread(runner.warehouse.sync_state)
                for job, j in runner.config.jobs.items():
                    s = state.get(job) or {}
                    last = s.get("last_success")
                    sync_rows.append([e(job), e(j.table), e(s.get("status", "chưa chạy")),
                                      e(f"{last:%Y-%m-%d %H:%M} UTC" if last else "-"),
                                      e(str(s.get("rows") or "")), e(s.get("error") or ""),
                                      btn(f"/admin/sync/{job}", "Chạy ngay", {})])
            parts.append(card("Nguồn dữ liệu", table(["Nguồn", "Loại", "Mô tả", "Trạng thái"], rows)
                              + (table(["Job đồng bộ", "Bảng", "Trạng thái", "Thành công lần cuối", "Dòng", "Lỗi", ""],
                                       sync_rows) if sync_rows else ""), "nguon"))

        # người dùng
        file_ids = {u.id for u in ws.users}
        urows = []
        for u in ws.all_users():
            actions = ""
            if is_admin and u.id not in file_ids and u.id != p.user_id:
                actions = btn("/admin/users/remove", "Xoá", {"id": u.id})
            urows.append([e(u.id), e(u.name), e(u.role), "users.yaml" if u.id in file_ids else "thêm trên web/chat",
                          actions])
        roles_opt = "".join(f'<option value="{e(r)}">{e(r)}</option>' for r in ws.info.roles)
        add_form = "" if not is_admin else f"""
<form method="post" action="/admin/users/add" class="filters" style="padding:10px 0 0">
<label>ID<input name="id" placeholder="telegram:123 / zalo:... / slack:U... / web:ten" required></label>
<label>Tên<input name="name"></label><label>Role<select name="role">{roles_opt}</select></label>
<label>Mật khẩu (tài khoản web)<input name="password" type="password" autocomplete="new-password"></label>
<button type="submit" style="align-self:flex-end">Thêm / cập nhật</button></form>"""
        parts.append(card("Người dùng", table(["ID", "Tên", "Role", "Khai báo ở", ""], urows) + add_form, "nguoi-dung"))

        # quyền cấp riêng
        grows = []
        for g in await asyncio.to_thread(store.list_grants, None, True):
            expired = g["expires_at"] is not None and g["expires_at"] < utc_now()
            until = f"{g['expires_at']:%Y-%m-%d %H:%M} UTC" if g["expires_at"] else "không hết hạn"
            grows.append([e(g["user_id"]), e(g["source"]), e(until + (" (đã hết hạn)" if expired else "")),
                          e(g["granted_by"]), btn("/admin/grants/revoke", "Thu hồi",
                                                  {"user": g["user_id"], "source": g["source"]})])
        my_sources = ws.sources_for(p.user_id, p.role) if not is_admin else [n for n, s in ws.sources.items()
                                                                              if s.enabled]
        src_opt = "".join(f'<option value="{e(s)}">{e(s)}</option>' for s in my_sources)
        grant_form = f"""<form method="post" action="/admin/grants/add" class="filters" style="padding:10px 0 0">
<label>Người dùng<input name="user" placeholder="telegram:123" required></label>
<label>Nguồn<select name="source">{src_opt}</select></label>
<label>Số giờ (trống = không hết hạn)<input name="hours" inputmode="decimal"></label>
<button type="submit" style="align-self:flex-end">Cấp quyền</button></form>"""
        parts.append(card("Quyền cấp riêng (có hạn dùng)",
                          table(["Người dùng", "Nguồn", "Hạn", "Cấp bởi", ""], grows) + grant_form, "quyen"))

        # báo cáo định kỳ
        if is_admin:
            runner = getattr(service, "report_runner", None)
            if runner is None:
                from bizagent.reports import ReportRunner

                try:
                    runner = service.report_runner = ReportRunner(service)
                except Exception as ex:
                    parts.append(card("Báo cáo định kỳ", f'<p class="err">{e(str(ex))}</p>'))
            if runner is not None:
                rrows = []
                for name, spec in runner.config.reports.items():
                    sched = (f"ngày {spec.schedule.day_of_month} hằng tháng" if spec.schedule.day_of_month
                             else ", ".join(spec.schedule.days)) + f" lúc {spec.schedule.at}"
                    rrows.append([e(name), e(spec.title), e(sched), e(", ".join(runner.recipients(spec))),
                                  f'<a href="/admin/reports/{e(name)}/preview">Xem trước</a> '
                                  + btn(f"/admin/reports/{name}/send", "Gửi ngay", {})])
                parts.append(card("Báo cáo định kỳ", table(["Tên", "Tiêu đề", "Lịch", "Người nhận", ""], rrows)
                                  or "<p>Chưa có reports.yaml</p>", "bao-cao"))
            # mức dùng hôm nay (token do model báo về; dùng để soát chi phí API)
            day = service._today()
            usage = await asyncio.to_thread(store.kv_prefix, f"usage:{day}:")
            lim = ws.agents.limits.daily_tokens_per_user
            urows = sorted(((k.split(":", 2)[2], v) for k, v in usage.items()),
                           key=lambda x: -(x[1]["tokens_in"] + x[1]["tokens_out"]))
            utable = table(["Người dùng", "Câu hỏi", "Token vào", "Token ra", "Hạn mức/ngày"],
                           [[e(uid), str(v["asks"]), f'{v["tokens_in"]:,}', f'{v["tokens_out"]:,}',
                             f"{lim:,}" if lim else "không giới hạn"] for uid, v in urows[:100]])
            parts.append(card(f"Mức dùng hôm nay ({day})", utable if urows else "<p>Chưa có câu hỏi nào.</p>",
                              "muc-dung"))
            parts.append('<p><a href="/admin/audit">Xem audit log →</a></p>')
        return page("Quản trị", "".join(parts))

    # ------------------------------------------------------------------ hành động
    def actor(p) -> tuple[str, str]:
        return (p.user_id, p.role)

    @app.post("/admin/users/add")
    async def users_add(request: Request):
        p, redirect = guard(request, need_admin=True)
        if redirect:
            return redirect
        f = await form(request)
        try:
            pw = hash_password(f["password"]) if f.get("password") else None
            uid = await asyncio.to_thread(access.add_user, ws, store, actor(p), f.get("id", ""), f.get("role", ""),
                                          f.get("name", ""), pw)
        except (access.AccessError, ValueError) as ex:
            return done(f"Lỗi: {ex}", "#nguoi-dung")
        return done(f"Đã lưu người dùng {uid}", "#nguoi-dung")

    @app.post("/admin/users/remove")
    async def users_remove(request: Request):
        p, redirect = guard(request, need_admin=True)
        if redirect:
            return redirect
        f = await form(request)
        try:
            uid = await asyncio.to_thread(access.remove_user, ws, store, actor(p), f.get("id", ""))
        except access.AccessError as ex:
            return done(f"Lỗi: {ex}", "#nguoi-dung")
        return done(f"Đã xoá {uid}", "#nguoi-dung")

    @app.post("/admin/grants/add")
    async def grants_add(request: Request):
        p, redirect = guard(request)
        if redirect:
            return redirect
        f = await form(request)
        try:
            hours = float(f["hours"]) if f.get("hours") else None
            uid, _ = await asyncio.to_thread(access.grant, ws, store, actor(p), f.get("user", ""),
                                             f.get("source", ""), hours)
        except (access.AccessError, ValueError) as ex:
            return done(f"Lỗi: {ex}", "#quyen")
        return done(f"Đã cấp nguồn {f.get('source')} cho {uid}", "#quyen")

    @app.post("/admin/grants/revoke")
    async def grants_revoke(request: Request):
        p, redirect = guard(request)
        if redirect:
            return redirect
        f = await form(request)
        try:
            n = await asyncio.to_thread(access.revoke, ws, store, actor(p), f.get("user", ""), f.get("source", "all"))
        except access.AccessError as ex:
            return done(f"Lỗi: {ex}", "#quyen")
        return done(f"Đã thu hồi {n} quyền", "#quyen")

    @app.post("/admin/sync/{job}")
    async def sync_now(job: str, request: Request):
        p, redirect = guard(request, need_admin=True)
        if redirect:
            return redirect
        await form(request)
        runner = getattr(service, "sync_runner", None)
        if runner is None or job not in runner.config.jobs:
            raise HTTPException(status_code=404)
        try:
            n = await asyncio.to_thread(runner.run_job, job)
        except Exception as ex:
            return done(f"Đồng bộ {job} lỗi: {ex}", "#nguon")
        store.audit(p.user_id, "sync_manual", job=job, rows=n)
        return done(f"Đồng bộ {job}: {n} dòng", "#nguon")

    def report_runner():
        from bizagent.reports import ReportRunner

        if getattr(service, "report_runner", None) is None:
            service.report_runner = ReportRunner(service)
        return service.report_runner

    @app.get("/admin/reports/{name}/preview", response_class=HTMLResponse)
    async def report_preview(name: str, request: Request, user: str = ""):
        p, redirect = guard(request, need_admin=True)
        if redirect:
            return redirect
        runner = report_runner()
        if name not in runner.config.reports:
            raise HTTPException(status_code=404)
        recipients = runner.recipients(runner.config.reports[name])
        target = user or (recipients[0] if recipients else p.user_id)
        text = await runner.build(name, target)
        opts = "".join(f'<option {"selected" if r == target else ""}>{e(r)}</option>' for r in recipients)
        body = (f'<form class="filters" style="padding:0 0 10px"><label>Xem như người nhận<select name="user" '
                f'onchange="this.form.submit()">{opts}</select></label></form>'
                f'<pre class="card" style="white-space:pre-wrap">{e(text or "(người này không xem được mục nào)")}</pre>'
                '<p><a href="/admin#bao-cao">← Quay lại</a></p>')
        return page(f"Xem trước: {name}", body)

    @app.post("/admin/reports/{name}/send")
    async def report_send(name: str, request: Request):
        p, redirect = guard(request, need_admin=True)
        if redirect:
            return redirect
        await form(request)
        runner = report_runner()
        if name not in runner.config.reports:
            raise HTTPException(status_code=404)
        res = await runner.send_now(name)
        store.audit(p.user_id, "report_manual", report=name, result=res)
        return done("; ".join(f"{k}: {v}" for k, v in res.items()) or "Không có người nhận", "#bao-cao")

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, user: str = "", kind: str = "", limit: int = 200):
        p, redirect = guard(request, need_admin=True)
        if redirect:
            return redirect
        rows = await asyncio.to_thread(store.audit_rows, min(limit, 1000), user or None)
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        import json as _json

        trs = [[e(f"{r['ts']:%Y-%m-%d %H:%M:%S}"), e(r["user_id"]), e(r["kind"]),
                f'<code>{e(_json.dumps(r["detail"], ensure_ascii=False)[:400])}</code>'] for r in rows]
        filt = (f'<form class="filters" style="padding:0 0 10px"><label>Người dùng<input name="user" value="{e(user)}">'
                f'</label><label>Loại<input name="kind" value="{e(kind)}" placeholder="ask, sql, sql_denied, grant...">'
                '</label><button type="submit" style="align-self:flex-end">Lọc</button></form>')
        return page("Audit log", filt + table(["Thời điểm (UTC)", "Người dùng", "Loại", "Chi tiết"], trs)
                    + '<p><a href="/admin">← Quản trị</a></p>')
