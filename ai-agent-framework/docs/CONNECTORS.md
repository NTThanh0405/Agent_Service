# Kết nối nguồn dữ liệu

Mọi nguồn khai báo trong `workspaces/<ten>/sources.yaml`. Bí mật (token, mật khẩu) **không** ghi trong YAML:
dùng `${TEN_BIEN}` hoặc `value_env: TEN_BIEN` và đặt giá trị trong `.env`.

| `type` | Dùng cho | Agent hỏi bằng |
|---|---|---|
| `sql` | PostgreSQL, MySQL, SQL Server, SQLite, Oracle... | `run_sql` |
| `rest` | API trả JSON: Base.vn, iPOS, MISA, CRM, sàn TMĐT... | `call_api` |
| `google_sheets` | Google Sheets | `run_sql` (DuckDB) |
| `files` | Thư mục CSV / Excel | `run_sql` (DuckDB) |
| `warehouse` | Kho DuckDB nội bộ, nhận dữ liệu đồng bộ | `run_sql` (DuckDB) |

> ⚠️ Các mẫu Base.vn / iPOS / MISA dưới đây chỉ là **khung**: đường dẫn, tên tham số và cách xác thực phải điền
> theo tài liệu API chính thức mà nhà cung cấp cấp cho tài khoản của bạn. Hãy dùng token **chỉ có quyền đọc**.

---

## REST API (`type: rest`)

Agent chỉ gọi được các `operations` khai báo sẵn, trên đúng `base_url`, không gọi được URL tuỳ ý.

```yaml
ten_nguon:
  type: rest
  base_url: "https://api.example.com"
  description: "Mô tả để agent biết nguồn này có gì"
  auth: {type: bearer, value_env: EXAMPLE_TOKEN}
  timeout_seconds: 20
  retries: 2                  # thử lại khi 429/5xx/mất kết nối (tôn trọng Retry-After)
  rate_limit_per_minute: 60
  cache_seconds: 60           # cùng câu hỏi trong 60 giây không gọi lại API
  max_rows: 200
  max_days: 366               # khoảng ngày dài nhất một lần gọi
  mask_columns: ["phone", "email"]
  operations:
    orders:
      description: "Đơn hàng trong khoảng ngày"
      method: GET
      path: "/v1/orders"
      params: {from: "{start}", to: "{end}", page: "{page}", limit: 100}
      inputs:
        status: {type: string, enum: [new, done, cancelled]}
      result_path: "data.items"
      fields: {ma_don: id, ngay: created_at, tong_tien: total, khach: customer.name}
      pagination: {type: page, start: 1, size: 100, max_pages: 50}
```

**Xác thực (`auth.type`)**

| Kiểu | Khai báo | Gửi đi |
|---|---|---|
| `none` | | |
| `bearer` | `value_env` | `Authorization: Bearer <token>` |
| `header` | `header`, `value_env`, `prefix` | `<header>: <prefix><token>` |
| `query` | `param`, `value_env` | `?<param>=<token>` |
| `body` | `param`, `value_env` | token trong body POST (form/json); với GET thành tham số URL |
| `basic` | `user_env`, `password_env` | HTTP Basic |

**Biến trong `params` và `path`:** `{start}` `{end}` (YYYY-MM-DD), `{start_dmy}` `{end_dmy}` (DD/MM/YYYY),
`{start_ts}` `{end_ts}` (unix giây), `{page}` `{offset}` `{cursor}` `{size}`, và tên các `inputs`.
Tham số có biến chưa có giá trị bị bỏ khỏi request. Viết `"{page}"` đứng một mình thì giữ kiểu số.

**Khoảng ngày:** agent truyền `period` ("tháng này", "7 ngày qua"...) hoặc `start`/`end`; code đổi ra ngày.

**Phân trang (`pagination.type`):** `none` · `page` (`start`, `size`, `param`?) · `offset` (`param`, `size`, `size_param`)
· `cursor` (`next_path` = đường dẫn tới cursor trang sau trong JSON, `param` = tên tham số gửi cursor).

**Kết quả:** `result_path` trỏ tới mảng bản ghi; `fields` chọn và đổi tên cột (đường dẫn JSON có dấu chấm).
Không khai báo `fields` thì JSON được làm phẳng (`customer.name`). Agent có thể truyền thêm `sql` cho `call_api`
để gộp/lọc kết quả bằng SQL DuckDB (bảng `result`).

### Base.vn (khung)

```yaml
base_vn:
  type: rest
  base_url: "${BASE_API_URL}"               # địa chỉ API theo tài liệu Base cấp cho bạn
  description: "Base: đề xuất, công việc, nhân sự"
  auth: {type: body, param: access_token, value_env: BASE_ACCESS_TOKEN}
  operations:
    danh_sach_de_xuat:
      description: "Danh sách đề xuất"
      method: POST
      body_format: form
      path: "<đường dẫn theo tài liệu Base>"
      params: {page: "{page}"}
      result_path: "<khoá chứa danh sách>"
      pagination: {type: page, start: 1, max_pages: 20}
```

### iPOS (khung)

```yaml
ipos:
  type: rest
  base_url: "${IPOS_API_URL}"
  description: "iPOS: doanh thu, hoá đơn theo cửa hàng"
  auth: {type: header, header: "<tên header>", value_env: IPOS_TOKEN}
  operations:
    doanh_thu:
      description: "Doanh thu theo ngày, theo cửa hàng"
      method: GET
      path: "<đường dẫn theo tài liệu iPOS>"
      params: {from_date: "{start_dmy}", to_date: "{end_dmy}"}
      result_path: "<khoá chứa danh sách>"
```

