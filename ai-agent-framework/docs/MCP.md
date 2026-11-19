# MCP (Model Context Protocol)

`pip install -e ".[mcp]"`. Hai chiều:

## 1. Dùng MCP server làm nguồn dữ liệu cho agent (`type: mcp`)

Có hàng nghìn MCP server dựng sẵn (CRM, Google Drive, Notion, Jira, cơ sở dữ liệu...). Khai báo trong `sources.yaml`:

```yaml
crm:
  type: mcp
  description: "CRM: khách hàng, cơ hội bán hàng"
  transport: stdio                         # stdio | streamable_http | sse
  command: npx
  args: ["-y", "@example/crm-mcp-server"]
  env: {CRM_API_KEY: "${CRM_API_KEY}"}
  allowed_tools: [search_contacts, get_deal]   # BẮT BUỘC
  max_output_chars: 8000
  timeout_seconds: 30

tai_lieu:
  type: mcp
  transport: streamable_http
  url: "https://mcp.example.com/mcp"
  headers: {Authorization: "Bearer ${DOCS_TOKEN}"}
  allowed_tools: [search_docs]
```

- Agent thấy tool dưới tên `<nguồn>__<tool>` (vd `crm__search_contacts`).
- **Chỉ tool trong `allowed_tools` được dùng.** MCP server thường có cả tool ghi/xoá: chỉ cho phép tool đọc.
- Quyền theo role như mọi nguồn (`workspace.yaml: roles`); mỗi lần gọi được ghi audit; kết quả dài bị cắt ở
  `max_output_chars`; quá `timeout_seconds` thì báo lỗi cho agent.
- MCP server lỗi (không chạy được lệnh, sai URL, thiếu tool) không làm hỏng agent: lỗi hiện ở `bizagent check`
  và `GET /api/v1/health` → `mcp_errors`.
- Dữ liệu từ MCP server đi thẳng tới model AI; MCP server bên thứ ba có thể trả nội dung chứa "chỉ dẫn" giả.
  System prompt dặn agent coi kết quả tool là dữ liệu, nhưng vẫn chỉ nên dùng MCP server tin cậy.

## 2. Mở dữ liệu doanh nghiệp cho Claude Desktop / Claude Code / Cursor (`bizagent mcp-server`)

bizagent chạy như một MCP server (stdio) với các tool: `business_context`, `list_sources`, `list_tables`,
`describe_table`, `run_sql`, `call_api`, `get_date_range`, `list_datasets`, `get_dataset`.
Mọi lớp bảo vệ vẫn áp dụng: guard chỉ-SELECT, che cột, quyền theo `--role`, chỉ operation API đã khai báo, audit.
Không cần API key model AI (model nằm ở ứng dụng phía client).

Claude Desktop → Settings → Developer → Edit Config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "du-lieu-cong-ty": {
      "command": "C:/bizagent/.venv/Scripts/bizagent.exe",
      "args": ["mcp-server", "-w", "C:/bizagent/workspaces/cong_ty_a", "--role", "manager"]
    }
  }
}
```

Claude Code: `claude mcp add du-lieu-cong-ty -- C:/bizagent/.venv/Scripts/bizagent.exe mcp-server -w C:/bizagent/workspaces/cong_ty_a --role manager`

Role cố định theo `--role` cho cả máy đó, nên chỉ cấu hình trên máy của người có quyền tương ứng.
Audit ghi người dùng là `mcp:<role>`.
