# Tri thức nghiệp vụ

Mọi file `*.md` trong thư mục này (trừ README.md) được ghép vào system prompt theo thứ tự tên file.

Gợi ý nên có:
- `glossary.md`: định nghĩa chỉ số (doanh thu, đơn, AOV...).
- `business_context.md`: chuỗi cửa hàng, kênh bán, mùa vụ, quy ước dữ liệu (đơn huỷ, đơn test...).
- `sql_examples.md`: vài câu SQL mẫu đúng cho dữ liệu của bạn — agent học rất nhanh từ ví dụ.

Phần nằm sau dòng `<!-- private -->` trong một file sẽ không được nạp.
Xem prompt cuối cùng agent nhận được: `bizagent prompt -w <ten>`.
