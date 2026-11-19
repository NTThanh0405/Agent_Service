"""Web server: REST API chat, trang chat, dashboard sống, dữ liệu cho Power BI.

    POST /api/v1/chat                       {"message": "...", "thread_id": "abc"}
    GET  /api/v1/health
    GET  /                                  trang chat
    GET  /login, POST /login, GET /logout   đăng nhập tài khoản web
    GET  /login/link?t=...                  link đăng nhập gửi qua Telegram (/login)
    GET  /dashboards, /d/<slug>             danh sách và trang dashboard sống
    GET  /api/v1/dashboards[/<slug>[/data]] spec và dữ liệu dashboard (JSON)
    GET  /powerbi, /powerbi/<dataset>.csv|.json   dữ liệu cho Power BI (HTTP Basic hoặc Bearer)

Xác thực: xem bizagent/webauth.py. Nghe ngoài 127.0.0.1 thì phải có BIZAGENT_API_TOKEN hoặc tài khoản web.
"""

from __future__ import annotations

import asyncio
import html
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from bizagent import __version__
from bizagent.agent.service import AgentService
from bizagent.dashboards.engine import AccessDenied, DashboardError
from bizagent.dashboards.powerbi import column_types, dataset_params, m_code, to_csv, to_json
from bizagent.sources.base import SourceError
from bizagent.webauth import SESSION_COOKIE, SESSION_DAYS, Principal, WebAuth

