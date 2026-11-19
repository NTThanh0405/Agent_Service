# Quản trị: người dùng, quyền, kiểm soát chất lượng

## Người dùng

Hai nơi khai báo, dùng chung:

| | `users.yaml` | Thêm bằng lệnh / trang `/admin` |
|---|---|---|
| Lưu ở | File trong workspace (không commit) | State DB |
| Sửa | Sửa file | `/adduser`, `/removeuser` hoặc trang `/admin` |
| Ưu tiên | Cao hơn (trùng ID thì dùng file) | |

ID: `telegram:<id>`, `zalo:<id>`, `slack:<U...>`, `web:<tên>` (tài khoản web cần mật khẩu).

## Ai được làm gì (`workspace.yaml`)

```yaml
admin_roles: [admin]           # thêm/xoá người dùng, cấp mọi nguồn, gửi báo cáo, chạy đồng bộ, xem audit
grant_roles: [admin, manager]  # cấp/thu hồi quyền xem nguồn cho người khác (chỉ nguồn chính mình xem được)
```

## Cấp quyền theo người, có hạn dùng

Quyền xem = nguồn theo role (`roles` trong workspace.yaml) **cộng** nguồn được cấp riêng còn hạn. Áp dụng ở mọi nơi:
chat, dashboard, Power BI, báo cáo, nguồn MCP.

| Lệnh (Telegram / Zalo / Slack / terminal) | Tác dụng |
|---|---|
| `/capquyen <id> <nguồn> [số giờ]` | Cấp quyền xem một nguồn; bỏ trống số giờ = không hết hạn |
| `/thuhoi <id> <nguồn\|all>` | Thu hồi |
| `/quyen [id]` | Xem quyền của mình / của người khác |
| `/users` | Danh sách người dùng |
| `/adduser <id> <role> [tên]` | Thêm người dùng (gõ số không có tiền tố = cùng kênh với người gõ) |
| `/removeuser <id>` | Xoá người dùng thêm bằng lệnh (cả quyền cấp riêng của họ) |

Ví dụ: kế toán `telegram:555` role `staff` (không xem gì) cần xem dữ liệu bán hàng 48 giờ để đối soát:
`/capquyen 555 sales 48`. Hết 48 giờ quyền tự mất. Mọi thao tác ghi vào audit.

## Trang `/admin`

Đăng nhập web (role trong `grant_roles`). Gồm: trạng thái nguồn dữ liệu và job đồng bộ (nút **Chạy ngay**),
người dùng (thêm/xoá, đặt mật khẩu web), quyền cấp riêng (cấp/thu hồi), báo cáo định kỳ (**Xem trước**, **Gửi ngay**),
audit log có lọc. Form chống gửi chéo trang (kiểm tra Origin + cookie SameSite).

## Reviewer: đối chiếu số trong câu trả lời

```yaml
# agents.yaml
reviewer:
  enabled: true
  add_note: true     # còn số chưa đối chiếu được thì thêm dòng "⚠️ Lưu ý" cuối câu trả lời
  min_value: 10      # bỏ qua số nhỏ
```

Mọi con số trong câu trả lời (hiểu "41,5 triệu", "1.234.567", "12,3%", "850k"...) phải khớp với dữ liệu tool trả về
trong lượt đó, hoặc một phép tính đơn giản giữa hai số dữ liệu (tỉ lệ, % thay đổi, chênh lệch, tổng, thương).
Bỏ qua ngày, giờ, năm. Chạy bằng code, không gọi AI. Số bị gắn cờ được ghi audit (`review_flag`).

Giới hạn: đây là phép kiểm tra heuristic. Nó bắt được số bịa hoặc chép nhầm, nhưng một số sai vẫn có thể tình cờ
trùng với một phép tính giữa hai số trong cùng dòng/cột (nhất là % làm tròn). Reviewer giảm rủi ro, không thay được
việc người đọc kiểm tra số quan trọng.
