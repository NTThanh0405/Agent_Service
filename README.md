# bizagent — AI agent phân tích dữ liệu cho doanh nghiệp

Hỏi số liệu kinh doanh bằng tiếng Việt tự nhiên: *"doanh thu tuần này so với tuần trước?"*, *"cửa hàng nào đạt
mục tiêu tháng?"*. Agent tự viết và chạy truy vấn **chỉ-đọc** trên dữ liệu của bạn, trả lời bằng số liệu thật,
dựng dashboard, gửi báo cáo định kỳ qua Telegram/Zalo/Slack.

**bizagent là khung (framework):** tải về, khai báo dữ liệu và nghiệp vụ của doanh nghiệp bằng file cấu hình,
sửa code tuỳ ý. Có **hai phiên bản triển khai** dùng chung một mã nguồn:

| | 🪟 Windows Server | 🐧 Linux (Docker) |
|---|---|---|
| Chạy bằng | Windows Service (WinSW), tự bật khi khởi động máy | `docker compose`, tự khởi động lại |
| Cài đặt | `deploy\windows\install.ps1` | `deploy/docker/setup.sh` + `docker compose up -d` |
| HTTPS ra Internet | Cloudflare Tunnel hoặc IIS/nginx phía trước | Caddy (tự lấy chứng chỉ) hoặc Cloudflare Tunnel, có sẵn trong compose |
| SQL Server | ODBC Driver có sẵn trên Windows | Build với `BIZAGENT_MSSQL=1` |
| Hợp với | Máy chủ Windows sẵn có (cùng máy SQL Server / MISA / phần mềm kế toán) | VPS / máy chủ Linux, cloud |

> 📘 **Hướng dẫn chi tiết bản Word (tải về):** [docs/HUONG_DAN_CHI_TIET.docx](docs/HUONG_DAN_CHI_TIET.docx?raw=true)
> — 16 chương, từng bước cho cá nhân và doanh nghiệp: cách hệ thống hoạt động, cài trên máy cá nhân / Windows Server /
> Docker, cấu hình từng file workspace (kèm mẫu cho PostgreSQL, MySQL, SQL Server, Excel, Google Sheets, REST API),
> tạo bot Telegram, Zalo, Slack, email Gmail/Microsoft 365, Power BI, tạo tài khoản database chỉ-đọc, sao lưu,
> bảng khắc phục sự cố, tham chiếu lệnh và biến môi trường. Trên GitHub bấm vào link rồi chọn **Download raw file**.