STATIC_DIR = Path(__file__).resolve().parent / "static"
DASH_CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
STATIC_FILES = {"dashboard.js": "application/javascript", "dashboard.css": "text/css"}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str = Field(default="default", max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class ChatResponse(BaseModel):
    answer: str
    thread_id: str


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/static/dashboard.css"></head>
<body><header class="top"><div class="titles"><h1>{html.escape(title)}</h1></div>
<div class="status"><a href="/dashboards">Dashboard</a> · <a href="/powerbi">Power BI</a> · <a href="/">Chat</a> · <a href="/admin">Quản trị</a>
 · <a href="/logout">Đăng xuất</a></div></header><main style="padding:20px;max-width:1100px">{body}</main></body></html>"""


def create_app(service: AgentService) -> FastAPI:
    auth = WebAuth(service.ws)
    if auth.api_role not in service.ws.info.roles:
        raise RuntimeError(f"BIZAGENT_API_ROLE='{auth.api_role}' không có trong roles của workspace")
    catalog = service.catalog
    secure_cookie = bool(catalog and catalog.config.public_url.startswith("https://"))

    from bizagent.channels.slack import SlackChannel
    from bizagent.channels.zalo import ZaloChannel, channels_config, load_zalo_config

    zalo_cfg = load_zalo_config(service.ws)
    zalo = ZaloChannel(service, zalo_cfg) if zalo_cfg.enabled else None
    slack = SlackChannel(service) if (channels_config(service.ws).get("slack") or {}).get("enabled") else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = service.start_background_tasks()
        await service.ensure_ready()
        yield
        for t in tasks:
            t.cancel()
        if zalo:
            await zalo.client.aclose()
        if slack:
            await slack.client.aclose()
        await service.aclose()

    app = FastAPI(title="bizagent", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.zalo = zalo
    app.state.slack = slack
    if zalo:
        zalo.mount(app)
    if slack:
        slack.mount(app)
    from bizagent.channels.admin import mount_admin

    mount_admin(app, service, lambda request: principal(request), _page)

    # ------------------------------------------------------------------ xác thực
    def principal(request: Request) -> Principal | None:
        return auth.authenticate(
            cookie=request.cookies.get(SESSION_COOKIE),
            authorization=request.headers.get("authorization", ""),
            client_host=request.client.host if request.client else None,
        )

    def require_api(request: Request) -> Principal:
        p = principal(request)
        if p is None:
            raise HTTPException(status_code=401, detail="Cần đăng nhập hoặc token")
        return p

    def require_page(request: Request) -> Principal | RedirectResponse:
        p = principal(request)
        if p is None:
            return RedirectResponse("/login?next=" + quote(request.url.path + ("?" + request.url.query
                                                                               if request.url.query else "")), 303)
        return p

    def require_basic(request: Request) -> Principal:
        """Cho Power BI/Excel: trả 401 kèm WWW-Authenticate để công cụ hỏi tên/mật khẩu."""
        p = principal(request)
        if p is None:
            raise HTTPException(status_code=401, detail="Cần xác thực", headers={"WWW-Authenticate": 'Basic realm="bizagent"'})
        return p

    def need_catalog():
        if catalog is None:
            raise HTTPException(status_code=404, detail=f"Dashboard chưa bật hoặc lỗi cấu hình: {service.catalog_error}")
        catalog.maybe_reload()
        return catalog

    # ------------------------------------------------------------------ chung
    @app.get("/api/v1/health")
    def health() -> dict:
        return {
            "status": "ok",
            "workspace": service.ws.name,
            "model": service.model_ref.label,
            "sources": sorted(service.registry.sources),
            "source_errors": service.registry.errors,
            "sync": service.sync_status(),
            "dashboards": sorted(catalog.dashboards) if catalog else [],
            "dashboard_errors": (catalog.errors if catalog else {"_config": service.catalog_error or ""}),
            "mcp_errors": service.mcp_errors,
            "channels": {
                "zalo": ({"enabled": True, "missing_env": zalo.check_ready()} if zalo else {"enabled": False}),
                "slack": ({"enabled": True, "missing_env": slack.missing_env()} if slack else {"enabled": False}),
            },
        }

    @app.get("/static/{name}")
    def static(name: str):
        if name not in STATIC_FILES:
            raise HTTPException(status_code=404)
        return FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name], headers={"Cache-Control": "max-age=300"})

    # ------------------------------------------------------------------ đăng nhập
    @app.get("/login", response_class=HTMLResponse)
    def login_page(next: str = "/dashboards", error: str = "") -> str:
        msg = '<p class="err">Sai tên hoặc mật khẩu</p>' if error else ""
        hint = ("<p class='note'>Người dùng Telegram: gõ <b>/login</b> với bot để nhận link đăng nhập.</p>")
        body = f"""<section class="card" style="max-width:380px">{msg}
<form method="post" action="/login"><input type="hidden" name="next" value="{html.escape(next)}">
<div class="filters" style="flex-direction:column;padding:0">
<label>Tên đăng nhập<input name="username" autocomplete="username" required></label>
<label>Mật khẩu<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit" style="padding:8px;border-radius:6px;border:0;background:var(--accent);color:#fff">Đăng nhập</button>
</div></form>{hint}</section>"""
        return _page("Đăng nhập", body)

    def _safe_next(n: str) -> str:
        return n if n.startswith("/") and not n.startswith("//") else "/dashboards"

    def _with_session(resp: Response, user_id: str) -> Response:
        resp.set_cookie(SESSION_COOKIE, auth.session_token(user_id), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=secure_cookie)
        return resp

    @app.post("/login")
    async def login(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
        nxt = _safe_next(form.get("next", "/dashboards"))
        p = await asyncio.to_thread(auth.check_password, form.get("username", ""), form.get("password", ""))
        if p is None:
            return RedirectResponse(f"/login?error=1&next={quote(nxt)}", 303)
        return _with_session(RedirectResponse(nxt, 303), p.user_id)

    @app.get("/login/link")
    def login_link(t: str, next: str = "/dashboards"):
        session = auth.exchange_link(t)
        if session is None:
            return HTMLResponse(_page("Link hết hạn", "<p>Link đăng nhập không hợp lệ hoặc đã hết hạn. "
                                      "Gõ /login với bot để nhận link mới.</p>"), status_code=400)
        resp = RedirectResponse(_safe_next(next), 303)
        resp.set_cookie(SESSION_COOKIE, session, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=secure_cookie)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ------------------------------------------------------------------ chat
    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request) -> ChatResponse:
        p = require_api(request)
        answer = await service.ask(req.message, user_id=p.user_id, role=p.role, thread_id=req.thread_id)
        return ChatResponse(answer=answer, thread_id=req.thread_id)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if auth.has_web_users:                      # có tài khoản web: phải đăng nhập
            p = require_page(request)
            if isinstance(p, RedirectResponse):
                return p
        page = (STATIC_DIR / "chat.html").read_text(encoding="utf-8")
        return page.replace("{{TITLE}}", html.escape(service.ws.info.display_name or service.ws.name))

    # ------------------------------------------------------------------ dashboard sống
    @app.get("/dashboards", response_class=HTMLResponse)
    def dashboards_page(request: Request):
        p = require_page(request)
        if isinstance(p, RedirectResponse):
            return p
        cat = need_catalog()
        items = []
        for slug in cat.visible(p.role, p.user_id):
            d = cat.dashboards[slug]
            items.append(f'<section class="card" style="grid-column:span 4"><h2><a href="/d/{slug}">'
                         f'{html.escape(d.title)}</a></h2><div class="note">{html.escape(d.description)}</div>'
                         f'<div class="meta">làm mới mỗi {d.refresh_seconds}s'
                         f'{" · do agent tạo" if d.created_by == "agent" else ""}</div></section>')
        for _key, r in cat.config.powerbi.reports.items():
            if not r.roles or p.role in r.roles:
                items.append(f'<section class="card" style="grid-column:span 4"><h2><a href="{html.escape(r.url)}" '
                             f'target="_blank" rel="noopener">{html.escape(r.title)} ↗</a></h2>'
                             f'<div class="note">{html.escape(r.description)}</div><div class="meta">Power BI</div>'
                             "</section>")
        if p.role == "admin" and cat.errors:
            errs = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(v)}</li>" for k, v in cat.errors.items())
            items.append(f'<section class="card" style="grid-column:1/-1"><h2>Dashboard lỗi cấu hình</h2>'
                         f'<ul class="err">{errs}</ul></section>')
        body = f'<div class="grid" style="padding:0">{"".join(items) or "<p>Chưa có dashboard nào.</p>"}</div>'
        return _page("Dashboard", body)

    @app.get("/d/{slug}", response_class=HTMLResponse)
    def dashboard_page(slug: str, request: Request):
        p = require_page(request)
        if isinstance(p, RedirectResponse):
            return p
        cat = need_catalog()
        if not cat.can_view(slug, p.role, p.user_id):
            raise HTTPException(status_code=404, detail="Không có dashboard này hoặc không có quyền xem")
        page = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8").replace("{{SLUG}}", html.escape(slug))
        return HTMLResponse(page, headers={"Content-Security-Policy": DASH_CSP, "Cache-Control": "no-store"})

    @app.get("/api/v1/dashboards")
    def list_dashboards(request: Request) -> list[dict]:
        p = require_api(request)
        cat = need_catalog()
        return [{"slug": s, "title": cat.dashboards[s].title, "url": f"/d/{s}"} for s in cat.visible(p.role, p.user_id)]

    @app.get("/api/v1/dashboards/{slug}")
    def dashboard_spec(slug: str, request: Request) -> dict:
        p = require_api(request)
        cat = need_catalog()
        if not cat.can_view(slug, p.role, p.user_id):
            raise HTTPException(status_code=404, detail="Không có dashboard này hoặc không có quyền xem")
        spec = cat.dashboards[slug]
        # không gửi SQL ra trình duyệt
        return spec.model_dump(exclude={"datasets"})

    @app.get("/api/v1/dashboards/{slug}/data")
    async def dashboard_data(slug: str, request: Request) -> dict:
        p = require_api(request)
        need_catalog()
        try:
            return await asyncio.to_thread(service.dashboard_engine.run_dashboard, slug, dict(request.query_params),
                                           p.role, p.user_id)
        except AccessDenied as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except DashboardError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/v1/dashboards/{slug}/script/{name}")
    def dashboard_script(slug: str, name: str, request: Request):
        p = require_api(request)
        cat = need_catalog()
        if not cat.can_view(slug, p.role, p.user_id):
            raise HTTPException(status_code=404)
        allowed = {w.script for w in cat.dashboards[slug].widgets if w.script}
        if name not in allowed:
            raise HTTPException(status_code=404)
        return FileResponse(cat.dir / name, media_type="application/javascript", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ Power BI
    def _powerbi_dataset(name: str, request: Request, p: Principal):
        cat = need_catalog()
        if not cat.config.powerbi.enabled or name not in cat.powerbi_datasets():
            raise HTTPException(status_code=404, detail="Dataset không được mở cho Power BI")
        eng = service.dashboard_engine
        try:
            params = dataset_params(eng, name, dict(request.query_params))
            return eng.run_dataset(name, params, p.role, limit=cat.config.powerbi.max_rows, user_id=p.user_id)
        except AccessDenied as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except (DashboardError, SourceError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/powerbi/{filename}")
    async def powerbi_data(filename: str, request: Request):
        p = require_basic(request)
        name, _, ext = filename.rpartition(".")
        if ext not in ("csv", "json"):
            raise HTTPException(status_code=404)
        r = await asyncio.to_thread(_powerbi_dataset, name, request, p)
        headers = {"Cache-Control": "no-store"}
        if r.truncated:
            headers["X-Bizagent-Truncated"] = "1"
        if ext == "csv":
            return PlainTextResponse("﻿" + to_csv(r), media_type="text/csv; charset=utf-8", headers=headers)
        return Response(to_json(r), media_type="application/json", headers=headers)

    @app.get("/powerbi", response_class=HTMLResponse)
    async def powerbi_page(request: Request):
        p = require_page(request)
        if isinstance(p, RedirectResponse):
            return p
        cat = need_catalog()
        if not cat.config.powerbi.enabled:
            return _page("Power BI", "<p>Chưa bật: đặt <code>powerbi.enabled: true</code> trong dashboards.yaml.</p>")
        base = (cat.config.public_url or str(request.base_url)).rstrip("/")
        blocks = []
        for name in cat.powerbi_datasets():
            ds = cat.dataset(name)
            if ds.source not in service.ws.sources_for(p.user_id, p.role):
                continue
            url = f"{base}/powerbi/{name}.csv"
            try:
                eng = service.dashboard_engine
                sample = await asyncio.to_thread(eng.run_dataset, name, dataset_params(eng, name, {}), p.role,
                                                 limit=1000, user_id=p.user_id)
                code = html.escape(m_code(url, column_types(sample)))
            except Exception as e:
                code = html.escape(f"// Lỗi khi đọc dataset: {e}")
            blocks.append(f'<section class="card" style="margin-bottom:14px"><h2>{html.escape(name)}</h2>'
                          f'<div class="note">{html.escape(ds.description)}</div>'
                          f'<p><code>{html.escape(url)}</code> · <a href="/powerbi/{name}.json">JSON</a></p>'
                          f'<details><summary>Mã Power Query (M)</summary><pre style="white-space:pre-wrap">{code}'
                          "</pre></details></section>")
        guide = """<section class="card" style="margin-bottom:14px"><h2>Kết nối từ Power BI Desktop</h2><ol>
<li>Get data → <b>Web</b> → dán URL CSV bên dưới (hoặc Blank query → Advanced Editor → dán mã M).</li>
<li>Xác thực: <b>Basic</b> — tên và mật khẩu tài khoản web; hoặc tên bất kỳ và mật khẩu = API token.</li>
<li>Thêm <code>?period=tháng này</code> (hoặc 90 ngày qua, năm nay...) vào URL để đổi khoảng thời gian.</li>
<li>Publish lên Power BI Service → Semantic model → Settings → Data source credentials (Basic) →
 bật Scheduled refresh. Server cần có địa chỉ HTTPS truy cập được từ Internet (public_url).</li></ol></section>"""
        return _page("Power BI", guide + ("".join(blocks) or "<p>Chưa có dataset nào cho Power BI.</p>"))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app


def check_bind_safety(host: str, has_web_users: bool) -> None:
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    if not os.environ.get("BIZAGENT_API_TOKEN") and not has_web_users:
        raise SystemExit(
            f"Từ chối nghe trên {host} khi chưa đặt BIZAGENT_API_TOKEN và chưa có tài khoản web trong users.yaml. "
            "Đặt token trong .env hoặc chỉ chạy trên 127.0.0.1."
        )
    if not os.environ.get("BIZAGENT_SECRET_KEY"):
        raise SystemExit("Nghe ngoài 127.0.0.1 cần BIZAGENT_SECRET_KEY (khoá ký phiên đăng nhập) trong .env")
