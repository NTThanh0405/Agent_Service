"""Quản lý người dùng và quyền cấp riêng — dùng chung cho lệnh chat (/adduser, /capquyen...) và trang /admin.

- Role trong `admin_roles` (workspace.yaml, mặc định [admin]): thêm/xoá người dùng, cấp mọi nguồn.
- Role trong `grant_roles`: cấp/thu hồi quyền xem nguồn cho người khác, CHỈ các nguồn chính mình xem được.
- Người dùng trong users.yaml không sửa/xoá được bằng lệnh (sửa file); người thêm bằng lệnh lưu trong state DB.
"""

from __future__ import annotations

import re
from datetime import datetime

from bizagent.state import StateStore
from bizagent.workspace import Workspace

USER_ID = re.compile(r"^(telegram|zalo|slack|web|api):[A-Za-z0-9_.@-]{1,100}$")
MAX_HOURS = 24 * 366


class AccessError(Exception):
    pass


def is_admin(ws: Workspace, role: str | None) -> bool:
    return role in ws.info.admin_roles


def can_grant(ws: Workspace, role: str | None) -> bool:
    return role in ws.info.grant_roles or is_admin(ws, role)


def normalize_user_id(raw: str, actor_id: str) -> str:
    """'123' gõ trong Telegram -> 'telegram:123' (cùng kênh với người gõ lệnh)."""
    raw = raw.strip()
    if ":" not in raw and actor_id and ":" in actor_id:
        raw = f"{actor_id.split(':', 1)[0]}:{raw}"
    if not USER_ID.match(raw):
        raise AccessError(f"ID không hợp lệ: {raw} (dạng telegram:123, zalo:..., slack:U..., web:ten)")
    return raw


def add_user(ws: Workspace, store: StateStore, actor: tuple[str, str], user_id: str, role: str, name: str = "",
             password_hash: str | None = None) -> str:
    actor_id, actor_role = actor
    if not is_admin(ws, actor_role):
        raise AccessError("Chỉ quản trị viên được thêm người dùng")
    uid = normalize_user_id(user_id, actor_id)
    if role not in ws.info.roles:
        raise AccessError(f"Role '{role}' không có. Có: {', '.join(ws.info.roles)}")
    if any(u.id == uid for u in ws.users):
        raise AccessError(f"{uid} khai báo trong users.yaml: sửa file đó thay vì dùng lệnh")
    if uid.startswith("web:") and password_hash is None and not any(u.id == uid for u in ws.all_users()):
        raise AccessError("Tài khoản web cần mật khẩu")
    store.upsert_user(uid, role, name, password_hash, created_by=actor_id)
    store.audit(actor_id, "user_add", user=uid, role=role)
    return uid


def remove_user(ws: Workspace, store: StateStore, actor: tuple[str, str], user_id: str) -> str:
    actor_id, actor_role = actor
    if not is_admin(ws, actor_role):
        raise AccessError("Chỉ quản trị viên được xoá người dùng")
    uid = normalize_user_id(user_id, actor_id)
    if uid == actor_id:
        raise AccessError("Không tự xoá chính mình")
    if any(u.id == uid for u in ws.users):
        raise AccessError(f"{uid} khai báo trong users.yaml: xoá trong file đó")
    if not store.delete_user(uid):
        raise AccessError(f"Không có người dùng {uid}")
    store.audit(actor_id, "user_remove", user=uid)
    return uid


def grant(ws: Workspace, store: StateStore, actor: tuple[str, str], user_id: str, source: str,
          hours: float | None = None) -> tuple[str, datetime | None]:
    actor_id, actor_role = actor
    if not can_grant(ws, actor_role):
        raise AccessError("Bạn không có quyền cấp quyền cho người khác")
    uid = normalize_user_id(user_id, actor_id)
    if ws.role_of(uid) is None:
        raise AccessError(f"{uid} chưa là người dùng (thêm bằng /adduser trước)")
    if source not in ws.sources or not ws.sources[source].enabled:
        raise AccessError(f"Không có nguồn '{source}'. Có: {', '.join(n for n, s in ws.sources.items() if s.enabled)}")
    if not is_admin(ws, actor_role) and source not in ws.sources_for(actor_id, actor_role):
        raise AccessError(f"Bạn chỉ cấp được nguồn chính mình xem được; '{source}' không thuộc số đó")
    if hours is not None and not 0 < hours <= MAX_HOURS:
        raise AccessError(f"Số giờ phải trong khoảng 1..{MAX_HOURS}")
    expires = store.add_grant(uid, source, actor_id, hours)
    store.audit(actor_id, "grant", user=uid, source=source, hours=hours)
    return uid, expires


def revoke(ws: Workspace, store: StateStore, actor: tuple[str, str], user_id: str, source: str) -> int:
    actor_id, actor_role = actor
    if not can_grant(ws, actor_role):
        raise AccessError("Bạn không có quyền thu hồi quyền")
    uid = normalize_user_id(user_id, actor_id)
    n = store.revoke_grant(uid, None if source == "all" else source)
    store.audit(actor_id, "revoke", user=uid, source=source, removed=n)
    return n


def describe_user(ws: Workspace, store: StateStore, user_id: str) -> str:
    role = ws.role_of(user_id)
    if role is None:
        return f"{user_id}: chưa là người dùng"
    by_role = ws.sources_for_role(role)
    lines = [f"{user_id} — role {role}", f"Nguồn theo role: {', '.join(by_role) or '(không có)'}"]
    grants = store.list_grants(user_id)
    if grants:
        lines.append("Cấp riêng:")
        for g in grants:
            until = f"hết hạn {g['expires_at']:%d/%m %H:%M} UTC" if g["expires_at"] else "không hết hạn"
            lines.append(f"  • {g['source']} ({until}, cấp bởi {g['granted_by']})")
    return "\n".join(lines)


def list_users_text(ws: Workspace) -> str:
    rows = []
    file_ids = {u.id for u in ws.users}
    for u in ws.all_users():
        src = "file" if u.id in file_ids else "lệnh"
        rows.append(f"• {u.id} — {u.role}{' — ' + u.name if u.name else ''} ({src})")
    return "Người dùng:\n" + ("\n".join(rows) or "(chưa có)")
