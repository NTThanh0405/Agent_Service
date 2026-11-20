# Nhiều agent chuyên trách: phân tích, đánh giá, đề xuất

Mặc định bizagent dùng **một agent**: một model vừa truy vấn dữ liệu vừa trả lời. Bật chế độ **nhiều agent** để
tách việc cho các agent chuyên trách, mỗi agent một prompt và (tuỳ chọn) một model riêng. Mọi kênh (web, Telegram,
Zalo, Slack, terminal, REST API) vẫn đi qua `AgentService.ask()` nên không phải sửa gì ở kênh.

```
câu hỏi ─► router ──phan_tich / tra_cuu──► analyst ⇄ tool dữ liệu ──(phan_tich + có số liệu)──► advisor ─┐
                 └──dashboard───────────► designer ⇄ tool dashboard ─────────────────────────────────────┤
                                                                                     reviewer đối chiếu số ◄┘
```

| Agent | Việc | Gọi tool | Gợi ý model |
|---|---|---|---|
| `router` | Đọc câu hỏi (kèm vài lượt hội thoại trước, dạng chữ), chọn đường: `phan_tich`, `tra_cuu`, `dashboard` | Không | Rẻ, nhanh (vd `fast`) |
| `analyst` | Truy vấn dữ liệu bằng tool (`get_date_range`, `run_sql`, `call_api`, tool MCP...), trình bày số liệu | Có | Mạnh nhất (viết SQL) |
| `advisor` | Viết phần **Đánh giá & đề xuất** CHỈ dựa trên kết quả tool analyst vừa lấy và câu trả lời của analyst | Không | Mạnh / vừa |
| `designer` | Tìm dashboard có sẵn (`list_dashboards`) hoặc tạo mới (`create_dashboard`) | Có | Vừa |

- **`phan_tich`** (mặc định khi router không chắc): analyst lấy số liệu (kỳ hiện tại và kỳ trước để so sánh), rồi
  advisor nối phần đánh giá và 2–3 đề xuất hành động vào cuối câu trả lời.
- **`tra_cuu`**: chào hỏi, hỏi cách dùng, hỏi danh sách nguồn/bảng, hoặc người dùng chỉ cần con số. Chỉ analyst trả lời.
- **`dashboard`**: designer xử lý. Khi có designer, analyst không được dùng `create_dashboard` (vẫn gửi được link có sẵn).
- Advisor **không chạy** khi lượt này không lấy được số liệu (chào hỏi, bị từ chối quyền, truy vấn lỗi) — đỡ tốn
  token và không có đề xuất "suông". Advisor gặp lỗi thì người dùng vẫn nhận phần số liệu của analyst.
- **Reviewer** (`reviewer.enabled`) vẫn chạy cuối như chế độ một agent, trên toàn bộ câu trả lời kể cả phần đề xuất:
  con số advisor viết mà không có trong dữ liệu sẽ bị gắn "⚠️ Lưu ý".

## Cấu hình (`agents.yaml`)

```yaml
default_model: anthropic:claude-sonnet-5-5
models:
  fast: anthropic:claude-haiku-5-5
  strong: anthropic:claude-opus-5-5

agent:                     # giá trị chung; mục nào của từng agent để trống thì lấy ở đây
  model: default
  max_steps: 12
  timeout_seconds: 120     # giới hạn cho CẢ câu hỏi (mọi agent cộng lại)
  max_tokens: 3000

multi_agent:
  enabled: true            # false (mặc định) = một agent như cũ
  router:   {model: fast, max_tokens: 50}
  analyst:  {model: strong, max_steps: 12}
  advisor:  {model: default, max_tokens: 1200}
  designer: {enabled: true, max_steps: 8}
```

Mỗi agent nhận: `enabled`, `model` (alias trong `models`, `default` hoặc `provider:model`), `max_steps` (số vòng gọi
tool tối đa trong một câu hỏi — chỉ có nghĩa với analyst và designer), `max_tokens`, `temperature`.

- `router.enabled: false`: bỏ router (đỡ một lượt gọi model); mọi câu đi analyst → advisor, và designer cũng tắt
  theo (analyst tự làm dashboard).
- `advisor.enabled: false`: không có phần đề xuất (giống một agent, nhưng vẫn có router/designer).
- `designer.enabled: false`: câu hỏi dashboard đi analyst, analyst được dùng cả tool dashboard. Workspace chưa bật
  dashboard (`dashboards.yaml`, không có tool `create_dashboard`) thì designer tự tắt.
- `analyst` luôn bật (là agent truy vấn dữ liệu). Model của analyst và designer phải hỗ trợ gọi tool; router và
  advisor thì không cần (dùng được model rẻ, model chạy local...).

`bizagent check` kiểm tra model của từng agent (gói, API key) và dựng thử prompt của từng agent.

## Prompt từng agent

Prompt lõi trong `bizagent/prompts/`, ghi đè theo workspace ở `workspaces/<x>/prompts/` giống `system.md.j2`:

| File | Nội dung | Biến thêm |
|---|---|---|
| `router.md.j2` | Cách chọn đường | `routes` (các đường đang có) |
| `analyst.md.j2` | `{% extends "system.md.j2" %}` + vai trò phân tích | `route`, `advisor_follows` |
| `advisor.md.j2` | Quy tắc đánh giá, đề xuất (block `identity`, `advice_rules`, `business_rules`, `knowledge`) | |
| `designer.md.j2` | `{% extends "system.md.j2" %}` + cách chọn/tạo dashboard | `route` |

Analyst và designer **kế thừa `system.md.j2`** (kể cả bản ghi đè của workspace), nên quy tắc nghiệp vụ, nguồn dữ
liệu theo role và `knowledge/*.md` giữ nguyên. Advisor cũng nhận `knowledge/*.md`. Thêm luật riêng cho phần đề xuất:

```jinja
{# workspaces/<x>/prompts/advisor.md.j2 #}
{% extends "core/advisor.md.j2" %}
{% block business_rules %}
6. Đề xuất khuyến mãi phải nêu chi phí dự kiến; không đề xuất giảm giá quá 20%.
{% endblock %}
```

Xem prompt đã ghép: `bizagent prompt -w <x> --agent advisor` (`router`, `analyst`, `designer`).

## Quyền, an toàn

Không đổi so với một agent: tool đọc role người hỏi từ `config["configurable"]` (do server đặt, không đến từ tin
nhắn), chỉ cho dùng nguồn role đó được xem; guard chỉ-SELECT, che cột, giới hạn dòng, timeout truy vấn giữ nguyên.
Router và advisor không có tool nên không chạm được dữ liệu; advisor chỉ thấy kết quả analyst đã lấy với quyền
của người hỏi.

## Token và chi phí

- Mỗi lượt gọi model của mọi agent đều được đếm (từ `usage_metadata` model báo về), kể cả lượt router và advisor.
  Tổng ghi vào audit (`tokens_in`, `tokens_out`), cộng vào hạn mức `daily_tokens_per_user`.
- Ở chế độ nhiều agent, audit `ask` có thêm `route` và `agents`, ví dụ
  `{"router": {"calls": 1, "tokens_in": 820, "tokens_out": 3}, "analyst": {"calls": 4, ...}, "advisor": {...}}`.
- So với một agent: thêm một lượt router (nhỏ) và một lượt advisor cho câu `phan_tich` có số liệu. Dùng model rẻ cho
  router và giới hạn `max_tokens` của advisor để kiểm soát chi phí.
- Câu hỏi bị timeout: các lượt gọi model đã xong vẫn được tính.
