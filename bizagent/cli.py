"""Dòng lệnh `bizagent`.

    bizagent init <ten>            tạo workspace mới từ workspaces/_template
    bizagent seed-demo             tạo database SQLite mẫu cho workspace demo
    bizagent check   [-w ten]      kiểm tra cấu hình, kết nối nguồn dữ liệu, model AI
    bizagent prompt  [-w ten] [--role admin]   in system prompt đã ghép (để soát prompt)
    bizagent chat    [-w ten] [--role admin]   chat trong terminal
    bizagent sync    [-w ten] [job...] [--status] [--due]   đồng bộ dữ liệu về kho DuckDB
    bizagent run     [-w ten] [--host --port]  CHẠY THẬT: web + bot Telegram + tác vụ nền trong một tiến trình
    bizagent serve   [-w ten] [--host --port]  chỉ web: chat, dashboard sống, Power BI, /admin (+ tác vụ nền)
    bizagent mcp-server [-w ten] [--role manager]  MCP server (stdio) cho Claude Desktop / Cursor...
    bizagent audit   [-w ten] [--user id] [--limit 50]  xem audit log
    bizagent report <ten> [-w ten] [--to id] [--preview]  xem trước / gửi ngay báo cáo định kỳ
    bizagent hash-password <ten>   tạo mật khẩu cho tài khoản web
    bizagent powerbi-export [-w ten]           xuất file Parquet/CSV cho Power BI ngay
    bizagent powerbi-push [-w ten] [--check]   đẩy dữ liệu lên Power BI Service ngay (powerbi.push)
    bizagent telegram [-w ten]     bot Telegram

Workspace: -w <ten hoặc đường dẫn>, hoặc biến BIZAGENT_WORKSPACE, mặc định "demo".
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from bizagent import __version__


def _workspaces_dir() -> Path:
    return Path(os.environ.get("BIZAGENT_WORKSPACES_DIR") or "workspaces")


def _service(args):
    from bizagent.agent.service import AgentService

    return AgentService.from_workspace(args.workspace)


# ---------------------------------------------------------------------------- lệnh
def cmd_init(args) -> int:
    root = _workspaces_dir()
    src, dst = root / args.template, root / args.name
    if not (src / "workspace.yaml").exists():
        print(f"Không thấy template {src}")
        return 1
    if dst.exists():
        print(f"{dst} đã tồn tại")
        return 1
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("data", ".env", "users.yaml"))
    ws_file = dst / "workspace.yaml"
    ws_file.write_text(ws_file.read_text(encoding="utf-8").replace("name: my_company", f"name: {args.name}"),
                       encoding="utf-8")
    print(f"Đã tạo {dst}. Bước tiếp: sửa workspace.yaml, sources.yaml, agents.yaml, knowledge/*.md "
          f"rồi chạy: bizagent check -w {args.name}")
    return 0


def cmd_seed_demo(args) -> int:
    from bizagent.demo import create_demo_db, write_targets_csv

    root = _workspaces_dir() / "demo"
    print(f"Đã tạo {create_demo_db(root / 'data' / 'demo.db', days=args.days)}")
    print(f"Đã tạo {write_targets_csv(root / 'files' / 'muc_tieu.csv')}")
    print("Đồng bộ về kho: bizagent sync")
    return 0


def cmd_check(args) -> int:
    from bizagent.llm.registry import check_provider_ready, resolve_model
    from bizagent.prompts.engine import PromptEngine
    from bizagent.sources.registry import SourceRegistry
    from bizagent.workspace import load_workspace

    ok = True
    ws = load_workspace(args.workspace)
    print(f"Workspace: {ws.name} ({ws.path})")
    rm = resolve_model(ws.agents)
    problems = check_provider_ready(rm)
    print(f"Model: {rm.label} -> {'sẵn sàng' if not problems else '; '.join(problems)}")
    ok &= not problems

    reg = SourceRegistry(ws)
    for name, err in reg.errors.items():
        print(f"Nguồn {name}: LỖI cấu hình: {err}")
        ok = False
    for name, src in reg.sources.items():
        try:
            src.health()
            tables = src.list_tables()
            print(f"Nguồn {name} ({src.kind}, {src.dialect}): OK, {len(tables)} bảng được phép")
        except Exception as e:
            print(f"Nguồn {name}: LỖI: {e}")
            ok = False
    try:
        from bizagent.sync import build_runner

        runner = build_runner(ws, reg)
        if runner:
            print(f"Đồng bộ: {len(runner.config.jobs)} job -> kho {runner.config.warehouse}"
                  f"{' (chạy trong server)' if runner.config.run_in_server else ''}")
    except Exception as e:
        print(f"Đồng bộ: LỖI cấu hình: {e}")
        ok = False
    try:
        from bizagent.dashboards.config import DashboardCatalog

        cat = DashboardCatalog(ws)
        pbi = f"bật, {len(cat.powerbi_datasets())} dataset" if cat.config.powerbi.enabled else "tắt"
        live = "bật" if cat.config.live.enabled else "tắt"
        print(f"Dashboard sống: {live}, {len(cat.dashboards)} dashboard; Power BI: {pbi}")
        for slug, err in cat.errors.items():
            print(f"Dashboard {slug}: LỖI: {err}")
            ok = False
        if cat.config.powerbi.enabled and cat.config.powerbi.push:
            ok &= _check_powerbi_push(ws, reg, cat)
    except Exception as e:
        print(f"Dashboard: LỖI cấu hình: {e}")
        ok = False
    reg.close()

    if any(s.type == "mcp" for s in ws.sources.values()):
        from bizagent.sources.mcp import load_mcp_tools

        reg2 = SourceRegistry(ws)
        tools, errs = asyncio.run(load_mcp_tools(reg2, lambda *a, **k: None))
        print(f"MCP: {len(tools)} tool được phép")
        for k, v in errs.items():
            print(f"Nguồn MCP {k}: LỖI: {v}")
        ok &= not errs
        reg2.close()
    from bizagent.channels.zalo import load_zalo_config, missing_env

    zcfg = load_zalo_config(ws)
    if zcfg.enabled:
        missing = missing_env(zcfg)
        print(f"Kênh Zalo: {'thiếu ' + ', '.join(missing) if missing else 'đủ cấu hình'}")
        ok &= not missing
    try:                                              # báo cáo có người nhận email -> cần SMTP
        from bizagent.reports import load_reports, smtp_missing_env

        users = {u.id: u for u in ws.users}
        email_reports = [n for n, r in load_reports(ws).reports.items() if r.enabled and any(
            x.startswith("email:") or (x.startswith("role:") and any(
                u.role == x[5:] and u.id.startswith("email:") for u in users.values()))
            for x in r.recipients)]
        if email_reports:
            missing = smtp_missing_env()
            print(f"Email (báo cáo {', '.join(email_reports)}): "
                  f"{'thiếu ' + ', '.join(missing) if missing else 'đủ cấu hình SMTP'}")
            ok &= not missing
    except Exception as e:
        print(f"Báo cáo: LỖI cấu hình: {e}")
        ok = False
    for role in ws.info.roles:
        print(f"Role {role}: nguồn {ws.sources_for_role(role) or '(không có)'}")
    try:
        PromptEngine(ws).render("system.md.j2", role="admin", sources=[])
        print("Prompt: OK")
    except Exception as e:
        print(f"Prompt: LỖI: {e}")
        ok = False
    print("=> Tất cả OK" if ok else "=> Còn lỗi, xem các dòng trên")
    return 0 if ok else 1


def _check_powerbi_push(ws, reg, cat) -> bool:
    from bizagent.dashboards.engine import DashboardEngine
    from bizagent.dashboards.powerbi_push import PowerBIPusher, missing_env

    push = cat.config.powerbi.push
    names = cat.powerbi_push_datasets()
    head = f"Power BI push ({push.mode}, mỗi {push.every}, {len(names)} dataset)"
    missing = missing_env()
    if missing:
        print(f"{head}: thiếu {', '.join(missing)} trong .env")
        return False
    if not names:
        print(f"{head}: LỖI: chưa có dataset nào (powerbi.push.datasets hoặc powerbi.datasets)")
        return False
    try:
        pusher = PowerBIPusher(DashboardEngine(reg, cat))
        try:
            status = pusher.check()
        finally:
            pusher.close()
    except Exception as e:
        print(f"{head}: LỖI: {e}")
        return False
    print(f"{head}: kết nối workspace {push.workspace_id} OK, {status}")
    return True


def cmd_prompt(args) -> int:
    from bizagent.prompts.engine import PromptEngine
    from bizagent.sources.registry import SourceRegistry
    from bizagent.workspace import load_workspace

    ws = load_workspace(args.workspace)
    reg = SourceRegistry(ws)
    sources = [{"name": s.name, "kind": s.kind, "dialect": s.dialect, "description": s.description}
               for s in reg.for_role(args.role)]
    print(PromptEngine(ws).render("system.md.j2", role=args.role, sources=sources))
    reg.close()
    return 0


def cmd_chat(args) -> int:
    from bizagent.channels.common import current_thread, handle_command

    service = _service(args)
    user = "cli:local"
    print(f"Workspace {service.ws.name} · model {service.model_ref.label} · role {args.role}. "
          "Lệnh: /new, /sessions, /chon <số>, /exit.")

    async def loop() -> None:
        try:
            while True:
                try:
                    text = (await asyncio.to_thread(input, "\nBạn: ")).strip()
                except (EOFError, KeyboardInterrupt):
                    return
                if not text:
                    continue
                if text in ("/exit", "/quit"):
                    return
                answer = await handle_command(service, user, args.role, text)
                if answer is None:
                    thread = await current_thread(service, user)
                    answer = await service.ask(text, user_id=user, role=args.role, thread_id=thread)
                print("\nTrợ lý:", answer)
        finally:
            await service.aclose()

    _run_async(loop())
    return 0


def _run_async(coro) -> None:
    """Windows: dùng SelectorEventLoop (psycopg async không chạy trên ProactorEventLoop mặc định)."""
    if sys.platform == "win32":
        asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(coro)


def cmd_serve(args) -> int:
    import uvicorn

    from bizagent.channels.rest import check_bind_safety, create_app

    service = _service(args)
    check_bind_safety(args.host, any(u.password_hash for u in service.ws.all_users()))
    app = create_app(service)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level.lower(),
                            server_header=False, workers=1, proxy_headers=args.behind_proxy,
                            forwarded_allow_ips="*" if args.behind_proxy else None)
    server = uvicorn.Server(config)
    with_bot = getattr(args, "with_telegram", False)

    async def main() -> None:
        bot_task = None
        if with_bot:
            from bizagent.channels.telegram import run_telegram

            bot_task = asyncio.create_task(run_telegram(service, embedded=True))

            def _bot_done(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception() is not None:
                    logging.getLogger("bizagent.telegram").error(
                        "Bot Telegram dừng vì lỗi (web vẫn chạy): %s — kiểm tra TELEGRAM_BOT_TOKEN rồi khởi động lại",
                        t.exception())
            bot_task.add_done_callback(_bot_done)
        try:
            await server.serve()
        finally:
            if bot_task:
                bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await bot_task

    try:
        _run_async(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_run(args) -> int:
    """Một tiến trình cho tất cả: web (chat, dashboard, Power BI, /admin, webhook Zalo/Slack) + bot Telegram
    (nếu có TELEGRAM_BOT_TOKEN) + tác vụ nền (đồng bộ, báo cáo, xuất/đẩy Power BI).
    Dùng cho Windows Service và Docker."""
    args.with_telegram = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and not args.no_telegram
    if args.with_telegram:
        try:
            import aiogram  # noqa: F401
        except ImportError:
            print('Có TELEGRAM_BOT_TOKEN nhưng chưa cài aiogram: pip install "bizagent[telegram]"', file=sys.stderr)
            return 1
    logging.getLogger(__name__).info("bizagent run: web %s:%s%s", args.host, args.port,
                                     " + bot Telegram" if args.with_telegram else "")
    return cmd_serve(args)


def cmd_sync(args) -> int:
    from bizagent.sources.registry import SourceRegistry
    from bizagent.sync import build_runner
    from bizagent.workspace import load_workspace

    ws = load_workspace(args.workspace)
    reg = SourceRegistry(ws)
    try:
        runner = build_runner(ws, reg)
        if runner is None:
            print(f"Workspace {ws.name} chưa có sync.yaml (hoặc chưa có job)")
            return 1
        if args.status:
            state = runner.warehouse.sync_state()
            for name, job in runner.config.jobs.items():
                s = state.get(name)
                if not s:
                    print(f"{name:24} -> {job.table:24} chưa chạy")
                    continue
                last = s["last_success"].astimezone().strftime("%Y-%m-%d %H:%M") if s["last_success"] else "-"
                err = f"  lỗi: {s['error']}" if s["status"] != "ok" else ""
                print(f"{name:24} -> {s['table_name']:24} {s['status']:5} {s['rows'] or 0:>8} dòng  "
                      f"thành công lần cuối {last}{err}")
            return 0
        names = args.jobs or (runner.due_jobs() if args.due else list(runner.config.jobs))
        unknown = [n for n in names if n not in runner.config.jobs]
        if unknown:
            print(f"Không có job: {unknown}. Có: {list(runner.config.jobs)}")
            return 1
        failed = 0
        for name in names:
            try:
                print(f"{name}: OK, {runner.run_job(name)} dòng")
            except Exception as e:
                print(f"{name}: LỖI: {e}")
                failed += 1
        return 1 if failed else 0
    finally:
        reg.close()


def cmd_hash_password(args) -> int:
    import getpass

    from bizagent.webauth import hash_password

    pw = getpass.getpass("Mật khẩu (ít nhất 8 ký tự): ")
    if pw != getpass.getpass("Nhập lại: "):
        print("Hai lần nhập không khớp")
        return 1
    try:
        h = hash_password(pw)
    except ValueError as e:
        print(e)
        return 1
    print("Thêm vào users.yaml của workspace:\n")
    print(f'  - id: "web:{args.username}"\n    name: "{args.username}"\n    role: manager\n    password_hash: "{h}"')
    return 0


def cmd_powerbi_export(args) -> int:
    from bizagent.dashboards.config import DashboardCatalog
    from bizagent.dashboards.engine import DashboardEngine
    from bizagent.dashboards.powerbi import PowerBIExporter
    from bizagent.sources.registry import SourceRegistry
    from bizagent.workspace import load_workspace

    ws = load_workspace(args.workspace)
    reg = SourceRegistry(ws)
    try:
        exporter = PowerBIExporter(DashboardEngine(reg, DashboardCatalog(ws)))
        results = exporter.export_all()
        for name, status in results.items():
            print(f"{name}: {status}")
        print(f"Thư mục: {exporter.dir}")
        return 0 if all(s.startswith("ok") for s in results.values()) else 1
    finally:
        reg.close()


def cmd_powerbi_push(args) -> int:
    from bizagent.dashboards.config import DashboardCatalog
    from bizagent.dashboards.engine import DashboardEngine
    from bizagent.dashboards.powerbi_push import PowerBIPusher
    from bizagent.sources.registry import SourceRegistry
    from bizagent.workspace import load_workspace

    ws = load_workspace(args.workspace)
    reg = SourceRegistry(ws)
    try:
        cat = DashboardCatalog(ws)
        if not cat.config.powerbi.enabled or cat.config.powerbi.push is None:
            print("Chưa bật: cần powerbi.enabled: true và powerbi.push trong dashboards.yaml (xem docs/DASHBOARDS.md)")
            return 1
        pusher = PowerBIPusher(DashboardEngine(reg, cat))
        try:
            if args.check:
                print(f"Power BI: {pusher.check()}")
                return 0
            results = pusher.push_all()
        finally:
            pusher.close()
        for name, status in results.items():
            print(f"{name}: {status}")
        print(f"Model: {pusher.model} · workspace {pusher.cfg.workspace_id}")
        return 0 if results and all(s.startswith("ok") for s in results.values()) else 1
    except Exception as e:
        print(f"Lỗi: {e}")
        return 1
    finally:
        reg.close()


def cmd_mcp_server(args) -> int:
    from bizagent.mcp_server import build_server
    from bizagent.workspace import load_workspace

    try:
        mcp, tools = build_server(load_workspace(args.workspace), args.role)
    except ImportError:
        print('Cần gói mcp: pip install "bizagent[mcp]"', file=sys.stderr)
        return 1
    try:
        mcp.run()               # stdio: KHÔNG in gì ra stdout ngoài giao thức MCP
    finally:
        tools.close()
    return 0


def cmd_audit(args) -> int:
    from bizagent.state import StateStore
    from bizagent.workspace import load_workspace

    store = StateStore.for_workspace(load_workspace(args.workspace))
    for r in reversed(store.audit_rows(limit=args.limit, user_id=args.user)):
        detail = json.dumps(r["detail"], ensure_ascii=False)
        print(f"{r['ts']:%Y-%m-%d %H:%M:%S} UTC  {r['user_id']:24} {r['kind']:16} {detail[:160]}")
    store.close()
    return 0


def cmd_report(args) -> int:
    from bizagent.reports import ReportRunner

    service = _service(args)

    async def run() -> int:
        try:
            runner = ReportRunner(service)
            if args.name not in runner.config.reports:
                print(f"Không có báo cáo '{args.name}'. Có: {list(runner.config.reports)}")
                return 1
            targets = [args.to] if args.to else runner.recipients(runner.config.reports[args.name])
            if args.preview:
                for uid in targets or ["(chưa có người nhận)"]:
                    text = await runner.build(args.name, uid) if ":" in uid else None
                    print(f"===== {uid}\n{text or '(không có mục nào được xem)'}\n")
                return 0
            for uid, status in (await runner.send_now(args.name, targets)).items():
                print(f"{uid}: {status}")
            return 0
        finally:
            await service.aclose()

    result: list[int] = []

    async def wrapper() -> None:
        result.append(await run())

    _run_async(wrapper())
    return result[0]


def cmd_telegram(args) -> int:
    from bizagent.channels.telegram import run_telegram

    _run_async(run_telegram(_service(args)))
    return 0


# ---------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bizagent", description="Khung AI agent phân tích dữ liệu doanh nghiệp")
    p.add_argument("--version", action="version", version=f"bizagent {__version__}")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def ws_arg(sp):
        sp.add_argument("-w", "--workspace", default=None, help="tên hoặc đường dẫn workspace")

    sp = sub.add_parser("init", help="tạo workspace mới")
    sp.add_argument("name")
    sp.add_argument("--template", default="_template")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("seed-demo", help="tạo database mẫu cho workspace demo")
    sp.add_argument("--days", type=int, default=180)
    sp.set_defaults(func=cmd_seed_demo)

    for name, func, help_ in (("check", cmd_check, "kiểm tra cấu hình"), ("telegram", cmd_telegram, "bot Telegram")):
        sp = sub.add_parser(name, help=help_)
        ws_arg(sp)
        sp.set_defaults(func=func)

    for name, func, help_ in (("prompt", cmd_prompt, "in system prompt"), ("chat", cmd_chat, "chat trong terminal")):
        sp = sub.add_parser(name, help=help_)
        ws_arg(sp)
        sp.add_argument("--role", default="admin")
        sp.set_defaults(func=func)

    sp = sub.add_parser("sync", help="đồng bộ dữ liệu về kho (sync.yaml)")
    ws_arg(sp)
    sp.add_argument("jobs", nargs="*", help="tên job; bỏ trống = mọi job")
    sp.add_argument("--status", action="store_true", help="xem trạng thái đồng bộ")
    sp.add_argument("--due", action="store_true", help="chỉ chạy job đã tới lịch")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("hash-password", help="tạo password_hash cho tài khoản web")
    sp.add_argument("username")
    sp.set_defaults(func=cmd_hash_password)

    sp = sub.add_parser("powerbi-export", help="xuất file Parquet/CSV cho Power BI ngay")
    ws_arg(sp)
    sp.set_defaults(func=cmd_powerbi_export)

    sp = sub.add_parser("powerbi-push", help="đẩy dữ liệu lên Power BI Service ngay (powerbi.push)")
    ws_arg(sp)
    sp.add_argument("--check", action="store_true", help="chỉ kiểm tra token và quyền vào workspace, không đẩy")
    sp.set_defaults(func=cmd_powerbi_push)

    sp = sub.add_parser("mcp-server", help="mở dữ liệu workspace thành MCP server (stdio) cho Claude Desktop...")
    ws_arg(sp)
    sp.add_argument("--role", default=os.environ.get("BIZAGENT_MCP_ROLE", "manager"))
    sp.set_defaults(func=cmd_mcp_server)

    sp = sub.add_parser("report", help="xem trước hoặc gửi ngay một báo cáo định kỳ (reports.yaml)")
    ws_arg(sp)
    sp.add_argument("name")
    sp.add_argument("--to", default=None, help="chỉ gửi/xem cho một người (vd telegram:123)")
    sp.add_argument("--preview", action="store_true", help="chỉ in nội dung, không gửi")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("audit", help="xem audit log")
    ws_arg(sp)
    sp.add_argument("--user", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_audit)

    for name, func, help_ in (("serve", cmd_serve, "web: chat, dashboard, Power BI, /admin"),
                              ("run", cmd_run, "chạy tất cả trong một tiến trình: web + bot Telegram + tác vụ nền")):
        sp = sub.add_parser(name, help=help_)
        ws_arg(sp)
        sp.add_argument("--host", default=os.environ.get("BIZAGENT_HOST", "127.0.0.1"))
        sp.add_argument("--port", type=int, default=int(os.environ.get("BIZAGENT_PORT", "8000")))
        sp.add_argument("--behind-proxy", action="store_true",
                        default=os.environ.get("BIZAGENT_BEHIND_PROXY", "") == "1",
                        help="tin header X-Forwarded-* (khi đứng sau nginx/Caddy/Cloudflare Tunnel)")
        if name == "run":
            sp.add_argument("--no-telegram", action="store_true", help="không chạy bot Telegram")
        sp.set_defaults(func=func)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env", override=False)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")       # tiếng Việt trên console Windows
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from bizagent.workspace import WorkspaceError

    try:
        return args.func(args)
    except WorkspaceError as e:
        print(f"Lỗi cấu hình: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
