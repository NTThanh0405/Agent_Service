"""Đăng nhập cho trang web (dashboard, Power BI, chat web).

Cách xác thực được chấp nhận:
1. Cookie phiên sau khi đăng nhập bằng tài khoản web (users.yaml: id "web:<ten>", password_hash).
2. Link đăng nhập gửi qua Telegram (/login) — có hạn, dùng MỘT lần, đổi thành cookie phiên.
3. Header "Authorization: Bearer <BIZAGENT_API_TOKEN>" (ứng dụng khác gọi API).
4. HTTP Basic (Power BI, Excel): tên + mật khẩu tài khoản web, hoặc mật khẩu = BIZAGENT_API_TOKEN.
5. Máy cá nhân (chưa đặt token, chưa có tài khoản web, truy cập thẳng từ 127.0.0.1, không qua proxy/tunnel):
   tự vào với role BIZAGENT_API_ROLE.

Role luôn tra lại từ users.yaml ở mỗi request, nên xoá người dùng là mất quyền ngay.
Đăng nhập sai quá nhiều lần thì bị tạm khoá (LoginLimiter). Đăng xuất thu hồi mọi phiên của người đó.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass

from bizagent.workspace import Workspace

log = logging.getLogger(__name__)
SESSION_COOKIE = "bz_session"
SESSION_DAYS = 30
LINK_HOURS = 24
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
# Header do reverse proxy / tunnel thêm vào: có header này thì request không thật sự đến từ máy cá nhân.
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "true-client-ip")
# Khoá ký dự phòng khi chưa đặt BIZAGENT_SECRET_KEY: sinh MỘT lần cho cả tiến trình, để link /login do bot tạo
# (một WebAuth khác) vẫn khớp với web server. Khởi động lại thì phiên cũ mất hiệu lực.
_FALLBACK_SECRET = secrets.token_urlsafe(32)


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


# ----------------------------------------------------------------------------- chống dò mật khẩu
class LoginLimiter:
    """Quá `max_failures` lần sai trong `window` giây (theo tên đăng nhập hoặc theo IP) thì khoá tới khi hết cửa sổ.
    Lưu trong bộ nhớ của tiến trình web (bizagent chạy một tiến trình web)."""

    def __init__(self, max_failures: int = 10, window: int = 900):
        self.max_failures = max_failures
        self.window = window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        items = [t for t in self._fails.get(key, []) if now - t < self.window]
        if items:
            self._fails[key] = items
        else:
            self._fails.pop(key, None)
        return items

    def blocked(self, *keys: str) -> bool:
        now = time.time()
        with self._lock:
            return any(len(self._recent(k, now)) >= self.max_failures for k in keys if k)

    def failed(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                if k:
                    self._recent(k, now)
                    self._fails.setdefault(k, []).append(now)
            if len(self._fails) > 10_000:                 # chống phình bộ nhớ khi bị dò từ rất nhiều IP
                for k in list(self._fails)[:5_000]:
                    self._fails.pop(k, None)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


# ----------------------------------------------------------------------------- xác thực
class WebAuth:
    def __init__(self, ws: Workspace, store=None, limiter: LoginLimiter | None = None):
        self.ws = ws
        self.store = store                 # StateStore: thu hồi phiên khi đăng xuất, link đăng nhập dùng một lần
        self.limiter = limiter or LoginLimiter()
        self.api_token = os.environ.get("BIZAGENT_API_TOKEN", "")
        self.api_role = os.environ.get("BIZAGENT_API_ROLE", "admin")
        secret = os.environ.get("BIZAGENT_SECRET_KEY", "")
        if not secret:
            secret = _FALLBACK_SECRET
            log.warning("Chưa đặt BIZAGENT_SECRET_KEY: phiên đăng nhập web sẽ mất khi khởi động lại")
        self.signer = Signer(secret)
        self._used_links: set[str] = set()

    @property
    def has_web_users(self) -> bool:
        return any(u.password_hash for u in self.ws.all_users())

    def local_mode(self) -> bool:
        return not self.api_token and not self.has_web_users

    def _role(self, user_id: str) -> Principal | None:
        role = self.ws.role_of(user_id)
        return Principal(user_id, role) if role else None

    @staticmethod
    def _keys(username: str, client_host: str | None) -> tuple[str, str]:
        return f"u:{username.lower()}", (f"ip:{client_host}" if client_host else "")

    def login_blocked(self, username: str, client_host: str | None) -> bool:
        return self.limiter.blocked(*self._keys(username, client_host))

    def check_password(self, username: str, password: str, client_host: str | None = None) -> Principal | None:
        user_key, ip_key = self._keys(username, client_host)
        if self.limiter.blocked(user_key, ip_key):
            log.warning("Tạm khoá đăng nhập '%s' từ %s: sai mật khẩu quá nhiều lần", username, client_host)
            return None
        for u in self.ws.all_users():
            if u.id == f"web:{username}" and u.password_hash and verify_password(password, u.password_hash):
                self.limiter.succeeded(user_key)
                return Principal(u.id, u.role)
        self.limiter.failed(user_key, ip_key)
        return None

    def _sessions_valid_after(self, user_id: str) -> float:
        if self.store is None:
            return 0.0
        try:
            return float(self.store.kv_get(f"session_epoch:{user_id}") or 0)
        except Exception:
            log.exception("Không đọc được trạng thái phiên của %s", user_id)
            return float("inf")            # không chắc phiên còn hợp lệ -> coi như đã thu hồi

    def authenticate(self, *, cookie: str | None, authorization: str, client_host: str | None,
                     forwarded: bool = False) -> Principal | None:
        if cookie:
            body = self.signer.verify(cookie)
            after = self._sessions_valid_after(body["u"]) if body else 0.0
            # chưa từng đăng xuất (after == 0): mọi phiên còn hạn đều hợp lệ, kể cả phiên cũ chưa có iat
            if body and body.get("k") == "session" and (after == 0 or body.get("iat", 0) > after):
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
            return self.check_password(user, pw, client_host)
        # Máy cá nhân: chỉ khi request đến thẳng từ 127.0.0.1. Request qua tunnel/proxy chạy cùng máy (cloudflared,
        # IIS, nginx) cũng có IP 127.0.0.1 nhưng mang header chuyển tiếp -> không cho vào.
        if self.local_mode() and client_host in LOOPBACK and not forwarded:
            return Principal("local", self.api_role)
        return None

    def session_token(self, user_id: str) -> str:
        # Phiên hợp lệ khi iat > thời điểm đăng xuất gần nhất. Đồng hồ trên Windows có độ phân giải thô, nên phiên mới
        # có thể cùng mốc với lần đăng xuất: đẩy iat lên sau mốc đó để đăng nhập lại ngay vẫn được.
        epoch = self._sessions_valid_after(user_id)
        iat = max(time.time(), epoch + 0.001) if epoch != float("inf") else time.time()
        return self.signer.sign({"k": "session", "u": user_id, "iat": iat}, SESSION_DAYS * 86400)

    def revoke_sessions(self, user_id: str) -> None:
        """Đăng xuất: mọi cookie phiên đã cấp cho người này (mọi thiết bị) hết hiệu lực."""
        if self.store is not None:
            self.store.kv_set(f"session_epoch:{user_id}", time.time())

    def login_link_token(self, user_id: str) -> str:
        return self.signer.sign({"k": "link", "u": user_id, "n": secrets.token_urlsafe(12)}, LINK_HOURS * 3600)

    def exchange_link(self, token: str) -> str | None:
        """Đổi link đăng nhập lấy cookie phiên. Mỗi link chỉ dùng được MỘT lần."""
        body = self.signer.verify(token)
        if not body or body.get("k") != "link" or not body.get("n") or not self._role(body["u"]):
            return None
        key = f"login_link:{body['n']}"
        if key in self._used_links:
            return None
        if self.store is not None:
            if self.store.kv_get(key):
                return None
            self.store.kv_set(key, {"user": body["u"], "exp": body["exp"]})
        self._used_links.add(key)
        return self.session_token(body["u"])
