# Kênh chat, phiên trò chuyện, audit

## Phiên trò chuyện lưu trong database

Mọi kênh (web, Telegram, Zalo, terminal) lưu hội thoại vào **state DB** của workspace, nên khởi động lại server
agent vẫn nhớ ngữ cảnh. Mặc định là SQLite trong `workspaces/<ten>/data/`. Muốn dùng PostgreSQL:

```yaml
# workspace.yaml
state:
  url: "postgresql+psycopg://${STATE_DB_USER}:${STATE_DB_PASSWORD}@${STATE_DB_HOST:-localhost}:5432/bizagent_state"
  idle_days: 30               # cuộc trò chuyện không dùng quá số ngày này thì bị xoá
  max_threads_per_user: 20    # vượt thì xoá cuộc cũ nhất
  audit_days: 180
```

PostgreSQL cần `pip install -e ".[postgres-state]"` và tài khoản **có quyền tạo bảng** trong database riêng cho bizagent
(không dùng tài khoản chỉ-đọc của dữ liệu kinh doanh). Server dọn phiên cũ mỗi giờ.

Lệnh dùng chung trên Telegram, Zalo và terminal (xử lý bằng code, không gọi AI):

| Lệnh | Tác dụng |
|---|---|
| `/new` | Bắt đầu cuộc trò chuyện mới |
| `/sessions` | 10 cuộc gần nhất |
| `/chon <số>` | Chuyển sang cuộc khác |
| `/login` | Link đăng nhập dashboard (24 giờ) |
| `/myid` | Xem ID để gửi quản trị viên (người lạ cũng dùng được) |

## Audit log

Mỗi câu hỏi, mỗi câu SQL/API/MCP (kể cả bị từ chối), mỗi lần agent tạo dashboard đều được ghi vào state DB.

```bash
bizagent audit --limit 50
bizagent audit --user telegram:123456789
```

## Telegram

`pip install -e ".[telegram]"`, đặt `TELEGRAM_BOT_TOKEN`, thêm người dùng vào `users.yaml` (`id: "telegram:<id>"`),
chạy `bizagent telegram`. Long polling: không cần mở port.

## Zalo Official Account

> ⚠️ Tài liệu chính thức của Zalo thay đổi theo thời gian. Trước khi chạy thật, đối chiếu với developers.zalo.me:
> định dạng chữ ký webhook, mã lỗi token, quy định gửi tin tư vấn (chỉ trả lời trong khung thời gian sau khi
> người dùng nhắn). Code để các điểm này cấu hình được.

1. Tạo **ứng dụng** trên developers.zalo.me, liên kết với OA, bật quyền gửi tin nhắn tư vấn.
2. Lấy `App ID`, `Secret key` (ứng dụng) và **OA Secret Key** (mục Webhook).
3. Cấp quyền OA cho ứng dụng (OAuth v4) để có **refresh token** ban đầu.
4. Đặt trong `.env`:
   ```env
   ZALO_APP_ID=
   ZALO_APP_SECRET=
   ZALO_OA_SECRET_KEY=
   ZALO_REFRESH_TOKEN=
   ZALO_WEBHOOK_SECRET=        # chuỗi ngẫu nhiên dài, vd python -c "import secrets;print(secrets.token_urlsafe(24))"
   ```
5. Bật kênh trong `workspaces/<ten>/channels.yaml`:
   ```yaml
   zalo:
     enabled: true
     signature: zalo_mac       # zalo_mac | off
   ```
6. Chạy `bizagent serve` sau HTTPS công khai; đặt URL webhook trong trang ứng dụng Zalo:
   `https://<public_url>/zalo/webhook/<ZALO_WEBHOOK_SECRET>`, bật sự kiện **user_send_text**.
7. Người dùng nhắn `/myid` cho OA → quản trị viên thêm `id: "zalo:<id>"` vào `users.yaml`.

Cách hoạt động:
- **Hai lớp bảo vệ webhook**: URL chứa chuỗi bí mật (sai → 404) và chữ ký `X-ZEvent-Signature: mac=<sha256(app_id + body
  + timestamp + OA secret key)>`. Nếu Zalo đổi định dạng chữ ký khiến mọi sự kiện bị từ chối (log báo "sai chữ ký"),
  tạm đặt `signature: off`: lớp URL bí mật vẫn còn.
- Webhook trả 200 ngay, câu trả lời gửi sau (Zalo không phải chờ AI). Sự kiện gửi lại cùng `msg_id` bị bỏ qua.
- Access token tự làm mới bằng refresh token. Refresh token mới (Zalo cấp lại mỗi lần làm mới) được lưu vào state DB,
  nên `ZALO_REFRESH_TOKEN` trong `.env` chỉ dùng lần đầu.
- Tin dài được cắt thành nhiều tin (2.000 ký tự/tin).
- Trạng thái kênh xem ở `GET /api/v1/health` → `channels.zalo.missing_env`; `bizagent check` cũng báo.
