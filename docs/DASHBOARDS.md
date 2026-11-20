# Dashboard: dashboard sống và Power BI

Mỗi workspace chọn một hoặc cả hai cách trong `dashboards.yaml`:

| | Dashboard sống (`live`) | Power BI (`powerbi`) |
|---|---|---|
| Dựng ở đâu | Ngay trong bizagent: file YAML (+ JS nếu muốn) | Power BI Desktop / Service |
| Cập nhật | Tự làm mới mỗi `refresh_seconds`, không hết hạn | Theo lịch refresh của Power BI |
| Tuỳ biến | Sửa YAML/SQL/JS, agent tạo được bằng lời | Toàn bộ sức mạnh của Power BI (DAX, visual...) |
| Chi phí | Không | Cần giấy phép Power BI (Pro/PPU) để chia sẻ |
| Hợp với | Theo dõi hằng ngày, xem trên điện thoại, gửi link qua chat | Phòng phân tích đã quen Power BI |

Cả hai dùng chung **dataset** trong `datasets.yaml`: định nghĩa truy vấn một lần, dùng cho cả hai.

```
workspaces/<ten>/
├── dashboards.yaml            bật/tắt live + powerbi, public_url
├── datasets.yaml              dataset dùng chung
└── dashboards/
    ├── tong_quan.yaml         một dashboard
    └── tien_do_muc_tieu.js    (tuỳ chọn) widget viết bằng code
```

## dashboards.yaml

```yaml
public_url: "https://bi.congty.vn"     # địa chỉ người dùng mở; dùng để tạo link gửi qua chat
live:
  enabled: true
  agent_can_create: [admin]            # role được nhờ agent tạo dashboard
  cache_seconds: 30                    # nhiều người xem cùng lúc chỉ truy vấn database một lần
powerbi:
  enabled: true
  datasets: [doanh_thu_ngay, top_mon]  # dataset mở cho Power BI ("*" = tất cả)
  default_period: "365 ngày qua"
  export: {every: 1h, formats: [parquet, csv], dir: data/powerbi}   # tuỳ chọn
  push: {every: 1h, workspace_id: "<GUID workspace Power BI>", mode: replace}   # tuỳ chọn: tự đẩy lên Power BI Service
  reports:                             # báo cáo Power BI có sẵn: agent gửi link khi được hỏi
    tai_chinh: {title: "Báo cáo tài chính", url: "https://app.powerbi.com/...", roles: [admin]}
```

## datasets.yaml

```yaml
datasets:
  theo_ngay:
    source: sales                       # nguồn trong sources.yaml (sql, warehouse, files, google_sheets)
    description: "Doanh thu theo ngày"
    max_rows: 5000
    sql: |
      SELECT order_date AS ngay, SUM(total_amount) AS doanh_thu
      FROM orders
      WHERE order_date BETWEEN {{start}} AND {{end}}
        AND ({{cua_hang}} IS NULL OR store_name = {{cua_hang}})
      GROUP BY order_date
  de_xuat:                              # dataset từ API (REST)
    source: base_vn
    operation: danh_sach_de_xuat
```

- `{{start}}` `{{end}}` `{{prev_start}}` `{{prev_end}}`: từ bộ lọc `period` (kỳ trước cùng độ dài để so sánh).
- `{{ten_bo_loc}}`: giá trị bộ lọc khác; chọn "Tất cả" thì là `NULL`, nên viết `({{x}} IS NULL OR cot = {{x}})`.
- Tham số khác khai báo trong `inputs: {ten: {type: string|number|date, default: ...}}`.
- Giá trị được chèn dưới dạng **literal SQL đã thoát ký tự** (không ghép chuỗi thô), rồi cả câu đi qua guard
  chỉ-SELECT như mọi truy vấn khác. Người xem phải có quyền với nguồn của dataset.

## dashboards/<slug>.yaml

