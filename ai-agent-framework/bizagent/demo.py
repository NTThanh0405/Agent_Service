"""Tạo database SQLite mẫu cho workspace demo (dữ liệu giả, sinh cố định theo seed).

Bảng: stores, products, customers, orders, order_items. Ngày đơn hàng: 180 ngày gần nhất tính tới hôm qua.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

STORES = [
    (1, "Chi nhánh Quận 1", "Hồ Chí Minh"),
    (2, "Chi nhánh Cầu Giấy", "Hà Nội"),
    (3, "Chi nhánh Hải Châu", "Đà Nẵng"),
]
PRODUCTS = [
    (1, "Cà phê sữa đá", "Cà phê", 35000), (2, "Bạc xỉu", "Cà phê", 39000), (3, "Americano", "Cà phê", 45000),
    (4, "Trà đào cam sả", "Trà", 49000), (5, "Trà sữa ô long", "Trà", 52000), (6, "Matcha latte", "Trà", 59000),
    (7, "Bánh croissant", "Bánh", 32000), (8, "Bánh tiramisu", "Bánh", 45000), (9, "Sinh tố bơ", "Sinh tố", 55000),
    (10, "Nước ép cam", "Sinh tố", 45000),
]
CHANNELS = [("tai_quan", 0.55), ("grabfood", 0.2), ("shopeefood", 0.15), ("website", 0.1)]

SCHEMA = """
CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price INTEGER NOT NULL);
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, created_at TEXT NOT NULL);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES stores(id), customer_id INTEGER REFERENCES customers(id),
  channel TEXT NOT NULL, status TEXT NOT NULL, order_date TEXT NOT NULL, created_at TEXT NOT NULL,
  total_amount INTEGER NOT NULL
);
CREATE TABLE order_items (
  order_id INTEGER NOT NULL REFERENCES orders(id), product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL, unit_price INTEGER NOT NULL
);
CREATE INDEX ix_orders_date ON orders(order_date);
"""


def create_demo_db(path: Path, days: int = 180, seed: int = 42, today: date | None = None) -> Path:
    rnd = random.Random(seed)
    today = today or date.today()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO stores VALUES (?,?,?)", STORES)
        con.executemany("INSERT INTO products VALUES (?,?,?,?)", PRODUCTS)

        customers = []
        start = today - timedelta(days=days)
        for cid in range(1, 801):
            created = start + timedelta(days=rnd.randrange(days))
            customers.append((cid, f"Khách {cid}", f"09{rnd.randrange(10**8):08d}", f"khach{cid}@example.com",
                              created.isoformat()))
        con.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

        order_id = 0
        orders, items = [], []
        for d in range(days):
            day = start + timedelta(days=d)
            weekend = day.weekday() >= 5
            for store_id, *_ in STORES:
                n = rnd.randint(25, 45) + (12 if weekend else 0) + store_id * 3
                for _ in range(n):
                    order_id += 1
                    channel = rnd.choices([c for c, _ in CHANNELS], [w for _, w in CHANNELS])[0]
                    status = "cancelled" if rnd.random() < 0.04 else "completed"
                    cust = rnd.randrange(1, 801) if rnd.random() < 0.6 else None
                    hour = rnd.choices(range(7, 22), [3, 6, 8, 6, 5, 6, 5, 4, 4, 5, 6, 6, 4, 3, 2])[0]
                    created = datetime(day.year, day.month, day.day, hour, rnd.randrange(60))
                    total = 0
                    for pid in rnd.sample(range(1, len(PRODUCTS) + 1), rnd.choices([1, 2, 3], [6, 3, 1])[0]):
                        qty = rnd.choices([1, 2, 3], [7, 2, 1])[0]
                        price = PRODUCTS[pid - 1][3]
                        items.append((order_id, pid, qty, price))
                        total += qty * price
                    orders.append((order_id, store_id, cust, channel, status, day.isoformat(),
                                   created.isoformat(sep=" "), total))
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", orders)
        con.executemany("INSERT INTO order_items VALUES (?,?,?,?)", items)
        con.commit()
    finally:
        con.close()
    return path


def write_targets_csv(path: Path, today: date | None = None, months_back: int = 6, months_ahead: int = 2) -> Path:
    """File kế hoạch giả (như sheet kế hoạch doanh nghiệp tự nhập): mục tiêu doanh thu tháng theo cửa hàng."""
    today = today or date.today()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["Tháng,Mã cửa hàng,Cửa hàng,Mục tiêu doanh thu (VNĐ),Mục tiêu số đơn"]
    base = {1: 165_000_000, 2: 150_000_000, 3: 135_000_000}
    for k in range(-months_back, months_ahead + 1):
        idx = today.year * 12 + today.month - 1 + k
        y, m = idx // 12, idx % 12 + 1
        for sid, name, _city in STORES:
            lines.append(f"{y}-{m:02d},{sid},{name},{base[sid]},{base[sid] // 85_000}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
