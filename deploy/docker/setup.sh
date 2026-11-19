#!/usr/bin/env bash
# deploy/docker/setup.sh — chuẩn bị lần đầu cho bản Linux (Docker). Chạy tại thư mục gốc dự án:
#   ./deploy/docker/setup.sh [ten_workspace]
# Tạo .env từ .env.example (sinh sẵn BIZAGENT_SECRET_KEY, BIZAGENT_API_TOKEN, UID/GID), tạo thư mục dữ liệu.
set -euo pipefail
cd "$(dirname "$0")/../.."
WS="${1:-demo}"

if [ ! -f "workspaces/$WS/workspace.yaml" ]; then
    echo "Không thấy workspaces/$WS/workspace.yaml (chép workspaces/_template thành workspaces/$WS rồi sửa)" >&2
    exit 1
fi
command -v docker >/dev/null || { echo "Chưa cài Docker: https://docs.docker.com/engine/install/" >&2; exit 1; }

secret() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; }

if [ ! -f .env ]; then
    cp .env.example .env
    sed -i "s|^BIZAGENT_SECRET_KEY=.*|BIZAGENT_SECRET_KEY=$(secret)|" .env
    sed -i "s|^BIZAGENT_API_TOKEN=.*|BIZAGENT_API_TOKEN=$(secret)|" .env
    sed -i "s|^BIZAGENT_WORKSPACE=.*|BIZAGENT_WORKSPACE=$WS|" .env
    {
        echo ""
        echo "# --- Docker (setup.sh) ---"
        echo "BIZAGENT_UID=$(id -u)"
        echo "BIZAGENT_GID=$(id -g)"
        echo "# BIZAGENT_DOMAIN=bi.congty.vn          # cho profile https (Caddy)"
        echo "# CLOUDFLARE_TUNNEL_TOKEN=               # cho profile tunnel"
        echo "# BIZAGENT_MSSQL=1                       # cài ODBC Driver 18 (SQL Server) khi build"
    } >> .env
    chmod 600 .env
    echo "Đã tạo .env. Mở .env điền API key model AI (vd ANTHROPIC_API_KEY) và token kênh chat."
else
    echo ".env đã có, giữ nguyên."
fi

mkdir -p "workspaces/$WS/data"
echo "Xong. Tiếp theo:  docker compose up -d --build   rồi   docker compose logs -f bizagent"
