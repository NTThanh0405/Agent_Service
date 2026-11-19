# Báo cáo định kỳ

Báo cáo gửi tự động qua **Telegram, Zalo OA, Slack** theo lịch, khai báo trong `workspaces/<ten>/reports.yaml`.
Số liệu lấy bằng **dataset** (`datasets.yaml`), nên khớp với dashboard và Power BI. Phần số liệu không qua AI.

```yaml
reports:
  bao_cao_sang:
    title: "Báo cáo sáng"
    schedule: {at: "07:00"}                 # mọi ngày
    # schedule: {at: "08:00", days: [mon]}  # mỗi thứ Hai
    # schedule: {at: "09:00", day_of_month: 1}   # ngày 1 hằng tháng
    period: "hôm qua"                       # tuần trước, tháng trước, 7 ngày qua...
    params: {cua_hang: null}                # tham số khác của dataset (null = tất cả)
    recipients: ["telegram:123456789", "zalo:...", "slack:U0123", "role:manager"]
    sections:
      - title: "Kết quả hôm qua"
        dataset: kpi
        kpis:
          - {label: "Doanh thu", value: doanh_thu, compare: doanh_thu_truoc, format: currency}
      - title: "Theo cửa hàng"
        dataset: theo_cua_hang
        table: {columns: [cua_hang, doanh_thu], max_rows: 5, formats: {doanh_thu: currency}}
    commentary: true                        # 2–3 câu nhận xét do AI viết
    dashboard: tong_quan                    # kèm link dashboard (cần public_url)
    max_delay_minutes: 180
```

- `recipients`: ID người dùng (phải có trong `users.yaml` hoặc thêm bằng `/adduser`), hoặc `role:<role>` = mọi người
  có role đó. Người dùng `web:`/`api:` không nhận được (không có kênh chat).
- **Mỗi người chỉ thấy mục thuộc nguồn mình được xem** (role + quyền cấp riêng). Không còn mục nào thì không gửi.
- **Nhận xét AI** (`commentary`): mọi con số trong nhận xét được đối chiếu với số liệu báo cáo; có số lạ thì bỏ nhận xét.
- **Hỏi tiếp**: báo cáo được lưu thành một cuộc trò chuyện mới của người nhận, nhắn tiếp là hỏi trong ngữ cảnh báo cáo
  (không cần lệnh `/reply` như bản cũ). `/sessions` để quay về cuộc khác.
- **Không gửi trùng, gửi bù**: trạng thái lưu trong state DB. Server khởi động lại trong `max_delay_minutes` sau giờ hẹn
  thì gửi bù; muộn hơn thì bỏ qua hôm đó. Gửi lỗi thì thử lại mỗi phút, tối đa 3 lần/ngày.
- Sửa `reports.yaml` có hiệu lực trong 1 phút, không cần khởi động lại.
- Một mục lỗi (nguồn mất kết nối...) hiện "tạm thiếu dữ liệu", các mục khác vẫn gửi.

Lệnh:

```bash
bizagent report bao_cao_sang --preview --to telegram:123456789   # xem nội dung người đó sẽ nhận
bizagent report bao_cao_sang                                     # gửi ngay cho mọi người nhận
```

Trang `/admin` (mục Báo cáo định kỳ) có **Xem trước** theo từng người nhận và **Gửi ngay**.

Kênh gửi cần: Telegram `TELEGRAM_BOT_TOKEN` (người nhận đã nhắn `/start` cho bot); Zalo các biến `ZALO_*`
(Zalo chỉ cho OA nhắn người dùng trong một khoảng thời gian sau lần cuối họ nhắn OA, kiểm tra quy định hiện hành);
Slack `SLACK_BOT_TOKEN`.
