FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Extras cài vào image: đổi khi build, vd --build-arg EXTRAS="anthropic,openai,postgres,mysql,telegram"
# SQL Server (mssql) cần thêm ODBC Driver 18 của Microsoft: xem docs của Microsoft cho Debian.
ARG EXTRAS="llm-all,postgres,mysql,telegram,gsheets,excel"

COPY pyproject.toml README.md ./
COPY bizagent ./bizagent
RUN pip install ".[${EXTRAS}]"

COPY workspaces ./workspaces
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/workspaces/demo/data /app/workspaces/demo/files && chown -R app /app/workspaces
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

# Workspace demo: tạo database mẫu nếu chưa có, rồi chạy API
CMD ["sh", "-c", "if [ \"$BIZAGENT_WORKSPACE\" = \"demo\" ] && [ ! -f workspaces/demo/data/demo.db ]; then bizagent seed-demo && bizagent sync; fi; exec bizagent serve --host 0.0.0.0 --port 8000"]