```yaml
title: "Tổng quan doanh thu"
description: "..."
refresh_seconds: 60                     # tối thiểu 10
roles: [admin, manager]                 # trống = mọi role xem được các nguồn liên quan
filters:
  period: {type: period, label: "Kỳ", default: "30 ngày qua"}
  cua_hang: {type: select, label: "Cửa hàng", dataset: ds_cua_hang, column: cua_hang}   # hoặc options: [...]
datasets: {}                            # dataset riêng của dashboard (cùng cú pháp datasets.yaml)
widgets:
  - {type: kpi, title: "Doanh thu", dataset: kpi, value: doanh_thu, compare: doanh_thu_truoc, format: currency, width: 4}
  - {type: line, title: "Theo ngày", dataset: theo_ngay, x: ngay, y: [doanh_thu], width: 8}
  - {type: area, dataset: theo_ngay_kenh, x: ngay, y: [doanh_thu], series: kenh, stack: true}
  - {type: bar, dataset: theo_cua_hang, x: cua_hang, y: [doanh_thu, so_don], horizontal: false}
  - {type: pie, dataset: theo_kenh, x: kenh, y: [doanh_thu]}
  - {type: table, dataset: top_mon, columns: [mon, so_luong], width: 12}
  - {type: custom, dataset: dat_muc_tieu, script: tien_do_muc_tieu.js, height: 260}
```

| Widget | Trường chính |
|---|---|
| `kpi` | `value` (cột, dòng đầu), `compare` (cột kỳ trước → % thay đổi) |
| `line` `bar` `area` | `x`, `y` (một hay nhiều cột), `series` (tách theo giá trị một cột), `stack`, `horizontal` |
| `pie` | `x` (nhãn), `y[0]` (giá trị) |
| `table` | `columns` (trống = tất cả) |
| `custom` | `script`: file JS trong `dashboards/` |
| chung | `title`, `format` (number/currency/percent/text), `width` (1–12), `height` (px), `note` |

**Sửa file là thấy ngay**: server tự nạp lại khi file đổi. Dashboard lỗi cấu hình không làm hỏng dashboard khác
(admin thấy lỗi ở trang `/dashboards`; `bizagent check` cũng báo).

### Widget viết bằng code

```js
// dashboards/tien_do_muc_tieu.js
window.BizagentWidgets = window.BizagentWidgets || {};
window.BizagentWidgets["tien_do_muc_tieu"] = function (el, ctx) {
  // ctx.data = {columns, rows}; ctx.datasets (mọi dataset của trang); ctx.filters; ctx.echarts; ctx.format(v, kind)
  const chart = ctx.echarts.init(el);
  chart.setOption({ /* tuỳ ý: https://echarts.apache.org/en/option.html */ });
};
```

Biểu đồ dùng Apache ECharts (tải từ cdn.jsdelivr.net). Không tải được thì widget tự chuyển thành bảng số.
Agent **không** được tạo widget custom; chỉ người quản trị viết JS.

### Agent tạo dashboard bằng lời

Người có role trong `agent_can_create` nhắn, ví dụ: *"tạo dashboard doanh thu theo kênh và cửa hàng, làm mới mỗi phút"*.
Agent viết `dashboards/<slug>.yaml` (có `created_by: agent`), chạy thử mọi dataset qua guard và quyền, kiểm tra cột
của từng widget, rồi gửi link. File này sửa tay được; xoá dòng `created_by` để agent không ghi đè nữa.
Agent không ghi đè dashboard do người viết.

## Đăng nhập

Dashboard không hết hạn nên **phải đăng nhập** (không dùng link bí mật như bản cũ):

1. **Tài khoản web**: `bizagent hash-password lan` → dán kết quả vào `users.yaml`:
   ```yaml
   - {id: "web:lan", name: "Lan", role: manager, password_hash: "scrypt$..."}
   ```
2. **Từ Telegram**: gõ `/login` với bot → nhận link đăng nhập (24 giờ) cho đúng người đó. Link do code tạo,
   không đi qua AI.
