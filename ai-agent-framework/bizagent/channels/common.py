"""Phần dùng chung cho các kênh chat (Telegram, Zalo...): cuộc trò chuyện lưu DB, lệnh cố định, link đăng nhập."""

from __future__ import annotations

import asyncio

from bizagent.agent.service import AgentService

HELP = ("Lệnh: /new cuộc trò chuyện mới · /sessions danh sách cuộc trò chuyện · /chon <số> chuyển cuộc · "
        "/login link mở dashboard · /myid xem ID")


def split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + ([text] if text else [])


def login_message(service: AgentService, user_id: str, role: str) -> str:
    cat = service.catalog
    if cat is None or not cat.config.public_url:
        return "Chưa cấu hình địa chỉ dashboard (dashboards.yaml: public_url)."
    from bizagent.webauth import LINK_HOURS, WebAuth

    token = WebAuth(service.ws).login_link_token(user_id)
    url = f"{cat.config.public_url.rstrip('/')}/login/link?t={token}"
    names = ", ".join(cat.dashboards[s].title for s in cat.visible(role, user_id)) or "(chưa có)"
    return (f"Link đăng nhập dashboard (dùng trong {LINK_HOURS} giờ, không chia sẻ cho người khác):\n{url}\n\n"
            f"Dashboard bạn xem được: {names}")


async def current_thread(service: AgentService, user_id: str) -> str:
    tid = await asyncio.to_thread(service.store.current_thread, user_id)
    return tid or await asyncio.to_thread(service.store.new_thread, user_id)


async def handle_command(service: AgentService, user_id: str, role: str, text: str) -> str | None:
    """Lệnh cố định (xử lý bằng code, không gọi AI). Trả None nếu không phải lệnh."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0].split("@")[0].lower()
    store = service.store
    if cmd in ("/start", "/help"):
        title = service.ws.info.display_name or service.ws.name
        from bizagent.access import can_grant

        extra = f"\n{ADMIN_HELP}" if can_grant(service.ws, role) else ""
        return f"Xin chào! Tôi là trợ lý dữ liệu của {title}. Hãy hỏi bằng tiếng Việt tự nhiên.\n{HELP}{extra}"
    if cmd == "/new":
        await asyncio.to_thread(store.new_thread, user_id)
        return "Đã bắt đầu cuộc trò chuyện mới."
    if cmd == "/sessions":
        rows = await asyncio.to_thread(store.list_threads, user_id)
        if not rows:
            return "Chưa có cuộc trò chuyện nào."
        lines = [f"{i}. {r['title'] or '(chưa có câu hỏi)'} — {r['updated_at']:%d/%m %H:%M}"
                 + (" ← đang dùng" if i == 1 else "") for i, r in enumerate(rows[:10], 1)]
        return "Cuộc trò chuyện gần đây (UTC):\n" + "\n".join(lines) + "\n\nGõ /chon <số> để chuyển."
    if cmd == "/chon":
        rows = await asyncio.to_thread(store.list_threads, user_id)
        if len(parts) < 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= min(len(rows), 10):
            return "Cách dùng: /chon <số> (xem số bằng /sessions)"
        r = rows[int(parts[1]) - 1]
        await asyncio.to_thread(store.touch, user_id, r["thread_id"])
        return f"Đã chuyển sang: {r['title'] or '(chưa có câu hỏi)'}"
    if cmd == "/login":
        return login_message(service, user_id, role)
    if cmd in ADMIN_COMMANDS:
        return await asyncio.to_thread(_admin_command, service, user_id, role, cmd, parts[1:])
    return None


ADMIN_COMMANDS = {"/users", "/adduser", "/removeuser", "/capquyen", "/thuhoi", "/quyen"}
ADMIN_HELP = ("Quản trị: /users · /adduser <id> <role> [tên] · /removeuser <id> · "
              "/capquyen <id> <nguồn> [số giờ] · /thuhoi <id> <nguồn|all> · /quyen [id]")


def _admin_command(service: AgentService, user_id: str, role: str, cmd: str, args: list[str]) -> str:
    from bizagent import access

    ws, store, actor = service.ws, service.store, (user_id, role)
    try:
        if cmd == "/quyen":
            target = access.normalize_user_id(args[0], user_id) if args else user_id
            if target != user_id and not access.can_grant(ws, role):
                raise access.AccessError("Bạn chỉ xem được quyền của chính mình")
            return access.describe_user(ws, store, target)
        if cmd == "/users":
            if not access.can_grant(ws, role):
                raise access.AccessError("Lệnh dành cho quản trị viên")
            return access.list_users_text(ws)
        if cmd == "/adduser":
            if len(args) < 2:
                return "Cách dùng: /adduser <id> <role> [tên]"
            uid = access.add_user(ws, store, actor, args[0], args[1], " ".join(args[2:]))
            return f"Đã thêm {uid} với role {args[1]}. Người đó nhắn /start để bắt đầu."
        if cmd == "/removeuser":
            if not args:
                return "Cách dùng: /removeuser <id>"
            return f"Đã xoá {access.remove_user(ws, store, actor, args[0])}"
        if cmd == "/capquyen":
            if len(args) < 2:
                return "Cách dùng: /capquyen <id> <nguồn> [số giờ]"
            hours = float(args[2]) if len(args) > 2 else None
            uid, exp = access.grant(ws, store, actor, args[0], args[1], hours)
            until = f"đến {exp:%d/%m/%Y %H:%M} UTC" if exp else "không thời hạn"
            return f"Đã cấp nguồn {args[1]} cho {uid} ({until})"
        if cmd == "/thuhoi":
            if len(args) < 2:
                return "Cách dùng: /thuhoi <id> <nguồn|all>"
            n = access.revoke(ws, store, actor, args[0], args[1])
            return f"Đã thu hồi {n} quyền"
    except access.AccessError as e:
        return f"Không thực hiện được: {e}"
    except ValueError:
        return "Số giờ không hợp lệ"
    return ADMIN_HELP
