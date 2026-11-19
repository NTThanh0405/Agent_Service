"""Kênh Zalo Official Account (OA): nhận tin qua webhook, trả lời bằng API tin nhắn tư vấn.

Bật trong workspaces/<ten>/channels.yaml:
    zalo:
      enabled: true
      signature: zalo_mac        # zalo_mac | off — xem docs/CHANNELS.md

Biến môi trường (.env):
    ZALO_APP_ID, ZALO_APP_SECRET     ứng dụng trên developers.zalo.me (liên kết với OA)
    ZALO_OA_SECRET_KEY               "OA Secret Key" trong mục Webhook của ứng dụng (kiểm tra chữ ký)
    ZALO_REFRESH_TOKEN               refresh token ban đầu (lấy qua OAuth v4 của OA); token mới lưu vào state DB
    ZALO_WEBHOOK_SECRET              chuỗi ngẫu nhiên, đặt trong URL webhook: <public_url>/zalo/webhook/<chuỗi này>

Người dùng: users.yaml id "zalo:<user_id>". Người lạ nhắn "/myid" để nhận ID gửi quản trị viên.
⚠️ Định dạng chữ ký webhook và mã lỗi token cần đối chiếu tài liệu Zalo hiện hành (developers.zalo.me).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from collections import deque
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from bizagent.agent.service import AgentService
from bizagent.channels.common import current_thread, handle_command, split_text

log = logging.getLogger(__name__)
OAUTH_URL = "https://oauth.zaloapp.com/v4/oa/access_token"
SEND_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
ZALO_LIMIT = 2000
TOKEN_KEY = "zalo.token"
TOKEN_ERRORS = {-216, -124, -14}        # token sai/hết hạn (đối chiếu tài liệu Zalo) -> làm mới rồi gửi lại


class ZaloError(Exception):
    pass


class ZaloConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    signature: str = "zalo_mac"                 # zalo_mac | off
    signature_header: str = "X-ZEvent-Signature"


def channels_config(ws) -> dict:
    path = ws.path / "channels.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_zalo_config(ws) -> ZaloConfig:
    return ZaloConfig(**(channels_config(ws).get("zalo") or {}))


def missing_env(config: ZaloConfig) -> list[str]:
    need = ["ZALO_APP_ID", "ZALO_WEBHOOK_SECRET"]
    if config.signature == "zalo_mac":
        need.append("ZALO_OA_SECRET_KEY")
    if not os.environ.get("ZALO_ACCESS_TOKEN"):
        need += ["ZALO_APP_SECRET", "ZALO_REFRESH_TOKEN"]
    return [n for n in need if not os.environ.get(n)]


def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise ZaloError(f"Chưa đặt biến môi trường {name}")
    return v


def expected_mac(app_id: str, raw_body: bytes, timestamp: str, oa_secret: str) -> str:
    """mac = sha256(app_id + body + timestamp + OA secret key) — theo mô tả webhook của Zalo OA."""
    return hashlib.sha256((app_id + raw_body.decode("utf-8") + timestamp + oa_secret).encode()).hexdigest()


class ZaloClient:
    def __init__(self, store, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.http = httpx.AsyncClient(timeout=20, transport=transport, follow_redirects=False)
        self._lock = asyncio.Lock()

    async def access_token(self, force_refresh: bool = False) -> str:
        async with self._lock:
            tok = await asyncio.to_thread(self.store.kv_get, TOKEN_KEY) or {}
            if not force_refresh and tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
                return tok["access_token"]
            if not force_refresh and not tok and os.environ.get("ZALO_ACCESS_TOKEN") \
                    and not os.environ.get("ZALO_REFRESH_TOKEN"):
                return os.environ["ZALO_ACCESS_TOKEN"]
            refresh = tok.get("refresh_token") or _env("ZALO_REFRESH_TOKEN")
            resp = await self.http.post(
                OAUTH_URL,
                headers={"secret_key": _env("ZALO_APP_SECRET")},
                data={"refresh_token": refresh, "app_id": _env("ZALO_APP_ID"), "grant_type": "refresh_token"},
            )
            data = resp.json() if resp.content else {}
            if "access_token" not in data:
                reason = data.get("error_name") or data.get("error") or resp.status_code
                raise ZaloError(f"Làm mới token Zalo thất bại: {reason}")
            new = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token") or refresh,
                   "expires_at": time.time() + int(data.get("expires_in", 3600))}
            await asyncio.to_thread(self.store.kv_set, TOKEN_KEY, new)   # refresh token đổi sau mỗi lần làm mới
            return new["access_token"]

    async def send_text(self, user_id: str, text: str) -> None:
        for part in split_text(text, ZALO_LIMIT):
            await self._send(user_id, part)

    async def _send(self, user_id: str, text: str, retried: bool = False) -> None:
        token = await self.access_token(force_refresh=retried)
        resp = await self.http.post(SEND_URL, headers={"access_token": token},
                                    json={"recipient": {"user_id": user_id}, "message": {"text": text}})
        data = resp.json() if resp.content else {}
        err = data.get("error", 0 if resp.status_code < 400 else resp.status_code)
        if err == 0:
            return
        if err in TOKEN_ERRORS and not retried:
            await self._send(user_id, text, retried=True)
            return
        raise ZaloError(f"Gửi tin Zalo lỗi {err}: {data.get('message', '')}")

    async def aclose(self) -> None:
        await self.http.aclose()


class ZaloChannel:
    def __init__(self, service: AgentService, config: ZaloConfig, client: ZaloClient | None = None):
        self.service = service
        self.config = config
        self.client = client or ZaloClient(service.store)
        self._seen: deque[str] = deque(maxlen=2000)
        self._tasks: set[asyncio.Task] = set()

    def check_ready(self) -> list[str]:
        return missing_env(self.config)

    def verify(self, raw: bytes, payload: dict, header: str) -> bool:
        if self.config.signature == "off":
            return True
        given = header.strip().removeprefix("mac=").strip()
        want = expected_mac(os.environ.get("ZALO_APP_ID", ""), raw, str(payload.get("timestamp", "")),
                            os.environ.get("ZALO_OA_SECRET_KEY", ""))
        return bool(given) and hmac.compare_digest(given, want)

    def mount(self, app: FastAPI) -> None:
        missing = self.check_ready()
        if missing:
            log.error("Kênh Zalo chưa đủ cấu hình, thiếu: %s", missing)

        @app.post("/zalo/webhook/{secret}")
        async def webhook(secret: str, request: Request) -> dict:
            want = os.environ.get("ZALO_WEBHOOK_SECRET", "")
            if not want or not hmac.compare_digest(secret, want):
                raise HTTPException(status_code=404)
            raw = await request.body()
            try:
                import json

                payload = json.loads(raw)
            except ValueError as e:
                raise HTTPException(status_code=400, detail="JSON không hợp lệ") from e
            if not self.verify(raw, payload, request.headers.get(self.config.signature_header, "")):
                log.warning("Webhook Zalo sai chữ ký (kiểm tra ZALO_OA_SECRET_KEY / signature trong channels.yaml)")
                raise HTTPException(status_code=401, detail="Sai chữ ký")
            task = asyncio.create_task(self.handle(payload))     # trả 200 ngay, Zalo không phải chờ AI
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return {"ok": True}

    async def handle(self, payload: dict[str, Any]) -> None:
        if payload.get("event_name") != "user_send_text":
            return
        sender = str((payload.get("sender") or {}).get("id") or "")
        msg = payload.get("message") or {}
        text = (msg.get("text") or "").strip()
        msg_id = str(msg.get("msg_id") or "")
        if not sender or not text:
            return
        if msg_id:
            if msg_id in self._seen:                      # Zalo có thể gửi lại cùng sự kiện
                return
            self._seen.append(msg_id)
        uid = f"zalo:{sender}"
        role = self.service.ws.role_of(uid)
        try:
            if role is None:
                if text.lower() in ("/myid", "myid"):
                    await self.client.send_text(sender, f"Zalo ID của bạn: {sender}")
                else:
                    log.info("Bỏ qua người lạ %s", uid)
                return
            if text.lower() == "/myid":
                answer = f"Zalo ID của bạn: {sender}"
            else:
                answer = await handle_command(self.service, uid, role, text)
                if answer is None:
                    thread = await current_thread(self.service, uid)
                    answer = await self.service.ask(text, user_id=uid, role=role, thread_id=thread)
            await self.client.send_text(sender, answer)
        except Exception:
            log.exception("Kênh Zalo lỗi khi xử lý tin của %s", uid)
            try:
                await self.client.send_text(sender, "Xin lỗi, đã có lỗi khi xử lý câu hỏi. Hãy thử lại sau.")
            except Exception:
                pass
