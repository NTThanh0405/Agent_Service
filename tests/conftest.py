from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from bizagent.demo import create_demo_db, write_targets_csv
from bizagent.workspace import load_workspace

ROOT = Path(__file__).resolve().parents[1]
WORKSPACES = ROOT / "workspaces"


@pytest.fixture
def demo_ws(tmp_path: Path):
    """Bản sao workspace demo kèm database mẫu (dữ liệu tới hết 2026-11-18)."""
    dst = tmp_path / "demo"
    shutil.copytree(WORKSPACES / "demo", dst, ignore=shutil.ignore_patterns("data", "files"))
    create_demo_db(dst / "data" / "demo.db", days=30, today=date(2026, 11, 19))
    write_targets_csv(dst / "files" / "muc_tieu.csv", today=date(2026, 11, 19))
    return load_workspace(str(dst))


class FakeToolModel(BaseChatModel):
    """Model giả: trả lần lượt các AIMessage cho sẵn, ghi lại tin nhắn đã nhận."""

    responses: list[AIMessage]
    seen: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        msg = self.responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture
def fake_model_cls():
    return FakeToolModel
