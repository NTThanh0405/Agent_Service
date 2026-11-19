"""Tạo các nguồn dữ liệu từ sources.yaml.

Loại nguồn có sẵn: `sql`. Loại khác đăng ký theo một trong hai cách:
1. Gói Python bên ngoài khai báo entry point nhóm "bizagent.sources" (xem pyproject.toml).
2. Gọi register_source_type("ten", LopNguon) trong code của bạn trước khi chạy.
Lớp nguồn phải kế thừa bizagent.sources.base.DataSource.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from bizagent.sources.base import DataSource, SourceError
from bizagent.sources.gsheets import GoogleSheetsSource
from bizagent.sources.mcp import MCPSource
from bizagent.sources.rest import RestSource
from bizagent.sources.sql import SqlSource
from bizagent.sources.tabular import FilesSource
from bizagent.sources.warehouse import WarehouseSource
from bizagent.workspace import Workspace, expand_env

log = logging.getLogger(__name__)

SOURCE_TYPES: dict[str, type[DataSource]] = {
    "sql": SqlSource,
    "rest": RestSource,
    "google_sheets": GoogleSheetsSource,
    "files": FilesSource,
    "warehouse": WarehouseSource,
    "mcp": MCPSource,
}
_PLANNED: dict[str, str] = {}
_loaded_plugins = False


def register_source_type(name: str, cls: type[DataSource]) -> None:
    if not issubclass(cls, DataSource):
        raise TypeError(f"{cls} phải kế thừa DataSource")
    SOURCE_TYPES[name] = cls


def _load_plugins() -> None:
    global _loaded_plugins
    if _loaded_plugins:
        return
    _loaded_plugins = True
    for ep in entry_points(group="bizagent.sources"):
        try:
            register_source_type(ep.name, ep.load())
        except Exception as e:
            log.error("Không nạp được plugin nguồn %s: %s", ep.name, e)


class SourceRegistry:
    """Giữ các nguồn đã tạo của một workspace. Nguồn lỗi cấu hình không làm hỏng nguồn khác."""

    def __init__(self, ws: Workspace):
        _load_plugins()
        self.ws = ws
        self.sources: dict[str, DataSource] = {}
        self.errors: dict[str, str] = {}
        for name, cfg in ws.sources.items():
            if not cfg.enabled:
                continue
            raw = cfg.model_dump()
            raw.setdefault("timezone", ws.info.timezone)
            cls = SOURCE_TYPES.get(cfg.type)
            if cls is None:
                hint = _PLANNED.get(cfg.type, "chưa có plugin cho loại này")
                self.errors[name] = f"Loại nguồn '{cfg.type}' chưa hỗ trợ ({hint})"
                continue
            try:
                self.sources[name] = cls(name, expand_env(raw, ws.env()))
            except Exception as e:
                self.errors[name] = str(e)
                log.error("Nguồn %s lỗi cấu hình: %s", name, e)

    def get(self, name: str) -> DataSource:
        if name in self.sources:
            return self.sources[name]
        if name in self.errors:
            raise SourceError(f"Nguồn {name} đang lỗi: {self.errors[name]}")
        raise SourceError(f"Không có nguồn '{name}'")

    def for_role(self, role: str | None, user_id: str | None = None) -> list[DataSource]:
        """Nguồn người dùng được xem: theo role + quyền cấp riêng (nếu biết user_id)."""
        return [self.sources[n] for n in self.ws.sources_for(user_id, role) if n in self.sources]

    def close(self) -> None:
        for s in self.sources.values():
            s.close()