Mục lục: [Dùng cho ai](#dùng-cho-ai-chọn-cách-triển-khai) · [Tính năng](#tính-năng) ·
[Cách hoạt động](#cách-bizagent-hoạt-động) · [Chạy thử](#chạy-thử-5-phút) · [Bản Windows Server](#-bản-windows-server) ·
[Bản Linux (Docker)](#-bản-linux-docker) · [Cấu hình doanh nghiệp](#cấu-hình-cho-doanh-nghiệp-của-bạn) ·
[Kênh chat](#kết-nối-kênh-chat-và-email) · [Bảo mật](#bảo-mật) · [Lệnh](#lệnh-bizagent) ·
[Khắc phục sự cố](#khắc-phục-sự-cố) · [Cấu trúc mã nguồn](#cấu-trúc-mã-nguồn) · [Lộ trình](#lộ-trình)

---

## Dùng cho ai, chọn cách triển khai

| Bạn là | Dùng để | Nên chạy |
|---|---|---|
| **Cá nhân / freelancer** | Hỏi đáp file Excel/CSV, Google Sheets thu chi, dữ liệu shop online; báo cáo email mỗi sáng | Laptop: [Chạy thử](#chạy-thử-5-phút) rồi `bizagent init` workspace riêng. Muốn dữ liệu không ra ngoài: model Ollama chạy trên máy |
| **Cửa hàng, chuỗi nhỏ** | Doanh thu theo cửa hàng, món bán chạy, đạt mục tiêu; báo cáo Telegram/Zalo cho chủ | Máy Windows sẵn có (cùng máy phần mềm bán hàng): [Bản Windows Server](#-bản-windows-server) |
| **Doanh nghiệp vừa** | Nhiều phòng ban, phân quyền theo vai trò, dashboard nội bộ, Power BI, audit | VPS Linux: [Bản Linux (Docker)](#-bản-linux-docker) + HTTPS |
| **Đơn vị triển khai / IT** | Dựng cho nhiều khách hàng | Mỗi khách một thư mục workspace, một tiến trình/container |

---

## Tính năng

| Nhóm | Có gì |
|---|---|
| **Dữ liệu** | PostgreSQL, MySQL/MariaDB, SQL Server, SQLite, Oracle… · REST API khai báo bằng YAML (Base.vn, iPOS, MISA…) · Google Sheets · file CSV/Excel · MCP server · kho DuckDB đồng bộ theo lịch để ghép dữ liệu nhiều nguồn |
| **Model AI** | Claude, ChatGPT, Gemini, DeepSeek, Ollama (chạy trên máy), mọi API tương thích OpenAI — đổi bằng một dòng |
| **Tuỳ chỉnh theo doanh nghiệp** | Mỗi doanh nghiệp một thư mục *workspace*: prompt (Jinja2), định nghĩa chỉ số, SQL mẫu, phân quyền |
| **Kênh** | Trang chat web, REST API, Telegram, Zalo OA, Slack, terminal, Claude Desktop/Cursor (MCP) · báo cáo định kỳ qua cả email |
| **Dashboard** | Dashboard sống viết bằng YAML/SQL/JS, tự làm mới, không hết hạn, agent tạo được bằng lời · hoặc đưa dữ liệu sang **Power BI** |
| **Vận hành** | Báo cáo định kỳ · trang quản trị `/admin` · cấp quyền theo người có hạn dùng · audit log · phiên chat lưu DB |
| **An toàn** | Chỉ cho `SELECT` (kiểm tra theo từng loại database) · phiên DB chỉ-đọc · che cột cá nhân · reviewer đối chiếu mọi con số trong câu trả lời với dữ liệu |

---

## Cách bizagent hoạt động

Mọi kênh (web, Telegram, Zalo, Slack, terminal, REST API) đi qua một điểm vào là `AgentService.ask()`:

1. **Kênh nhận tin** và xác định người hỏi (`telegram:<id>`, `zalo:<id>`, `slack:<id>`, `web:<tên>`, `api:token`).
2. **Tra role** trong `users.yaml` hoặc state DB. Người lạ bị bỏ qua (chỉ `/myid` được trả lời).
3. **Lệnh cố định** (`/new`, `/sessions`, `/login`, `/adduser`, `/capquyen`…) xử lý bằng code, không gọi AI.
4. **Kiểm tra hạn mức** (câu hỏi/phút, token/ngày), khoá cuộc trò chuyện, hàng đợi `max_concurrent`.
5. **Ghép system prompt**: thông tin doanh nghiệp, ngày hôm nay, role, *chỉ các nguồn người này được xem*, `knowledge/*.md`.
6. **Vòng agent ↔ tool (LangGraph)**: `get_date_range` (đổi "tuần này" ra ngày bằng code) → `list_tables` /
   `describe_table` → `run_sql` / `call_api` → (tuỳ chọn) `list_dashboards` / `create_dashboard`, tool MCP.
7. **Mỗi câu SQL**: kiểm tra quyền nguồn ở server → guard chỉ-SELECT + allowlist bảng → viết lại SQL để che cột cá nhân →
   phiên DB chỉ-đọc, giới hạn dòng, huỷ khi quá giờ → ghi audit.
8. **Reviewer** đối chiếu con số trong câu trả lời với dữ liệu vừa truy vấn; lưu lịch sử, mức dùng, audit; trả lời về kênh.

Chạy nền trong cùng tiến trình (`bizagent run`): **đồng bộ** nguồn → kho DuckDB (`sync.yaml`), **báo cáo định kỳ**
(`reports.yaml`, kiểm tra lịch mỗi phút), **xuất Power BI** (`powerbi.export`), **dọn** phiên chat/audit cũ mỗi giờ.

---

## Chạy thử (5 phút)

Dữ liệu giả của một chuỗi cà phê, chạy trên máy cá nhân (Windows, Linux hoặc macOS). Cần Python 3.11+.

```bash
git clone https://github.com/<tai-khoan>/<repo>.git bizagent
cd bizagent
python -m venv .venv
```

Kích hoạt venv: Windows `.venv\Scripts\activate` · Linux/macOS `source .venv/bin/activate`

```bash
pip install -e ".[anthropic,telegram,excel]"
```

```bash
cp .env.example .env
```

Mở `.env`, điền `ANTHROPIC_API_KEY` (hoặc key của model khác, đổi `default_model` trong `workspaces/demo/agents.yaml`).

```bash
bizagent seed-demo
```

```bash
bizagent sync
```

```bash
bizagent check
```

```bash
bizagent run
```

Mở http://127.0.0.1:8000 (chat) · `/dashboards` · `/powerbi` · `/admin`. Trên máy cá nhân chưa đặt mật khẩu thì
tự đăng nhập quyền admin. Thử hỏi: *"tháng này mỗi cửa hàng đạt bao nhiêu % mục tiêu?"*

---

## 🪟 Bản Windows Server

Windows Server 2019/2022/2025 (hoặc Windows 10/11). bizagent chạy thành **một Windows Service** gồm web,
bot Telegram và các tác vụ nền (đồng bộ, báo cáo, xuất Power BI).

### Chuẩn bị

1. **Python 3.12** 64-bit từ python.org: chọn *Install for all users* và *Add to PATH*.
2. **Git for Windows**.
3. **WinSW v2.12** — tải `WinSW-x64.exe` từ trang releases của dự án WinSW trên GitHub, đặt vào `C:\tools\WinSW-x64.exe`.
4. (Nếu đọc SQL Server) **ODBC Driver 18 for SQL Server** của Microsoft.

### Cài đặt

PowerShell **Run as Administrator**:

```powershell
cd C:\
git clone https://github.com/<tai-khoan>/<repo>.git bizagent
cd C:\bizagent
```

Tạo workspace cho doanh nghiệp (hoặc dùng `demo` để thử):

```powershell
py -3.12 -m venv .venv; .venv\Scripts\pip install -e ".[anthropic]"; .venv\Scripts\bizagent init cong_ty_a
```

Sửa `workspaces\cong_ty_a\*.yaml` (xem [Cấu hình](#cấu-hình-cho-doanh-nghiệp-của-bạn)), rồi chạy trình cài:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -WinSW C:\tools\WinSW-x64.exe -Workspace cong_ty_a
```

Trình cài sẽ:

1. Tạo `.venv`, cài thư viện (mặc định: Claude, Telegram, PostgreSQL, SQL Server, Excel, Google Sheets, MCP; đổi bằng `-Extras`).
2. Tạo `.env` nếu chưa có, **sinh sẵn** `BIZAGENT_SECRET_KEY` và `BIZAGENT_API_TOKEN`. Bạn điền API key model và token kênh chat.
3. Chạy `bizagent check`.
4. Cấp quyền cho tài khoản dịch vụ ít quyền `LocalService`: chỉ đọc thư mục dự án, ghi `workspaces\` và `logs\`.
5. Sinh `deploy\windows\service\Bizagent.xml`, cài và bật service `Bizagent` (tự bật khi khởi động máy, tự khởi động lại khi lỗi).

Thử trước không thay đổi gì: thêm `-DryRun`. Các tham số khác: `-Port 8000`, `-ListenHost 127.0.0.1`,
`-ServiceName Bizagent`, `-PythonVersion 3.12`, `-SkipInstall`.

Bật theo dõi treo (khởi động lại khi service còn chạy nhưng không trả lời):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\healthcheck.ps1 -Register
```

### Đưa ra Internet (dashboard, Power BI Service, webhook Zalo/Slack)

Service chỉ nghe `127.0.0.1:8000`, không mở port trên Windows Firewall. Hai cách:

- **Cloudflare Tunnel** (khuyên dùng, không cần IP tĩnh hay mở port): cài `cloudflared` bản Windows, chạy
  `cloudflared.exe service install <TUNNEL_TOKEN>`; trên Cloudflare Zero Trust → Tunnels → Public Hostname:
  `bi.congty.vn` → `HTTP` → `localhost:8000`.
- **IIS (URL Rewrite + ARR) hoặc nginx** làm reverse proxy có chứng chỉ HTTPS.

Sau đó trong `.env`: `BIZAGENT_PUBLIC_URL=https://bi.congty.vn` và `BIZAGENT_BEHIND_PROXY=1`, rồi `Restart-Service Bizagent`.

### Vận hành

| Việc | Lệnh |
|---|---|
| Trạng thái | `Get-Service Bizagent` · `Invoke-RestMethod http://127.0.0.1:8000/api/v1/health` |
| Xem log | `Get-Content C:\bizagent\logs\Bizagent.err.log -Wait -Tail 50` |
| Khởi động lại (sau khi sửa `.env`, `agents.yaml`, `sources.yaml`) | `Restart-Service Bizagent` |
| Cập nhật code từ GitHub | `powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1` |
| Gỡ service (giữ dữ liệu) | `powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1` |
| Sao lưu | Thư mục `workspaces\<ten>\` (cấu hình + `data\`) và file `.env` |

Database chạy cùng máy: bỏ comment dòng `<depend>` trong `deploy\windows\bizagent.xml.template` (tên service
PostgreSQL/SQL Server) rồi chạy lại `install.ps1`, để bizagent khởi động sau database. Tắt chế độ ngủ của máy chủ và
đặt giờ Windows Update khởi động lại ngoài giờ gửi báo cáo.

---

## 🐧 Bản Linux (Docker)

Ubuntu 22.04/24.04, Debian 12, hoặc Linux bất kỳ có **Docker Engine + Docker Compose v2**.
Một container `bizagent` chạy web, bot Telegram và tác vụ nền; thêm Caddy (HTTPS) hoặc Cloudflare Tunnel bằng *profile*.

### Cài đặt

```bash
git clone https://github.com/<tai-khoan>/<repo>.git bizagent && cd bizagent
```

Tạo workspace cho doanh nghiệp (hoặc dùng `demo` để thử) bằng cách chép mẫu rồi sửa:

```bash
cp -r workspaces/_template workspaces/cong_ty_a
```

```bash
./deploy/docker/setup.sh cong_ty_a
```

`setup.sh` tạo `.env` (sinh sẵn `BIZAGENT_SECRET_KEY`, `BIZAGENT_API_TOKEN`, UID/GID để container ghi được
`./workspaces`). Mở `.env` điền API key model và token kênh chat, rồi:

```bash
docker compose up -d --build
```

```bash
docker compose logs -f bizagent
```

Web chạy ở `http://127.0.0.1:8000` trên máy chủ (chỉ mở cục bộ). Workspace `demo` tự tạo dữ liệu mẫu lần đầu.

### Đưa ra Internet

| Cách | Cần | Lệnh |
|---|---|---|
| **Caddy** — HTTPS tự động (Let's Encrypt) | Tên miền trỏ về IP máy chủ, mở cổng 80/443; `.env`: `BIZAGENT_DOMAIN=bi.congty.vn` | `docker compose --profile https up -d` |
| **Cloudflare Tunnel** — không cần mở cổng | `.env`: `CLOUDFLARE_TUNNEL_TOKEN=...`; trên Cloudflare: hostname → `http://bizagent:8000` | `docker compose --profile tunnel up -d` |

Rồi đặt `BIZAGENT_PUBLIC_URL=https://bi.congty.vn` trong `.env` và `docker compose up -d`.

### Tuỳ chọn khi build

| Biến trong `.env` | Tác dụng |
|---|---|
| `BIZAGENT_EXTRAS` | Thư viện cài vào image (mặc định: mọi model AI, PostgreSQL, MySQL, Telegram, Sheets, Excel, MCP) |
| `BIZAGENT_MSSQL=1` | Cài ODBC Driver 18 để đọc SQL Server (đồng nghĩa chấp nhận EULA của Microsoft) |
| `BIZAGENT_PORT`, `BIZAGENT_MEM_LIMIT` | Cổng trên máy chủ, giới hạn RAM (mặc định 2g) |

Database trên chính máy chủ: dùng `host.docker.internal` làm địa chỉ trong `sources.yaml` / `.env`.

### Vận hành

| Việc | Lệnh |
|---|---|
| Trạng thái | `docker compose ps` · `curl -s http://127.0.0.1:8000/api/v1/health` |
| Log | `docker compose logs -f bizagent` |
| Khởi động lại (sau khi sửa `.env`, `agents.yaml`, `sources.yaml`) | `docker compose restart bizagent` |
| Cập nhật code | `git pull && docker compose up -d --build` |
| Chạy lệnh bizagent | `docker compose exec bizagent bizagent check` (tương tự: `sync`, `report ... --preview`, `audit`) |
| Dừng | `docker compose down` (dữ liệu trong `./workspaces` vẫn còn) |
| Sao lưu | Thư mục `workspaces/<ten>/` và file `.env` |

---

## Cấu hình cho doanh nghiệp của bạn

Mọi thứ riêng của một doanh nghiệp nằm trong **một thư mục workspace** — lõi không cần sửa.

```
workspaces/cong_ty_a/
├── workspace.yaml    tên, ngôn ngữ, múi giờ, tiền tệ, role → nguồn được xem, ai được quản trị
├── sources.yaml      database / API / Google Sheets / file / MCP (mật khẩu dùng ${BIEN}, đặt trong .env)
├── agents.yaml       model AI, số bước, timeout, reviewer
├── knowledge/*.md    định nghĩa chỉ số, bối cảnh kinh doanh, SQL mẫu → ghép vào prompt
├── prompts/          ghi đè prompt lõi (Jinja2)
├── datasets.yaml     truy vấn dùng chung cho dashboard, Power BI, báo cáo
├── dashboards.yaml   bật dashboard sống và/hoặc Power BI
├── dashboards/       mỗi dashboard một file YAML (+ JS cho widget tuỳ chỉnh)
├── sync.yaml         lịch đồng bộ nguồn → kho DuckDB
├── reports.yaml      báo cáo định kỳ
├── channels.yaml     bật Zalo OA, Slack
├── users.yaml        người dùng và role (không commit)
├── .env              bí mật riêng của workspace (không commit)
└── data/             state DB, kho DuckDB, file xuất Power BI (tự sinh)
```

Thứ tự nên làm: `sources.yaml` → `knowledge/` → `bizagent check -w cong_ty_a` → `bizagent chat -w cong_ty_a`
để thử hỏi → `datasets.yaml` + `dashboards/` → `reports.yaml`. `bizagent prompt -w cong_ty_a` in ra prompt cuối cùng
agent nhận được.

Ví dụ một nguồn PostgreSQL và đổi model:

```yaml
# sources.yaml
sources:
  sales:
    type: sql
    url: "postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST:-localhost}:5432/${DB_NAME}"
    description: "Dữ liệu bán hàng"
    allowed_tables: ["public.*"]          # bắt buộc: bảng agent được đọc
    mask_columns: ["phone", "email"]      # che trước khi tới AI
```

```yaml
# agents.yaml
default_model: anthropic:claude-sonnet-5-5   # hoặc openai:<model>, google_genai:<model>, deepseek:<model>, ollama:<model>
```

| Tài liệu | Nội dung |
|---|---|
| [docs/HUONG_DAN_CHI_TIET.docx](docs/HUONG_DAN_CHI_TIET.docx?raw=true) | **Hướng dẫn đầy đủ bản Word** (tải về): cài đặt, cấu hình, kênh, bảo mật, vận hành, sự cố |
| [docs/CONNECTORS.md](docs/CONNECTORS.md) | Database, REST API (Base.vn, iPOS, MISA), Google Sheets, CSV/Excel, kho DuckDB và đồng bộ |
| [docs/DASHBOARDS.md](docs/DASHBOARDS.md) | Dashboard sống, Power BI, đăng nhập web |
| [docs/REPORTS.md](docs/REPORTS.md) | Báo cáo định kỳ qua Telegram/Zalo/Slack |
| [docs/CHANNELS.md](docs/CHANNELS.md) | Telegram, Zalo OA, phiên chat, audit |
| [docs/MCP.md](docs/MCP.md) | Dùng MCP server làm nguồn; mở dữ liệu cho Claude Desktop/Cursor |
| [docs/ADMIN.md](docs/ADMIN.md) | Người dùng, cấp quyền có hạn dùng, trang `/admin`, reviewer |
| `workspaces/_template/` | Mẫu có chú thích cho từng file |

---

## Kết nối kênh chat và email

**Telegram** (không cần mở cổng):

1. Trên Telegram mở **@BotFather** → `/newbot` → đặt tên → nhận token.
2. `.env`: `TELEGRAM_BOT_TOKEN=<token>`; cài extras `telegram`; chạy `bizagent run` (hoặc khởi động lại service/container).
3. Nhắn `/myid` cho bot, thêm ID vào `workspaces/<ten>/users.yaml`:
   ```yaml
   users:
     - {id: "telegram:123456789", name: "Giám đốc", role: admin}
   ```
4. Khởi động lại, nhắn `/start` rồi hỏi. Thêm nhân viên sau này: `/adduser <id> <role> [tên]` ngay trong chat
   hoặc trên trang `/admin` (không cần khởi động lại).

**Email báo cáo** (`.env`), người nhận `email:ten@congty.vn` trong `reports.yaml` hoặc `users.yaml`:

| Dịch vụ | `SMTP_HOST` | Cổng / `SMTP_SECURITY` | Mật khẩu |
|---|---|---|---|
| Gmail / Google Workspace | `smtp.gmail.com` | 587 / `starttls` | **App Password** (bật xác minh 2 bước trước) |
| Microsoft 365 | `smtp.office365.com` | 587 / `starttls` | Mật khẩu hộp thư (tài khoản phải bật SMTP AUTH) |

**Zalo OA, Slack:** bật trong `channels.yaml`, webhook `<public_url>/zalo/webhook/<ZALO_WEBHOOK_SECRET>` và
`<public_url>/slack/events` — từng bước trong [docs/CHANNELS.md](docs/CHANNELS.md) và bản Word.

**Ứng dụng khác gọi API:**

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/chat -Headers @{Authorization="Bearer <BIZAGENT_API_TOKEN>"} -ContentType "application/json" -Body '{"message":"doanh thu hôm qua?","thread_id":"erp-1"}'
```

---

## Bảo mật

Trước khi đưa ra Internet:

- [ ] Tài khoản database **chỉ có quyền SELECT**, không đọc được cột cá nhân nếu có thể (lớp bảo vệ quan trọng nhất).
- [ ] `.env` có `BIZAGENT_SECRET_KEY` và `BIZAGENT_API_TOKEN` (trình cài tự sinh); không commit `.env`, `users.yaml`.
- [ ] Chỉ mở qua HTTPS (Cloudflare Tunnel / Caddy / IIS), đặt `BIZAGENT_PUBLIC_URL` bắt đầu bằng `https://`.
- [ ] Tạo tài khoản web có mật khẩu (`bizagent hash-password <ten>` hoặc trang `/admin`); khi đã có tài khoản web,
      trang chat yêu cầu đăng nhập.
- [ ] `allowed_tables` (SQL), `operations` (API), `allowed_tools` (MCP) chỉ gồm thứ agent cần đọc.
- [ ] Bật `reviewer` trong `agents.yaml` để gắn cờ con số không đối chiếu được với dữ liệu.

Code còn có: kiểm tra chỉ-SELECT theo từng loại database, phiên DB chỉ-đọc, timeout và giới hạn dòng, phân quyền
role → nguồn kiểm tra ở server, audit mọi truy vấn. `mask_columns` được che ngay trong câu SQL (đổi tên cột, viết
biểu thức hay UNION đều chỉ thấy `***`). Đăng nhập sai 10 lần trong 15 phút thì tạm khoá; link `/login` dùng một
lần; đăng xuất thu hồi mọi phiên của người đó. `/api/v1/health` không đăng nhập chỉ trả `{"status": "ok"}`.

---

## Lệnh `bizagent`

| Lệnh | Việc |
|---|---|
| `bizagent run` | **Chạy thật**: web + bot Telegram (nếu có token) + tác vụ nền, một tiến trình |
| `bizagent serve` | Chỉ web (chat, dashboard, Power BI, `/admin`, webhook Zalo/Slack) |
| `bizagent telegram` | Chỉ bot Telegram |
| `bizagent chat` | Chat trong terminal |
| `bizagent check` | Kiểm tra model, nguồn dữ liệu, đồng bộ, dashboard, kênh |
| `bizagent init <ten>` | Tạo workspace từ mẫu |
| `bizagent prompt` | In system prompt đã ghép |
| `bizagent sync [job] [--status]` | Đồng bộ dữ liệu về kho |
| `bizagent report <ten> [--preview] [--to id]` | Xem trước / gửi ngay báo cáo |
| `bizagent powerbi-export` | Xuất file Parquet/CSV cho Power BI |
| `bizagent audit [--user id]` | Xem audit log |
| `bizagent hash-password <ten>` | Tạo mật khẩu tài khoản web |
| `bizagent mcp-server --role manager` | MCP server cho Claude Desktop / Cursor |
| `bizagent seed-demo` | Tạo dữ liệu mẫu cho workspace demo |

Mọi lệnh nhận `-w <workspace>` (mặc định lấy `BIZAGENT_WORKSPACE`, rồi `demo`).
Chạy nhiều tiến trình cho cùng một workspace (vd `serve` và `telegram` riêng): đặt `BIZAGENT_BACKGROUND_TASKS=0`
cho mọi tiến trình trừ một, để báo cáo không bị gửi trùng. Dùng `bizagent run` thì không cần.

---

## Khắc phục sự cố

| Hiện tượng / thông báo | Cách xử lý |
|---|---|
| `Thiếu biến môi trường X` | Thêm `X=...` vào `.env` (gốc) hoặc `workspaces/<ten>/.env` |
| `Model ... chưa cài gói` / `chưa đặt biến ..._API_KEY` | `pip install -e ".[anthropic]"` (đổi theo nhà cung cấp), điền key vào `.env` |
| `Từ chối nghe trên 0.0.0.0 khi chưa đặt BIZAGENT_API_TOKEN…` | Đặt `BIZAGENT_API_TOKEN` hoặc tạo tài khoản web; hoặc chỉ nghe `127.0.0.1` |
| `Nghe ngoài 127.0.0.1 cần BIZAGENT_SECRET_KEY` | `python -c "import secrets;print(secrets.token_urlsafe(32))"` → dán vào `.env` |
| Bot Telegram im lặng | ID chưa có trong `users.yaml` (gõ `/myid` để lấy), hoặc đang nhắn trong group (bot chỉ trả lời chat riêng) |
| `Không có quyền truy vấn bảng ...` / `Không có quyền dùng nguồn ...` | Sửa `allowed_tables`, `default_schema` (sources.yaml) hoặc `roles` (workspace.yaml) / `/capquyen` |
| `Kho ... đang bị tiến trình khác khoá` | Server đang chạy: đồng bộ bằng nút **Chạy ngay** trên `/admin` thay vì `bizagent sync` |
| `Câu hỏi xử lý quá lâu` / `vượt số bước` | Thu hẹp kỳ thời gian; tăng `timeout_seconds` / `max_steps`; dùng kho + dataset tổng hợp sẵn |
| Email `Máy chủ mail từ chối đăng nhập` | Gmail cần App Password; kiểm tra `SMTP_USER` / `SMTP_PASSWORD` |
| Báo cáo gửi hai lần | Hai tiến trình cùng chạy tác vụ nền: `BIZAGENT_BACKGROUND_TASKS=0` cho tiến trình phụ |

Luôn chạy `bizagent check -w <ten>` trước tiên: lệnh này kiểm tra model, từng nguồn, đồng bộ, dashboard, kênh, SMTP và
prompt, và chỉ ra dòng nào lỗi. Bảng đầy đủ hơn nằm trong bản Word.

---

## Cấu trúc mã nguồn

```
bizagent/
├── agent/          AgentService (điểm vào cho mọi kênh), graph LangGraph, tool của agent
├── llm/            "provider:model" → chat model, bảng năng lực từng model
├── prompts/        prompt lõi + engine ghi đè theo workspace
├── sources/        nguồn dữ liệu: sql, rest, google_sheets, files, warehouse, mcp
├── security/       kiểm tra chỉ-SELECT theo dialect, che dữ liệu cá nhân
├── dashboards/     dataset, dashboard sống, Power BI
├── channels/       web (chat, dashboard, /admin), Telegram, Zalo, Slack, lệnh chung
├── reports.py      báo cáo định kỳ          sync.py      đồng bộ về kho
├── state.py        phiên chat, audit, người dùng, quyền cấp riêng (SQLite/PostgreSQL)
├── access.py       quản lý người dùng & quyền    webauth.py   đăng nhập web
├── numcheck.py     đối chiếu số                 periods.py   "tuần này", "tháng 10"… → ngày
└── cli.py          lệnh bizagent
deploy/windows/     install.ps1, update.ps1, uninstall.ps1, healthcheck.ps1, mẫu WinSW
deploy/docker/      setup.sh, entrypoint.sh, Caddyfile        (Dockerfile, docker-compose.yml ở thư mục gốc)
workspaces/         _template (mẫu), demo (dữ liệu giả); workspace thật không lên git
docs/  tests/
```

Thêm loại nguồn dữ liệu: kế thừa `bizagent.sources.DataSource` và đăng ký bằng `register_source_type()` hoặc entry point
`bizagent.sources`. Thêm kênh chat: một file trong `bizagent/channels/` gọi `AgentService.ask()`.

### Kiểm thử

```bash
pip install -e ".[dev,anthropic,telegram,excel,mcp]"
```

```bash
pytest
```

Test không gọi model AI thật và không cần mạng. CI (GitHub Actions) chạy ruff + pytest trên Python 3.11–3.13.

---

## Lộ trình

- [x] Khung: workspace, nhiều model AI, prompt Jinja2, nguồn SQL đa dialect, kênh web/CLI/Telegram
- [x] Nguồn dữ liệu: REST API, Google Sheets, CSV/Excel, kho DuckDB + đồng bộ
- [x] Dashboard sống + Power BI
- [x] MCP hai chiều, Zalo OA, phiên chat lưu DB, audit
- [x] Báo cáo định kỳ, `/admin`, Slack, cấp quyền có hạn dùng, reviewer
- [x] Hai phiên bản triển khai: Windows Server (Windows Service) và Linux (Docker)
- [x] Báo cáo định kỳ qua email (SMTP, bản HTML có bảng)
- [ ] Cảnh báo theo ngưỡng (vd doanh thu giảm > 20%, tồn kho dưới mức) gửi ngay qua chat/email
- [ ] Xuất file từ chat (Excel/CSV), ảnh biểu đồ trên Telegram/Zalo · báo cáo PDF / Excel đính kèm email
- [ ] Trang chat web: danh sách và xoá cuộc trò chuyện, hiển thị markdown/bảng, stream câu trả lời, đánh giá 👍/👎
- [ ] Trình cấu hình nguồn dữ liệu trên `/admin` (không phải sửa YAML) · hiển thị giờ theo múi giờ workspace
- [ ] Đăng nhập Google / Microsoft 365, xác thực hai lớp, tự đổi mật khẩu
- [ ] Hỏi đáp tài liệu nội bộ (RAG trên PDF/Word) · lớp định nghĩa chỉ số có cấu trúc, câu hỏi đã lưu
- [ ] `bizagent backup` · chi phí API theo VND / người · nhiều workspace trong một tiến trình
- [ ] Microsoft Teams · bot trong group · tách nhiều agent chuyên trách khi cần

Bản Nyna AI Agent trước đây (bot Telegram riêng cho chuỗi Nyna) được lưu ở tag git `nyna-v1`.

## Giấy phép

MIT — xem [LICENSE](LICENSE).
