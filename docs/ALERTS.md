# Cảnh báo theo ngưỡng

Cảnh báo kiểm tra một **dataset** (`datasets.yaml`) theo lịch; khi chỉ số vượt ngưỡng thì gửi ngay một tin ngắn qua
**Telegram, Zalo OA, Slack, email**. Khai báo trong `workspaces/<ten>/alerts.yaml`. Khác với
[báo cáo định kỳ](REPORTS.md) (gửi đều đặn), cảnh báo chỉ gửi khi có chuyện, và mỗi người tối đa một lần mỗi kỳ dữ liệu.

```yaml
alerts:
  doanh_thu_giam:
    title: "Doanh thu hôm qua giảm mạnh"
    dataset: kpi                            # tên trong datasets.yaml
    period: "hôm qua"                       # hôm nay, tuần này, tháng này, 7 ngày qua...
    params: {cua_hang: null}                # tham số khác của dataset (null = tất cả)
    condition: "pct_change(doanh_thu, doanh_thu_truoc) <= -20"
    label: "Doanh thu so với hôm trước"     # tên chỉ số trong tin (mặc định: vế trái điều kiện)
    format: number                          # number | currency | percent (pct_change luôn hiện %)
    check: {at: "09:00"}                    # hoặc {every: 30m, between: "08:00-22:00"}
    recipients: ["role:admin", "telegram:123456789", "email:giamdoc@congty.vn"]
    dashboard: tong_quan                    # kèm link dashboard (cần public_url)
    # cooldown: 6h                          # gửi lại sau 6 giờ nếu vẫn vượt ngưỡng (mặc định: một lần mỗi kỳ)
    # enabled: false                        # tạm tắt
```

Tin cảnh báo:

```
🚨 Cảnh báo: Doanh thu hôm qua giảm mạnh
Doanh thu so với hôm trước: -27,4%
Ngưỡng: ≤ -20,0%
Kỳ dữ liệu: 18/11/2026
🔗 Dashboard: https://bi.congty.vn/d/tong_quan
Nhắn tiếp để hỏi thêm.
```

## Điều kiện

Điều kiện là **một phép so sánh** viết theo cú pháp khai báo, không phải biểu thức Python, nên không chạy được code:

```
<vế> <so sánh> <vế>        so sánh: <  <=  >  >=  ==  !=  (hoặc ≤ ≥ ≠, "=" như "==")
```

Mỗi vế là một trong:

| Vế | Ví dụ | Nghĩa |
|---|---|---|
| hằng số | `10`, `-20`, `0.8`, `0,8`, `30%` | `30%` đứng một mình = 30 (so với cột đã tính theo %) |
| tên cột | `ton_kho`, `doanh_thu_truoc` | cột của dataset |
| hệ số × cột | `0.8 * doanh_thu_truoc`, `80% * doanh_thu_truoc` | `80%` trong phép nhân = 0,8 |
| % thay đổi | `pct_change(doanh_thu, doanh_thu_truoc)` | (doanh_thu − doanh_thu_truoc) / doanh_thu_truoc × 100, kỳ trước âm thì lấy trị tuyệt đối |

Ví dụ:

```yaml
condition: "doanh_thu < 0.8 * doanh_thu_truoc"                  # thấp hơn 80% kỳ trước
condition: "pct_change(doanh_thu, doanh_thu_truoc) <= -20"      # giảm từ 20% trở lên
condition: "pct_change(so_don, so_don_truoc) >= 50"             # tăng đột biến từ 50%
condition: "ton_kho < 10"                                       # dưới mức tồn tối thiểu
condition: "ty_le_huy > 5"                                      # tỷ lệ huỷ đơn quá 5 (%)
```

- Ô trống (NULL) hoặc kỳ trước bằng 0 (với `pct_change`) thì **không** tính là vượt ngưỡng. Muốn coi trống là 0,
  viết `COALESCE(SUM(...), 0)` trong SQL của dataset.
- `bizagent check` báo lỗi cú pháp, dataset hoặc dashboard không tồn tại, kỳ không hiểu được.
- Cột không có trong kết quả dataset thì lần kiểm tra báo lỗi (xem trên `/admin` hoặc `bizagent alert`).

## Dataset nhiều dòng

Dataset trả nhiều dòng (tồn kho theo món, tiến độ theo cửa hàng...) thì **xét từng dòng**; tin liệt kê các dòng vượt
ngưỡng (tối đa `max_rows`, mặc định 5).

