# bizagent — bản Linux (Docker). Dùng cùng docker-compose.yml; hướng dẫn trong README.md.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1 TZ=Asia/Ho_Chi_Minh
WORKDIR /app

# Thư viện cài vào image (đổi khi build: docker compose build --build-arg EXTRAS="openai,postgres")
ARG EXTRAS="llm-all,postgres,mysql,telegram,gsheets,excel,mcp"
# MSSQL=1: cài thêm ODBC Driver 18 for SQL Server của Microsoft (nghĩa là bạn đồng ý điều khoản EULA của Microsoft)
ARG MSSQL=0

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates curl gnupg \
    && if [ "$MSSQL" = "1" ]; then \
         curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
         && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
         && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY bizagent ./bizagent
RUN if [ "$MSSQL" = "1" ]; then EX="$EXTRAS,mssql"; else EX="$EXTRAS"; fi && pip install ".[${EX}]"

COPY workspaces ./workspaces
COPY deploy/docker/entrypoint.sh /usr/local/bin/bizagent-entrypoint
RUN chmod +x /usr/local/bin/bizagent-entrypoint \
    && useradd --create-home --uid 10001 app && chown -R app /app/workspaces
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"

ENTRYPOINT ["bizagent-entrypoint"]
CMD ["bizagent", "run", "--host", "0.0.0.0", "--port", "8000"]
