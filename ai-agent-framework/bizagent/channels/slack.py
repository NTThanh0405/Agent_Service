"""Kênh Slack (Events API): người dùng nhắn tin riêng (DM) cho app, bot trả lời trong DM.

Bật trong workspaces/<ten>/channels.yaml:   slack: {enabled: true}
Biến môi trường: SLACK_BOT_TOKEN (xoxb-...), SLACK_SIGNING_SECRET.
Slack app: Event Subscriptions -> Request URL https://<public_url>/slack/events, sự kiện bot `message.im`;
OAuth scopes: chat:write, im:history. Người dùng: users.yaml id "slack:<Slack user ID, vd U0123ABC>".
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque

import httpx
from fastapi import FastAPI, HTTPException, Request

from bizagent.channels.common import current_thread, handle_command, split_text

log = logging.getLogger(__name__)
SLACK_LIMIT = 3500


def slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class SlackClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(timeout=20, transport=transport)

    async def post_message(self, channel: str, text: str) -> None:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("Chưa đặt SLACK_BOT_TOKEN")
        for part in split_text(text, SLACK_LIMIT):
            r = await self.http.post("https://slack.com/api/chat.postMessage",
                                     headers={"Authorization": f"Bearer {token}"},
                                     json={"channel": channel, "text": part})
            data = r.json() if r.content else {}
            if not data.get("ok"):
                raise RuntimeError(f"Slack chat.postMessage lỗi: {data.get('error', r.status_code)}")

    async def aclose(self) -> None:
        await self.http.aclose()


class SlackChannel:
    def __init__(self, service, client: SlackClient | None = None):
        self.service = service
        self.client = client or SlackClient()
        self._seen: deque[str] = deque(maxlen=2000)
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def missing_env() -> list[str]:
        return [n for n in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET") if not os.environ.get(n)]

    def verify(self, body: bytes, timestamp: str, signature: str) -> bool:
        secret = os.environ.get("SLACK_SIGNING_SECRET", "")
        if not secret or not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300:
            return False                                         # chống gửi lại yêu cầu cũ
        return hmac.compare_digest(slack_signature(secret, timestamp, body), signature)

    def mount(self, app: FastAPI) -> None:
        if self.missing_env():
            log.error("Kênh Slack thiếu biến: %s", self.missing_env())

        @app.post("/slack/events")
        async def events(request: Request) -> dict:
            body = await request.body()
            if not self.verify(body, request.headers.get("X-Slack-Request-Timestamp", ""),
                               request.headers.get("X-Slack-Signature", "")):
                raise HTTPException(status_code=401, detail="Sai chữ ký Slack")
            payload = json.loads(body)
            if payload.get("type") == "url_verification":
                return {"challenge": payload.get("challenge", "")}
            if request.headers.get("X-Slack-Retry-Num"):
                return {"ok": True}                               # Slack gửi lại do chờ lâu: đã xử lý lần đầu
            task = asyncio.create_task(self.handle(payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return {"ok": True}

    async def handle(self, payload: dict) -> None:
        ev = payload.get("event") or {}
        if ev.get("type") != "message" or ev.get("channel_type") != "im" or ev.get("bot_id") or ev.get("subtype"):
            return
        event_id = str(payload.get("event_id") or ev.get("client_msg_id") or "")
        if event_id:
            if event_id in self._seen:
                return
            self._seen.append(event_id)
        uid, text, channel = f"slack:{ev.get('user')}", (ev.get("text") or "").strip(), ev.get("channel")
        role = self.service.ws.role_of(uid)
        try:
            if role is None:
                if text.lower() == "/myid" or text.lower() == "myid":
                    await self.client.post_message(channel, f"Slack ID của bạn: {ev.get('user')}")
                return
            answer = await handle_command(self.service, uid, role, text)
            if answer is None:
                thread = await current_thread(self.service, uid)
                answer = await self.service.ask(text, user_id=uid, role=role, thread_id=thread)
            await self.client.post_message(channel, answer)
        except Exception:
            log.exception("Kênh Slack lỗi khi xử lý tin của %s", uid)
