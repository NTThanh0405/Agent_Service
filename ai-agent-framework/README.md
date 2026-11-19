# bizagent — khung AI agent phân tích dữ liệu doanh nghiệp

Hỏi dữ liệu kinh doanh bằng tiếng Việt tự nhiên ("doanh thu tuần này so với tuần trước?"), agent tự viết và
chạy câu `SELECT` **chỉ-đọc** trên database của bạn rồi trả lời bằng số liệu thật.

- **Nhiều database**: PostgreSQL, MySQL/MariaDB, SQL Server, SQLite, Oracle, DuckDB... (qua SQLAlchemy).
- **API và bảng tính**: REST API khai báo bằng YAML (Base.vn, iPOS, MISA...), Google Sheets, file CSV/Excel.
- **Kho dữ liệu nội bộ**: đồng bộ theo lịch về DuckDB để ghép dữ liệu nhiều nguồn bằng SQL, kết quả kèm giờ đồng bộ.
- **Dashboard — chọn một hoặc cả hai**: (1) dashboard sống viết bằng YAML/SQL/JS, tự làm mới, không hết hạn, agent tạo
  được bằng lời; (2) đưa dữ liệu sang **Power BI** (Web connector có mã M dựng sẵn, hoặc file Parquet/CSV theo lịch).
- **Nhiều model AI**: Claude, ChatGPT, Gemini, DeepSeek, Ollama (chạy trên máy), mọi API tương thích OpenAI. Đổi bằng một dòng.
- **Mỗi doanh nghiệp một workspace**: prompt, tri thức nghiệp vụ, nguồn dữ liệu, phân quyền nằm trong một thư mục.
- **Nhiều kênh**: REST API + trang chat web, dòng lệnh, Telegram, **Zalo OA**; hội thoại lưu trong database (nhớ qua khi
  khởi động lại), audit log mọi câu hỏi và truy vấn.
- **MCP**: dùng MCP server bất kỳ làm nguồn dữ liệu, và mở dữ liệu doanh nghiệp cho Claude Desktop / Claude Code / Cursor.
- **Vận hành**: báo cáo định kỳ gửi qua Telegram/Zalo/Slack (hỏi tiếp ngay trong ngữ cảnh báo cáo), cấp quyền theo người
  có hạn dùng (`/capquyen`), trang quản trị web `/admin`, reviewer đối chiếu mọi con số trong câu trả lời với dữ liệu.
- **An toàn**: guard SQL theo từng dialect, phiên database chỉ-đọc, che cột cá nhân, phân quyền role → nguồn.

