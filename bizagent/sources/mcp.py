"""Nguồn MCP: dùng tool của một MCP server (Model Context Protocol) làm nguồn dữ liệu cho agent.

    crm:
      type: mcp
      description: "CRM: khách hàng, cơ hội bán hàng"
      transport: stdio                        # stdio | streamable_http | sse
      command: npx                            # stdio: lệnh chạy MCP server
      args: ["-y", "@example/crm-mcp"]
      env: {CRM_TOKEN: "${CRM_TOKEN}"}
      # url: "https://mcp.example.com/mcp"    # streamable_http / sse
      # headers: {Authorization: "Bearer ${CRM_TOKEN}"}
      allowed_tools: [search_contacts, get_deal]   # BẮT BUỘC: chỉ tool đọc dữ liệu
      max_output_chars: 8000
      timeout_seconds: 30

Agent thấy mỗi tool dưới tên `<nguồn>__<tool>`. Quyền theo role như mọi nguồn khác (workspace.yaml: roles).
Tool không có trong allowed_tools bị bỏ qua: MCP server có thể có tool ghi/xoá, hãy chỉ cho phép tool đọc.
Cài: pip install "bizagent[mcp]".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool

from bizagent.sources.base import DataSource, QueryResult, SourceError, TableInfo

log = logging.getLogger(__name__)
_NAME = re.compile(r"[^A-Za-z0-9_-]")


class MCPSource(DataSource):
    kind = "mcp"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.transport = config.get("transport", "stdio")
        if self.transport not in ("stdio", "streamable_http", "sse"):
            raise SourceError(f"Nguồn {name}: transport phải là stdio | streamable_http | sse")
        if self.transport == "stdio" and not config.get("command"):
            raise SourceError(f"Nguồn {name}: thiếu command cho transport stdio")
        if self.transport != "stdio" and not config.get("url"):
            raise SourceError(f"Nguồn {name}: thiếu url")
        self.allowed_tools: list[str] = list(config.get("allowed_tools") or [])
        if not self.allowed_tools:
            raise SourceError(f"Nguồn {name}: phải khai báo allowed_tools (chỉ tool đọc dữ liệu)")
        self.max_output_chars = int(config.get("max_output_chars", 8000))
        self.timeout_seconds = int(config.get("timeout_seconds", 30))
        self.descriptions: dict[str, str] = {}
        self.roles_fn: Callable[[str | None, str | None], list[str]] | None = None

    def connection(self) -> dict[str, Any]:
        c = self.config
        if self.transport == "stdio":
            conn: dict[str, Any] = {"transport": "stdio", "command": c["command"], "args": list(c.get("args") or [])}
            if c.get("env"):
                conn["env"] = {k: str(v) for k, v in c["env"].items()}
            if c.get("cwd"):
                conn["cwd"] = c["cwd"]
            return conn
        conn = {"transport": self.transport, "url": c["url"]}
        if c.get("headers"):
            conn["headers"] = dict(c["headers"])
        return conn

    def health(self) -> None:
        return None

    def list_tables(self) -> list[str]:
        return list(self.allowed_tools)

    def describe_table(self, table: str) -> TableInfo:
        if table not in self.allowed_tools:
            raise SourceError(f"Nguồn {self.name} không cho phép tool '{table}'")
        return TableInfo(name=f"{self.name}__{table}", description=self.descriptions.get(table, ""))

    def query(self, query: str, max_rows: int | None = None) -> QueryResult:
        raise SourceError(f"Nguồn MCP {self.name}: gọi trực tiếp các tool {self.name}__<tool>")


def _text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, tuple):                      # (content, artifact)
        result = result[0]
    if isinstance(result, list):
        return "\n".join(b.get("text", json.dumps(b, ensure_ascii=False)) if isinstance(b, dict) else str(b)
                         for b in result)
    return json.dumps(result, ensure_ascii=False, default=str)


def wrap_tool(src: MCPSource, tool: BaseTool, audit: Callable[..., None]) -> BaseTool:
    """Bọc tool MCP: kiểm tra quyền theo role, timeout, cắt bớt kết quả, ghi audit."""
    ws_roles = src.roles_fn
    name = _NAME.sub("_", f"{src.name}__{tool.name}")[:64]

    async def run(config: RunnableConfig, **kwargs: Any) -> str:
        conf = config.get("configurable") or {}
        role, user = conf.get("role"), conf.get("user_id", "?")
        if ws_roles is not None and src.name not in ws_roles(user, role):
            return f"Lỗi: không có quyền dùng nguồn '{src.name}'"
        try:
            out = await asyncio.wait_for(tool.ainvoke(kwargs), timeout=src.timeout_seconds)
        except TimeoutError:
            return f"Lỗi: {name} quá {src.timeout_seconds} giây"
        except Exception as e:
            audit(user, "mcp_error", source=src.name, tool=tool.name, error=str(e)[:300])
            return f"Lỗi khi gọi {name}: {str(e)[:300]}"
        text = _text(out)
        audit(user, "mcp", source=src.name, tool=tool.name, args=kwargs, chars=len(text))
        if len(text) > src.max_output_chars:
            text = text[: src.max_output_chars] + f"\n…(đã cắt, còn {len(text) - src.max_output_chars} ký tự)"
        return text

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=f"[Nguồn {src.name} qua MCP] {tool.description or ''}"[:1024],
        args_schema=tool.args_schema,
    )


async def load_mcp_tools(registry, audit: Callable[..., None]) -> tuple[list[BaseTool], dict[str, str]]:
    """Nạp tool từ mọi nguồn MCP. Một MCP server lỗi không làm hỏng phần còn lại."""
    sources = [s for s in registry.sources.values() if isinstance(s, MCPSource)]
    if not sources:
        return [], {}
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        msg = 'Nguồn MCP cần: pip install "bizagent[mcp]"'
        return [], {s.name: msg for s in sources}

    tools: list[BaseTool] = []
    errors: dict[str, str] = {}
    for src in sources:
        src.roles_fn = registry.ws.sources_for
        try:
            client = MultiServerMCPClient({src.name: src.connection()})
            found = await asyncio.wait_for(client.get_tools(), timeout=src.timeout_seconds)
        except Exception as e:
            errors[src.name] = f"Không kết nối được MCP server: {e}"
            log.error("Nguồn MCP %s lỗi: %s", src.name, e)
            continue
        by_name = {t.name: t for t in found}
        missing = [t for t in src.allowed_tools if t not in by_name]
        if missing:
            errors[src.name] = f"MCP server không có tool: {missing}. Có: {sorted(by_name)}"
        for tname in src.allowed_tools:
            if tname in by_name:
                src.descriptions[tname] = by_name[tname].description or ""
                tools.append(wrap_tool(src, by_name[tname], audit))
        log.info("Nguồn MCP %s: %d tool", src.name, len([t for t in src.allowed_tools if t in by_name]))
    return tools, errors