```yaml
  ton_kho_thap:
    title: "Món sắp hết hàng"
    dataset: ton_kho                        # SELECT ten_mon, so_luong AS ton_kho FROM ton_kho
    period: "hôm nay"
    condition: "ton_kho < 10"
    label_column: ten_mon                   # cột tên dòng; mặc định là cột đầu tiên
    check: {every: 30m, between: "08:00-21:00"}
    cooldown: 4h
    recipients: ["role:manager"]
```

```
🚨 Cảnh báo: Món sắp hết hàng
ton_kho < 10: 2/48 dòng
• Sữa tươi: 4
• Hạt cà phê Arabica: 7
Kỳ dữ liệu: 20/11/2026
```

## Lịch kiểm tra

| `check` | Nghĩa |
|---|---|
| `{at: "09:00"}` | mỗi ngày lúc 9h (theo `timezone` của workspace) |
| `{at: "09:00", days: [mon, thu]}` | thứ Hai và thứ Năm |
| `{every: 30m}` | mỗi 30 phút (`30m`, `2h`, `1d`) |
| `{every: 30m, between: "08:00-22:00"}` | mỗi 30 phút trong giờ làm việc |

Không khai báo `check` thì mặc định mỗi giờ. Với `at`, server khởi động lại trong `max_delay_minutes` (mặc định 180)
sau giờ hẹn thì kiểm tra bù; muộn hơn thì bỏ qua hôm đó. Lần kiểm tra lỗi (nguồn mất kết nối, gửi lỗi) được thử lại
ở vòng sau (mỗi phút). Sửa `alerts.yaml` có hiệu lực trong 1 phút, không cần khởi động lại.

## Không gửi lặp

- Mặc định mỗi người nhận **tối đa một lần mỗi kỳ dữ liệu** (`period`): `period: "hôm qua"` kiểm tra mỗi 30 phút
  vẫn chỉ gửi một lần cho ngày hôm qua; sang ngày mới là kỳ mới.
- `cooldown: 6h` thay quy tắc trên: gửi lại khi vẫn vượt ngưỡng và đã quá 6 giờ từ lần gửi trước (kể cả cùng kỳ).
- Gửi lỗi cho một người thì thử lại ở lần kiểm tra sau, tối đa 3 lần mỗi kỳ; người khác không bị gửi lại.
- Trạng thái lưu trong state DB (`alert_sent:*`, `alert_check:*`), nên khởi động lại server không gửi lại.
- Chạy nhiều tiến trình cho cùng workspace: đặt `BIZAGENT_BACKGROUND_TASKS=0` cho mọi tiến trình trừ một
  (như báo cáo định kỳ). Dùng `bizagent run` thì không cần.

## Người nhận và quyền

`recipients` cùng dạng `reports.yaml`: ID người dùng (`telegram:`, `zalo:`, `slack:`, `email:`) hoặc `role:<role>`.

- Người nhận phải có role (trong `users.yaml` hoặc thêm bằng `/adduser`, trang `/admin`).
- **Chỉ ai xem được nguồn của dataset** (role + quyền cấp riêng còn hạn) mới nhận cảnh báo; người khác bị bỏ qua.
- Link dashboard chỉ gửi cho người xem được dashboard đó.
- Chat (Telegram/Zalo/Slack): tin cảnh báo thành một cuộc trò chuyện mới, nhắn tiếp là hỏi trong ngữ cảnh cảnh báo
  ("vì sao giảm?"). Email: dòng đầu tin là tiêu đề thư; kênh email cần biến `SMTP_*` (xem [REPORTS.md](REPORTS.md#gửi-qua-email)).

## Lệnh và trang quản trị

```bash
bizagent alert                                   # danh sách cảnh báo, lịch, kết quả lần kiểm tra cuối
bizagent alert doanh_thu_giam --preview          # kiểm tra, in tin từng người sẽ nhận, KHÔNG gửi
bizagent alert doanh_thu_giam                    # kiểm tra và gửi ngay (vẫn chống gửi lặp)
bizagent alert doanh_thu_giam --to telegram:123 --force   # gửi thử cho một người, bỏ qua chống gửi lặp
```

Trang `/admin` (mục **Cảnh báo theo ngưỡng**, chỉ role trong `admin_roles`): điều kiện, lịch, người nhận, lần kiểm tra
cuối và nút **Kiểm tra ngay** (vẫn chống gửi lặp). Mỗi lần gửi ghi audit `alert_sent`; bấm nút ghi `alert_manual`.