> Đây là **khung**: tải về, sửa thoải mái. Giai đoạn 1 đã xong (xem [Lộ trình](#lộ-trình)).

---

## Chạy thử trong 3 phút (workspace demo, dữ liệu giả)

```bash
git clone <repo> bizagent && cd bizagent
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[anthropic]"          # hoặc [openai], [google], [deepseek], [ollama], [llm-all]
cp .env.example .env                   # điền ANTHROPIC_API_KEY (hoặc key của model bạn chọn)
bizagent seed-demo                     # tạo database bán hàng giả + file kế hoạch giả
bizagent sync                          # đồng bộ hai nguồn về kho DuckDB (để so thực tế với mục tiêu)
bizagent check                         # kiểm tra model, kết nối, prompt, đồng bộ
bizagent chat                          # chat trong terminal
bizagent serve                         # http://127.0.0.1:8000: chat, /dashboards, /powerbi
```

Bằng Docker: `cp .env.example .env` (điền key và `BIZAGENT_API_TOKEN`) rồi `docker compose up -d --build`.

---

## Dùng cho doanh nghiệp của bạn

```bash
bizagent init cong_ty_a                # tạo workspaces/cong_ty_a từ template
# sửa workspaces/cong_ty_a/*.yaml và knowledge/*.md
bizagent check -w cong_ty_a
bizagent prompt -w cong_ty_a           # xem system prompt cuối cùng agent nhận được
bizagent serve -w cong_ty_a
```

```
workspaces/cong_ty_a/
├── workspace.yaml    tên, mô tả, ngôn ngữ, múi giờ, tiền tệ, role -> nguồn được xem, biến cho prompt
├── sources.yaml      database / API (mật khẩu dùng ${BIEN}, đặt trong .env)
├── agents.yaml       model AI, số bước, timeout
├── sync.yaml         (tuỳ chọn) lịch đồng bộ các nguồn về kho DuckDB
├── dashboards.yaml   bật dashboard sống và/hoặc Power BI
├── datasets.yaml     truy vấn dùng chung cho dashboard và Power BI
├── dashboards/       mỗi file một dashboard (YAML, + JS cho widget tuỳ chỉnh)
├── channels.yaml     (tuỳ chọn) bật Zalo OA, Slack
├── reports.yaml      (tuỳ chọn) báo cáo định kỳ
├── users.yaml        người dùng kênh chat và role (không commit)
├── knowledge/*.md    định nghĩa chỉ số, bối cảnh, SQL mẫu -> ghép vào prompt
├── prompts/          ghi đè prompt lõi (Jinja2)
└── .env              bí mật riêng của workspace (không commit)
```

Thư mục `workspaces/` chỉ đưa `_template` và `demo` lên git; workspace của doanh nghiệp thật ở lại máy.

### Kết nối database

```yaml
# sources.yaml
sources:
  sales:
    type: sql
    url: "postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST:-localhost}:5432/${DB_NAME}"
    description: "Dữ liệu bán hàng"
    allowed_tables: ["public.*"]       # bắt buộc: bảng agent được đọc
    default_schema: public
    mask_columns: ["phone", "email"]
```

| Database | `url` | Cài thêm |
|---|---|---|
| PostgreSQL | `postgresql+psycopg://user:pass@host:5432/db` | `pip install -e ".[postgres]"` |
| MySQL / MariaDB | `mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4` | `.[mysql]` |
| SQL Server | `mssql+pyodbc://user:pass@host:1433/db?driver=ODBC+Driver+18+for+SQL+Server` | `.[mssql]` + ODBC Driver 18 |
| SQLite | `sqlite:///${WORKSPACE_DIR}/data/x.db` | có sẵn |
| Khác | URL SQLAlchemy của driver đó | driver tương ứng |

### API, Google Sheets, file, kho dữ liệu

| `type` | Dùng cho |
|---|---|
| `rest` | API trả JSON (Base.vn, iPOS, MISA, CRM...): khai báo các `operations` được phép gọi, xác thực, phân trang |
| `google_sheets` | Google Sheets: mỗi tab (hoặc range) là một bảng, hỏi bằng SQL |
| `files` | Thư mục CSV/Excel |
| `warehouse` | Kho DuckDB nhận dữ liệu đồng bộ theo `sync.yaml`, để JOIN chéo nguồn |
| `mcp` | MCP server bất kỳ (CRM, Drive, Notion...): chỉ các tool trong `allowed_tools`, xem [docs/MCP.md](docs/MCP.md) |

Hướng dẫn và mẫu đầy đủ: [docs/CONNECTORS.md](docs/CONNECTORS.md).

**Luôn dùng tài khoản database chỉ có quyền SELECT** và không có quyền đọc cột cá nhân nếu được.
Bên cạnh đó code còn có: guard SQL (`bizagent/security/guard.py`), phiên chỉ-đọc theo dialect, timeout, giới hạn dòng,
che cột theo `mask_columns`.

### Đổi model AI

```yaml
# agents.yaml
default_model: anthropic:claude-sonnet-5-5     # hoặc openai:<model>, google_genai:<model>, deepseek:<model>, ollama:<model>
models:
  local: {provider: openai, model: <model>, base_url: "http://localhost:1234/v1", api_key_env: LOCAL_LLM_KEY}
agent:
  model: default                               # hoặc "local"
```

Tên ngắn được chấp nhận: `claude`, `chatgpt`/`gpt`, `gemini`, `deepseek`, `ollama`, `azure`, `groq`, `mistral`, `bedrock`.
Khác biệt giữa các model (prompt caching, temperature...) nằm trong `CAPABILITIES` ở `bizagent/llm/registry.py`.

### Tuỳ chỉnh prompt

Prompt lõi: `bizagent/prompts/system.md.j2`. Trong workspace tạo `prompts/system.md.j2`:

```jinja
{% extends "core/system.md.j2" %}
{% block business_rules %}
8. Doanh thu báo theo triệu đồng. Giọng văn: {{ vars.tone }}.
{% endblock %}
```

Xem thêm `workspaces/_template/prompts/README.md`.

### Dashboard và Power BI

| | Dashboard sống | Power BI |
|---|---|---|
| Bật | `dashboards.yaml` → `live.enabled: true` | `powerbi.enabled: true`, `powerbi.datasets: [...]` |
| Mở | `/dashboards`, `/d/<slug>` | `/powerbi` (URL + mã Power Query cho từng dataset) |
| Dựng | `dashboards/<slug>.yaml`: bộ lọc + widget (kpi, line, bar, area, pie, table, custom JS) | trong Power BI Desktop |
| Làm mới | tự động mỗi `refresh_seconds` | Scheduled refresh của Power BI, hoặc file xuất theo lịch |

Đăng nhập: tài khoản web (`bizagent hash-password`), `/login` trong Telegram, hoặc API token (Basic cho Power BI).
Hướng dẫn đầy đủ: [docs/DASHBOARDS.md](docs/DASHBOARDS.md).

### Kênh chat

| Kênh | Lệnh | Ghi chú |
|---|---|---|
| Terminal | `bizagent chat [--role admin]` | thử nhanh |
| REST + web | `bizagent serve` | `POST /api/v1/chat`, `GET /api/v1/health`, trang chat ở `/`. Nghe ngoài 127.0.0.1 bắt buộc có `BIZAGENT_API_TOKEN` |
| Telegram | `bizagent telegram` | `pip install -e ".[telegram]"`, `TELEGRAM_BOT_TOKEN`, người dùng trong `users.yaml` (`telegram:<id>`) |
| Slack | chạy cùng `bizagent serve` | `channels.yaml: slack.enabled`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, Request URL `/slack/events`, người dùng `slack:<U...>` |
| Zalo OA | chạy cùng `bizagent serve` | `channels.yaml: zalo.enabled`, biến `ZALO_*`, webhook `/zalo/webhook/<bí mật>`, người dùng `zalo:<id>` |
| Claude Desktop / Cursor | `bizagent mcp-server --role manager` | MCP stdio, xem [docs/MCP.md](docs/MCP.md) |

Lệnh chung (Telegram, Zalo, Slack, terminal): `/new`, `/sessions`, `/chon <số>`, `/login`, `/myid`;
quản trị: `/users`, `/adduser`, `/removeuser`, `/capquyen`, `/thuhoi`, `/quyen` ([docs/ADMIN.md](docs/ADMIN.md)).
Báo cáo định kỳ: `reports.yaml`, `bizagent report <ten> --preview` ([docs/REPORTS.md](docs/REPORTS.md)).
Phiên chat và audit: [docs/CHANNELS.md](docs/CHANNELS.md) · `bizagent audit`.

---

## Kiến trúc

```
kênh (cli / rest / telegram) ──► AgentService.ask(text, user_id, role, thread_id)
                                     │  system prompt = Jinja2(lõi + workspace) + knowledge + nguồn theo role
                                     ▼
                     LangGraph: agent ⇄ tools (list_sources, list_tables, describe_table, run_sql, call_api, get_date_range)
                                     │                         │ quyền: role -> nguồn (workspace.yaml)
                                     ▼                         ▼
                     llm/registry (mọi nhà cung cấp)    sources/registry ─┬─ sql ─────────── guard + masking -> DB
                                                                          ├─ rest ────────── operations khai báo -> API
                                                                          ├─ google_sheets / files ─ DuckDB tạm
                                                                          └─ warehouse (DuckDB) ◄── sync.py theo lịch
```

| Thư mục | Vai trò |
|---|---|
| `bizagent/workspace.py` | Nạp và kiểm tra cấu hình workspace, thay `${BIEN}` |
| `bizagent/llm/` | `provider:model` → chat model; bảng năng lực từng model |
| `bizagent/prompts/` | Prompt lõi + engine ghi đè theo lớp |
| `bizagent/sources/` | Giao diện `DataSource`; nguồn `sql`, `rest`, `google_sheets`, `files`, `warehouse`; đăng ký plugin (entry point `bizagent.sources`) |
| `bizagent/sync.py` | Đồng bộ nguồn → kho theo lịch (`replace` / `append` / `upsert`), lưu trạng thái |
| `bizagent/security/` | Guard SQL đa dialect, che dữ liệu cá nhân |
| `bizagent/agent/` | Tool, graph, `AgentService` |
| `bizagent/periods.py` | "hôm qua", "tuần này", "tháng 10/2026"... → khoảng ngày (code tính, không để AI đoán) |
| `bizagent/dashboards/` | Catalog dashboard/dataset, bộ chạy dataset (tham số an toàn, quyền, cache), Power BI |
| `bizagent/state.py` | Phiên chat (checkpoint LangGraph), danh sách cuộc trò chuyện, audit, khoá-giá trị — SQLite/PostgreSQL |
| `bizagent/mcp_server.py` | Mở dữ liệu workspace thành MCP server |
| `bizagent/access.py` | Người dùng thêm bằng lệnh/web, quyền cấp riêng có hạn dùng |
| `bizagent/reports.py` | Báo cáo định kỳ: lịch, dựng theo quyền người nhận, gửi qua kênh, nhận xét AI đã kiểm số |
| `bizagent/numcheck.py` | Đối chiếu con số trong câu chữ với dữ liệu (reviewer, nhận xét báo cáo) |
| `bizagent/webauth.py` | Tài khoản web, phiên ký HMAC, link đăng nhập từ Telegram, Basic/Bearer |
| `bizagent/channels/` | REST/web (chat, dashboard, Power BI, `/admin`), Telegram, Zalo OA, Slack, lệnh chung |

Thêm loại nguồn mới: viết lớp kế thừa `DataSource`, đăng ký bằng `register_source_type("ten", Lop)` hoặc entry point.

---

## Kiểm thử

```bash
pip install -e ".[dev,anthropic,telegram]"
pytest
```

Test không gọi model thật (dùng model giả) và không cần mạng.

---

## Lộ trình

- [x] **Giai đoạn 1 — Tách lõi**: workspace, nhiều model AI, prompt Jinja2 ghi đè theo lớp, nguồn SQL đa dialect + guard,
      kênh CLI/REST/web/Telegram, Docker, CI, workspace demo.
- [x] **Giai đoạn 2 — Nguồn dữ liệu**: REST khai báo bằng YAML (khung Base.vn, iPOS, MISA), Google Sheets,
      file CSV/Excel, kho DuckDB + đồng bộ theo lịch để JOIN chéo nguồn, kết quả kèm giờ đồng bộ.
- [x] **Giai đoạn 3 — Dashboard**: dashboard sống (YAML + SQL + JS, tự làm mới, không hết hạn, đăng nhập, agent tạo
      bằng lời) và kết nối Power BI (Web connector + mã M, xuất Parquet/CSV theo lịch, link báo cáo có sẵn).
- [x] **Giai đoạn 4 — Kết nối mở rộng**: MCP hai chiều (MCP server làm nguồn; bizagent làm MCP server cho Claude Desktop),
      kênh Zalo OA, phiên chat lưu SQLite/PostgreSQL (nhớ qua khởi động lại, `/sessions`), audit log trong DB.
- [x] **Giai đoạn 5 — Vận hành**: báo cáo định kỳ (Telegram/Zalo/Slack), trang quản trị `/admin`, kênh Slack,
      cấp quyền theo người có hạn dùng, reviewer đối chiếu số.
- [ ] **Tiếp theo (tuỳ nhu cầu)**: Microsoft Teams (Bot Framework), tách multi-agent (analyst/coder/designer) khi
      câu hỏi phức tạp hơn, email báo cáo, báo cáo dạng ảnh/PDF.

## Giấy phép

MIT — xem [LICENSE](LICENSE).
