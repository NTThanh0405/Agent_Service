"""Kênh Telegram (long polling, không cần mở port). Cài: pip install "bizagent[telegram]".

Người dùng khai báo trong users.yaml của workspace:  - {id: "telegram:123456789", name: "A", role: admin}
Người ngoài danh sách bị bỏ qua (chỉ /myid được trả lời để họ gửi ID cho quản trị viên).
Chỉ nhận chat riêng, không nhận tin trong group. Cuộc trò chuyện lưu trong state DB (giữ qua khi khởi động lại).
"""

from __future__ import annotations

import logging
import os

from bizagent.agent.service import AgentService
from bizagent.channels.common import current_thread, handle_command, split_text

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4000


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    return split_text(text, limit)


async def run_telegram(service: AgentService) -> None:
    try:
        from aiogram import Bot, Dispatcher, F
        from aiogram.filters import Command
        from aiogram.types import Message
    except ImportError as e:
        raise SystemExit('Chưa cài aiogram: pip install "bizagent[telegram]"') from e

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Chưa đặt TELEGRAM_BOT_TOKEN")

    bot = Bot(token)
    dp = Dispatcher()

    @dp.message(Command("myid"))
    async def myid(m: Message) -> None:
        await m.answer(f"Telegram ID của bạn: {m.from_user.id}")

    @dp.message(F.chat.type != "private")
    async def ignore_groups(m: Message) -> None:
        return

    @dp.message(F.text)
    async def on_text(m: Message) -> None:
        uid = f"telegram:{m.from_user.id}"
        role = service.ws.role_of(uid)
        if role is None:
            log.info("Bỏ qua người lạ %s", uid)
            return
        try:
            answer = await handle_command(service, uid, role, m.text)
            if answer is None:
                await bot.send_chat_action(m.chat.id, "typing")
                thread = await current_thread(service, uid)
                answer = await service.ask(m.text, user_id=uid, role=role, thread_id=thread)
        except Exception:
            log.exception("Lỗi khi trả lời %s", uid)
            answer = "Xin lỗi, đã có lỗi khi xử lý câu hỏi. Hãy thử lại sau."
        for part in split_message(answer):
            await m.answer(part)

    await service.ensure_ready()
    log.info("Bot Telegram đang chạy (workspace %s)", service.ws.name)
    tasks = service.start_background_tasks()
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await service.aclose()
