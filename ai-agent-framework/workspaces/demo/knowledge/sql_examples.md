Nguồn `sales` là SQLite: dùng `strftime`, `date()`; không có `DATE_TRUNC`.

Doanh thu theo cửa hàng trong một khoảng ngày:

```sql
SELECT s.name AS cua_hang, SUM(o.total_amount) AS doanh_thu, COUNT(*) AS so_don
FROM orders o JOIN stores s ON s.id = o.store_id
WHERE o.status = 'completed' AND o.order_date BETWEEN '2026-11-01' AND '2026-11-07'
GROUP BY s.name ORDER BY doanh_thu DESC;
```

Top món bán chạy:

```sql
SELECT p.name, SUM(i.quantity) AS so_luong, SUM(i.quantity * i.unit_price) AS doanh_thu
FROM order_items i
JOIN orders o ON o.id = i.order_id
JOIN products p ON p.id = i.product_id
WHERE o.status = 'completed' AND o.order_date BETWEEN '2026-11-01' AND '2026-11-07'
GROUP BY p.name ORDER BY so_luong DESC LIMIT 10;
```

Doanh thu theo tháng:

```sql
SELECT strftime('%Y-%m', order_date) AS thang, SUM(total_amount) AS doanh_thu
FROM orders WHERE status = 'completed' GROUP BY thang ORDER BY thang;
```
