Kho `warehouse` (DuckDB, cú pháp DuckDB: `date_trunc`, `strftime`):

- `doanh_thu_ngay(ngay DATE, ma_cua_hang, cua_hang, kenh, so_don, doanh_thu)`: chỉ đơn hoàn thành.
- `muc_tieu_thang(thang 'YYYY-MM', ma_cua_hang, cua_hang, muc_tieu_doanh_thu_vnd, muc_tieu_so_don)`: từ file kế hoạch.

So sánh thực tế với mục tiêu tháng:

```sql
SELECT m.cua_hang, m.muc_tieu_doanh_thu_vnd AS muc_tieu, SUM(d.doanh_thu) AS thuc_te,
       ROUND(100.0 * SUM(d.doanh_thu) / m.muc_tieu_doanh_thu_vnd, 1) AS ty_le_dat
FROM muc_tieu_thang m
LEFT JOIN doanh_thu_ngay d ON d.ma_cua_hang = m.ma_cua_hang AND strftime(d.ngay, '%Y-%m') = m.thang
WHERE m.thang = '2026-11'
GROUP BY m.cua_hang, m.muc_tieu_doanh_thu_vnd;
```
