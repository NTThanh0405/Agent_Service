"""Đăng nhập cho trang web (dashboard, Power BI, chat web).

Cách xác thực được chấp nhận:
1. Cookie phiên sau khi đăng nhập bằng tài khoản web (users.yaml: id "web:<ten>", password_hash).
2. Link đăng nhập gửi qua Telegram (/login) — có hạn, đổi thành cookie phiên.
3. Header "Authorization: Bearer <BIZAGENT_API_TOKEN>" (ứng dụng khác gọi API).
4. HTTP Basic (Power BI, Excel): tên + mật khẩu tài khoản web, hoặc mật khẩu = BIZAGENT_API_TOKEN.
5. Máy cá nhân (chưa đặt token, chưa có tài khoản web, truy cập từ 127.0.0.1): tự vào với role BIZAGENT_API_ROLE.

Role luôn tra lại từ users.yaml ở mỗi request, nên xoá người dùng là mất quyền ngay.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass

from bizagent.workspace import Workspace

log = logging.getLogger(__name__)
SESSION_COOKIE = "bz_session"
SESSION_DAYS = 30
LINK_HOURS = 24
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


# ----------------------------------------------------------------------------- mật khẩu
def hash_password(password: str, *, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    if len(password) < 8:
        raise ValueError("Mật khẩu cần ít nhất 8 ký tự")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


# ----------------------------------------------------------------------------- token ký HMAC
class Signer:
    def __init__(self, secret: str):
        self.key = secret.encode()

    def sign(self, payload: dict, ttl_seconds: int) -> str:
        body = {**payload, "exp": int(time.time()) + ttl_seconds}
        raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(hmac.new(self.key, raw, hashlib.sha256).digest()).rstrip(b"=")
        return f"{raw.decode()}.{sig.decode()}"

    def verify(self, token: str) -> dict | None:
        try:
            raw, sig = token.encode().split(b".")
            want = base64.urlsafe_b64encode(hmac.new(self.key, raw, hashlib.sha256).digest()).rstrip(b"=")
            if not hmac.compare_digest(sig, want):
                return None
            body = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        return body if body.get("exp", 0) >= time.time() else None


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str


class WebAuth:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.api_token = os.environ.get("BIZAGENT_API_TOKEN", "")
        self.api_role = os.environ.get("BIZAGENT_API_ROLE", "admin")
        secret = os.environ.get("BIZAGENT_SECRET_KEY", "")
        if not secret:
            secret = secrets.token_urlsafe(32)
            log.warning("Chưa đặt BIZAGENT_SECRET_KEY: phiên đăng nhập web sẽ mất khi khởi động lại")
        self.signer = Signer(secret)

    @property
    def has_web_users(self) -> bool:
        return any(u.password_hash for u in self.ws.all_users())

    def local_mode(self) -> bool:
        return not self.api_token and not self.has_web_users

    def _role(self, user_id: str) -> Principal | None:
        role = self.ws.role_of(user_id)
        return Principal(user_id, role) if role else None

    def check_password(self, username: str, password: str) -> Principal | None:
        for u in self.ws.all_users():
            if u.id == f"web:{username}" and u.password_hash and verify_password(password, u.password_hash):
                return Principal(u.id, u.role)
        return None

    def authenticate(self, *, cookie: str | None, authorization: str, client_host: str | None) -> Principal | None:
        if cookie:
            body = self.signer.verify(cookie)
            if body and body.get("k") == "session":
                p = self._role(body["u"])
                if p:
                    return p
        if authorization.startswith("Bearer ") and self.api_token:
            if hmac.compare_digest(authorization[7:].strip().encode(), self.api_token.encode()):
                return Principal("api:token", self.api_role)
        if authorization.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(authorization[6:]).decode().partition(":")
            except ValueError:
                return None
            if self.api_token and hmac.compare_digest(pw.encode(), self.api_token.encode()):
                return Principal("api:token", self.api_role)
            return self.check_password(user, pw)
        if self.local_mode() and client_host in LOOPBACK:
            return Principal("local", self.api_role)
        return None

    def session_token(self, user_id: str) -> str:
        return self.signer.sign({"k": "session", "u": user_id}, SESSION_DAYS * 86400)

    def login_link_token(self, user_id: str) -> str:
        return self.signer.sign({"k": "link", "u": user_id}, LINK_HOURS * 3600)

    def exchange_link(self, token: str) -> str | None:
        body = self.signer.verify(token)
        if not body or body.get("k") != "link" or not self._role(body["u"]):
            return None
        return self.session_token(body["u"])
