#!/bin/sh
# Chạy trước lệnh chính của container: workspace demo chưa có dữ liệu thì tạo dữ liệu mẫu + đồng bộ.
set -e
WS="${BIZAGENT_WORKSPACE:-demo}"
if [ "$WS" = "demo" ] && [ ! -f workspaces/demo/data/demo.db ]; then
    echo "[bizagent] Tạo dữ liệu mẫu cho workspace demo"
    bizagent seed-demo && bizagent sync || echo "[bizagent] Không tạo được dữ liệu mẫu (kiểm tra quyền ghi ./workspaces)"
fi
exec "$@"
