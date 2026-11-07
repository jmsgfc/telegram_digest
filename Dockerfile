FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    TELEGRAM_DIGEST_RUNTIME_DIR=/app/runtime

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip pip install . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runtime \
    && chown -R appuser:appuser /app/runtime

USER appuser

EXPOSE 8765

CMD ["sh", "-c", "umask 077 && exec uvicorn telegram_digest.web:app --host 0.0.0.0 --port 8765 --workers 1"]
