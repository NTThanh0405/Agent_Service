"""Prompt dạng Jinja2, ghi đè theo lớp.

Thứ tự tìm template:  workspaces/<x>/prompts/  ->  bizagent/prompts/ (lõi)
- Đặt file cùng tên trong workspace để THAY HẲN prompt lõi.
- Hoặc chỉ sửa một phần: tạo prompts/system.md.j2 với
      {% extends "core/system.md.j2" %}
      {% block business_rules %} ...luật riêng... {% endblock %}
  ("core/..." luôn trỏ tới bản lõi, kể cả khi workspace đã ghi đè cùng tên.)
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, StrictUndefined

from bizagent.workspace import Workspace

CORE_PROMPTS_DIR = Path(__file__).resolve().parent
PRIVATE_MARKER = "<!-- private -->"   # phần sau dấu này trong knowledge/*.md không được nạp cho agent
_WEEKDAYS_VI = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]


def load_knowledge(ws: Workspace) -> str:
    """Ghép knowledge/*.md (theo tên file) thành một khối văn bản."""
    if not ws.knowledge_dir.is_dir():
        return ""
    parts: list[str] = []
    for f in sorted(ws.knowledge_dir.glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        text = f.read_text(encoding="utf-8").split(PRIVATE_MARKER, 1)[0].strip()
        if text:
            parts.append(f"### {f.stem}\n\n{text}")
    return "\n\n".join(parts)


class PromptEngine:
    def __init__(self, ws: Workspace):
        self.ws = ws
        core = FileSystemLoader(str(CORE_PROMPTS_DIR))
        loaders = []
        if ws.prompts_dir.is_dir():
            loaders.append(FileSystemLoader(str(ws.prompts_dir)))
        loaders += [core, PrefixLoader({"core": core})]
        self.env = Environment(
            loader=ChoiceLoader(loaders),
            undefined=StrictUndefined,         # gõ sai tên biến thì báo lỗi ngay, không im lặng ra chuỗi rỗng
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )
        self._knowledge = load_knowledge(ws)

    def base_context(self) -> dict[str, Any]:
        tz = ZoneInfo(self.ws.info.timezone)
        now = datetime.now(tz)
        return {
            "workspace": self.ws.info,
            "vars": self.ws.info.prompt_vars,
            "knowledge": self._knowledge,
            "today": now.date(),
            "now": now,
            "weekday": _WEEKDAYS_VI[now.weekday()] if self.ws.info.language == "vi" else now.strftime("%A"),
        }

    def render(self, name: str, **context: Any) -> str:
        ctx = {**self.base_context(), **context}
        return self.env.get_template(name).render(**ctx).strip() + "\n"


def today_in(ws: Workspace) -> date:
    return datetime.now(ZoneInfo(ws.info.timezone)).date()