3. **API token** (`BIZAGENT_API_TOKEN`): header Bearer, hoặc HTTP Basic với mật khẩu = token (cho Power BI).

Đặt `BIZAGENT_SECRET_KEY` trong `.env` (bắt buộc khi chạy ngoài 127.0.0.1) để phiên đăng nhập không mất khi khởi động lại.
Đưa ra Internet: đặt sau HTTPS (Cloudflare Tunnel, nginx, Caddy...) và đặt `public_url` bắt đầu bằng `https://`
(cookie sẽ có cờ Secure).

## Power BI

### Cách 1 — Web connector (khuyên dùng)

1. Mở `https://<public_url>/powerbi`: có URL và **mã Power Query (M) đã đặt kiểu cột** cho từng dataset.
2. Power BI Desktop → **Get data → Web** → dán URL `.../powerbi/<dataset>.csv`
   (hoặc **Blank query → Advanced Editor** → dán mã M).
3. Xác thực **Basic**: tên + mật khẩu tài khoản web; hoặc tên bất kỳ + mật khẩu = `BIZAGENT_API_TOKEN`.
4. Đổi khoảng thời gian bằng tham số URL: `.../powerbi/doanh_thu_ngay.csv?period=năm nay`.
5. Publish lên Power BI Service → Semantic model → Settings → **Data source credentials** (Basic) →
   bật **Scheduled refresh**. Power BI Service gọi từ Internet nên server cần địa chỉ HTTPS công khai;
   nếu server chỉ chạy trong mạng nội bộ, dùng **On-premises data gateway**.

Có cả JSON: `.../powerbi/<dataset>.json`. Mỗi lần tối đa `powerbi.max_rows` dòng (header `X-Bizagent-Truncated: 1`
nếu bị cắt).

### Cách 2 — File theo lịch

Có `powerbi.export` thì server ghi `data/powerbi/<dataset>.parquet` và `.csv` theo lịch (chạy tay:
`bizagent powerbi-export`). Power BI đọc bằng connector **Folder** hoặc **Parquet** (qua gateway nếu dùng Power BI Service).
File được thay một lần (ghi file tạm rồi đổi tên) nên Power BI không đọc phải file đang ghi dở.

### Cách 3 — Tự đẩy lên Power BI Service (push semantic model)

bizagent **tự đẩy** dữ liệu lên Power BI Service theo lịch qua Power BI REST API. Power BI không cần gọi ngược vào
server, nên **không cần** địa chỉ HTTPS công khai, không cần On-premises data gateway, không cần đặt Scheduled refresh:
server chỉ cần ra được Internet tới `login.microsoftonline.com` và `api.powerbi.com`.

Mỗi dataset thành **một bảng** trong **một semantic model kiểu Push** trên workspace Power BI. Lần đẩy đầu, nếu chưa có
model thì bizagent tạo (kiểu cột suy từ dữ liệu: số nguyên → Int64, số thực → Double, ngày/giờ → DateTime, còn lại →
String). Dữ liệu đi qua đúng lớp kiểm soát như dashboard và `/powerbi`: guard chỉ-SELECT, allowlist bảng, **che cột**
`mask_columns` của nguồn.

#### Bước 1 — Tạo app registration (Microsoft Entra ID)