Nếu iPOS đã đổ dữ liệu vào database (như bản Nyna: PostgreSQL schema `dwh`), dùng `type: sql` sẽ tốt hơn gọi API.

### MISA (khung)

```yaml
misa:
  type: rest
  base_url: "${MISA_API_URL}"
  description: "MISA: số liệu kế toán đã ghi sổ"
  auth: {type: bearer, value_env: MISA_ACCESS_TOKEN}
  operations:
    tong_hop_doanh_thu:
      description: "Tổng hợp doanh thu kế toán trong khoảng ngày"
      method: POST
      body_format: json
      path: "<đường dẫn theo tài liệu MISA>"
      params: {from_date: "{start}", to_date: "{end}"}
      result_path: "<khoá chứa danh sách>"
```

Nếu API cần lấy token theo phiên (đăng nhập bằng app id/secret rồi mới có access token), có hai cách:
đặt token vào `.env` bằng một script lấy token định kỳ, hoặc viết plugin Python kế thừa `RestSource`
và ghi đè `_auth_parts()` (xem phần Plugin).

---

## Google Sheets (`type: google_sheets`)

1. Google Cloud Console → tạo project → bật **Google Sheets API** → tạo **Service account** → tạo key JSON.
2. Share Google Sheet cho email của service account, quyền **Người xem**.
3. `pip install -e ".[gsheets]"`, đặt `GOOGLE_APPLICATION_CREDENTIALS=đường/dẫn/key.json` trong `.env`.

```yaml
ke_hoach:
  type: google_sheets
  spreadsheet_id: "${KE_HOACH_SHEET_ID}"       # phần giữa /d/ và /edit trong link
  auth: {type: service_account}                 # hoặc {type: api_key, key_env: GOOGLE_SHEETS_API_KEY} cho sheet công khai
  tables:                                       # bỏ trống = mỗi tab là một bảng
    muc_tieu: {range: "Mục tiêu!A1:H"}
  cache_seconds: 300
```

Dòng đầu là tiêu đề cột. Tên được chuẩn hoá: "Doanh thu (VNĐ)" → `doanh_thu_vnd`; `describe_table` cho biết tên gốc.

## File CSV / Excel (`type: files`)

```yaml
bao_cao:
  type: files
  path: "${WORKSPACE_DIR}/files"
  csv: {delimiter: ",", encoding: "utf-8-sig"}
```

Mỗi file `.csv` là một bảng; mỗi sheet trong `.xlsx` là bảng `<ten_file>__<ten_sheet>` (cần `pip install -e ".[excel]"`).

---

## Kho dữ liệu và đồng bộ (`type: warehouse` + `sync.yaml`)

Dùng kho khi cần: **ghép dữ liệu nhiều nguồn** (doanh thu POS × mục tiêu trong Sheets × chi phí từ MISA),
**dữ liệu lịch sử dài** mà API chỉ cho lấy từng đợt, hoặc **giảm số lần gọi API**.

```yaml
# sources.yaml
warehouse:
  type: warehouse
  path: "${WORKSPACE_DIR}/data/warehouse.duckdb"
  description: "Kho tổng hợp"
```

```yaml
# sync.yaml
warehouse: warehouse
run_in_server: true            # serve/telegram tự chạy lịch
jobs:
  doanh_thu_ngay:              # từ database
    from: {source: sales, sql: "SELECT ... GROUP BY ..."}
    table: doanh_thu_ngay
    mode: replace
    every: 1h
  de_xuat:                     # từ API: mỗi lần lấy 30 ngày gần nhất, cập nhật theo id
    from: {source: base_vn, operation: danh_sach_de_xuat, window_days: 30}
    table: base_de_xuat
    mode: upsert
    key: [id]
    every: 2h
  muc_tieu:                    # từ Google Sheets / file
    from: {source: ke_hoach, table: muc_tieu}
    table: muc_tieu
    mode: replace
    at: "06:00"                # mỗi ngày lúc 6h (thay cho every)
```

- `mode`: `replace` (ghi đè cả bảng) · `append` (thêm vào) · `upsert` (thay dòng trùng `key`, thêm dòng mới).
- Đồng bộ dùng cùng lớp bảo vệ: SQL qua guard, cột trong `mask_columns` bị che **trước khi** ghi vào kho.
- Mỗi kết quả agent đọc từ kho kèm "Dữ liệu kho đồng bộ lúc ..." để câu trả lời nói rõ độ mới của dữ liệu.
- Lệnh: `bizagent sync` (chạy mọi job), `bizagent sync <job>`, `bizagent sync --due`, `bizagent sync --status`.
  Trạng thái cũng có ở `GET /api/v1/health`.
- DuckDB chỉ cho một tiến trình ghi: khi server đang chạy (và `run_in_server: true`), để server tự đồng bộ.

---

## Plugin: loại nguồn mới bằng Python

```python
from bizagent.sources import DataSource, QueryResult, register_source_type

class CrmSource(DataSource):
    kind = "sql"            # hoặc "rest"
    def health(self): ...
    def list_tables(self): ...
    def describe_table(self, table): ...
    def query(self, query, max_rows=None) -> QueryResult: ...

register_source_type("my_crm", CrmSource)
```

Hoặc đóng gói thành thư viện và khai báo entry point `bizagent.sources` trong `pyproject.toml` của bạn.
Cách nhanh cho nguồn dạng bảng: kế thừa `bizagent.sources.tabular.TabularSource`, chỉ cần viết
`table_names()` và `fetch_table()`; phần chạy SQL, cache, che cột đã có sẵn.
