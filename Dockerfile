FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONVERTFILEFAST_MCP_TRANSPORT=streamable-http \
    CONVERTFILEFAST_MCP_HOST=0.0.0.0 \
    CONVERTFILEFAST_MCP_PORT=8000 \
    CONVERTFILEFAST_MCP_PATH=/mcp \
    CONVERTFILEFAST_MCP_STATELESS=true \
    CONVERTFILEFAST_REMOTE_MODE=true \
    CONVERTFILEFAST_TEMP_DIR=/data/tmp

WORKDIR /app

COPY pyproject.toml README.md server.py ./

RUN pip install --no-cache-dir .

RUN mkdir -p /data/tmp && useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["convertfilefast-mcp"]