1. Vào [Azure portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**.
   Tên ví dụ `bizagent-powerbi`, *Supported account types*: **Single tenant**, bỏ trống Redirect URI → **Register**.
2. Trang **Overview**: chép **Directory (tenant) ID** và **Application (client) ID**.
3. **Certificates & secrets** → **New client secret** → chọn hạn dùng → chép **Value** (chỉ hiện một lần).
   Ghi lịch đổi secret trước ngày hết hạn (hết hạn thì lần đẩy báo lỗi lấy token).
4. **Không** thêm API permissions cho Power BI: với service principal, Microsoft khuyên không cấp delegated/application
   permission; quyền đến từ tenant setting và vai trò trong workspace (bước 2, 3).
5. (Khuyên dùng) **Groups** → **New group**: Security group, ví dụ `sg-powerbi-api`, thêm app vừa tạo làm member.

#### Bước 2 — Cho phép service principal dùng Power BI API (Power BI admin portal)

Cần tài khoản Fabric/Power BI administrator.

1. [app.powerbi.com](https://app.powerbi.com) → ⚙ **Settings** → **Admin portal** → **Tenant settings**.
2. Mục **Developer settings** → bật setting cho service principal gọi API: tên mới
   **Service principals can call Fabric public APIs** (các tên cũ: *Service principals can use Fabric APIs*,
   *Allow service principals to use Power BI APIs*; Microsoft đổi tên theo đợt cập nhật admin portal).
3. Chọn **Specific security groups** → thêm `sg-powerbi-api` (tránh mở cho cả tổ chức) → **Apply**.
   Thay đổi có thể mất khoảng 15 phút mới có hiệu lực.

#### Bước 3 — Workspace

1. Power BI Service → **Workspaces** → **New workspace** (ví dụ `Bizagent`). **Không dùng My Workspace**: service
   principal không truy cập được My Workspace.
2. Workspace → **Manage access** → **Add people or groups** → tìm tên app (`bizagent-powerbi`) hoặc nhóm
   `sg-powerbi-api` → vai trò **Member** (hoặc Admin) → **Add**.
3. Chép **workspace ID**: mở workspace, URL có dạng `https://app.powerbi.com/groups/<workspace ID>/list`.

#### Bước 4 — Cấu hình bizagent

`.env` (của dự án hoặc `workspaces/<ten>/.env`; không commit):

```
POWERBI_TENANT_ID=<Directory (tenant) ID>
POWERBI_CLIENT_ID=<Application (client) ID>
POWERBI_CLIENT_SECRET=<Value của client secret>
```

`dashboards.yaml`:

```yaml
powerbi:
  enabled: true
  datasets: [doanh_thu_ngay, muc_tieu_thang, top_mon]
  push:
    every: 1h                         # 30m, 1h, 1d
    workspace_id: "1f2e3d4c-...."     # bước 3
    model: "Bán hàng"                 # tên semantic model; trống = "bizagent <tên workspace>"
    datasets: []                      # trống = như powerbi.datasets; "*" = mọi dataset
    mode: replace                     # replace | append
    period: "365 ngày qua"            # kỳ cho {{start}}/{{end}}; trống = powerbi.default_period
    retention: none                   # none | basicFIFO
    batch_rows: 10000                 # dòng mỗi request (tối đa 10.000)
```

| `mode` | Làm gì | Dùng khi |
|---|---|---|
| `replace` | Xoá mọi dòng của bảng rồi đẩy lại toàn bộ kết quả dataset | Mặc định; dataset tổng hợp, vài trăm nghìn dòng trở xuống |
| `append` | Chỉ thêm dòng, không xoá | Dữ liệu mới theo kỳ, vd `period: "hôm qua"` + `every: 1d`; nên kèm `retention: basicFIFO` |

`retention: basicFIFO`: Power BI giữ 200.000 dòng mới nhất mỗi bảng, tự bỏ dòng cũ. `none`: giữ tới 5.000.000 dòng.
Chỉ có tác dụng khi model được tạo; đổi sau đó phải tạo model mới.

#### Bước 5 — Kiểm tra và chạy

```
bizagent check                 # dòng "Power BI push (...): kết nối workspace ... OK"
bizagent powerbi-push --check  # chỉ thử token + quyền vào workspace, không đẩy
bizagent powerbi-push          # đẩy ngay một lần
```

Sau đó `bizagent run` / `serve` tự đẩy theo `every` (tôn trọng `BIZAGENT_BACKGROUND_TASKS=0`: chỉ một tiến trình đẩy).
Trang **`/admin`** có mục *Power BI Service (tự đẩy)*: lần đẩy cuối, kết quả từng dataset, nút **Đẩy ngay**.
Trạng thái cũng ghi ở `data/powerbi/_push_state.json`.

Trên Power BI Service: workspace → semantic model vừa tạo → **Create report** (hoặc Power BI Desktop → *Power BI
semantic models* → chọn model). Report thấy dữ liệu mới sau mỗi lần đẩy, không cần refresh.

#### Giới hạn của Power BI (bizagent tự tuân thủ)

| Giới hạn | bizagent làm gì |
|---|---|
| 10.000 dòng mỗi request POST rows; ~16 MB mỗi request | Chia lô theo `batch_rows` (≤ 10.000) và ≤ 15 MB |
| 120 request POST rows mỗi phút mỗi model | Tự chờ khi chạm ngưỡng; HTTP 429/5xx thì chờ (theo `Retry-After`) rồi thử lại |
| 1.000.000 dòng thêm vào mỗi giờ mỗi model | Đếm số dòng đã đẩy trong 1 giờ qua; vượt thì **không xoá, không đẩy** dataset đó, báo lỗi, lần sau thử lại |
| 5.000.000 dòng mỗi bảng (`none`) / 200.000 (`basicFIFO`) | Lấy tối đa chừng ấy dòng (và `powerbi.max_rows`); bị cắt thì kết quả ghi "BỊ CẮT" |
| 75 bảng, 75 cột | Báo lỗi cấu hình / lỗi dataset |
| Chuỗi tối đa 4.000 ký tự | Cắt bớt |

Lưu ý:

- **Không thêm được bảng** vào push model đã tạo. Thêm dataset vào danh sách sau lần đẩy đầu thì dataset đó báo lỗi;
  đặt `model` tên mới (bizagent tạo model mới) hoặc xoá model cũ trên Power BI (report dựng trên model cũ cũng mất).
  Vì vậy nếu lần đầu có dataset lỗi, bizagent **chưa tạo** model. Thêm/đổi **cột** thì bizagent tự cập nhật schema bảng.
- `replace` xoá rồi mới đẩy: trong lúc đẩy, report có thể thấy bảng thiếu dòng. Lỗi giữa chừng thì bảng thiếu dữ liệu
  tới lần đẩy sau.
- Push model không có Power Query, không refresh từ nguồn; quan hệ giữa các bảng và measure dựng trong report.
  Cần mô hình phức tạp thì dùng cách 1 hoặc 2.
- Microsoft thông báo **ngừng cho tạo mới** push semantic model sau **31/10/2027** (model đã tạo vẫn chạy). Sau mốc
  đó, model mới nên dùng cách 1/2 hoặc Real-Time Intelligence của Microsoft Fabric.
- Cloud riêng (US Gov, China): đổi `push.authority` và `push.api_url`.

Tài liệu Microsoft: [Push datasets REST API](https://learn.microsoft.com/rest/api/power-bi/push-datasets),
[giới hạn push semantic model](https://learn.microsoft.com/power-bi/developer/embedded/push-datasets-limitations),
[service principal](https://learn.microsoft.com/power-bi/developer/embedded/embed-service-principal).

### Cách 4 — Power BI nối thẳng database

Power BI kết nối trực tiếp được PostgreSQL / MySQL / SQL Server. Khi đó bizagent không tham gia; nên tạo
tài khoản chỉ-đọc riêng cho Power BI. Kho DuckDB (`warehouse`) thì nên dùng cách 1, 2 hoặc 3 vì DuckDB chỉ cho
một tiến trình mở file để ghi.

### Không làm

- **Streaming dataset** realtime (từng giây, tile streaming): Microsoft đang ngừng tính năng này. Muốn dashboard
  realtime thì dùng dashboard sống; cách 3 đẩy theo lịch (từ vài phút một lần).
- **Nhúng báo cáo Power BI vào trang bizagent** (Power BI Embedded): cần capacity trả phí riêng; thay vào đó
  khai báo `powerbi.reports` để agent và trang `/dashboards` gửi link báo cáo.
