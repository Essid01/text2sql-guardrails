# One image for the API, the UI and the seed job (different commands in docker-compose).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The [embeddings] extra (torch, ~1 GB) is left out: without it, few-shot examples are
# picked in file order instead of by similarity.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[ui]"

# Non-root user; writable directories for the LLM cache and the audit log.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/.cache /app/logs \
    && chown app:app /app/.cache /app/logs
USER app

EXPOSE 8000 8501
CMD ["uvicorn", "text2sql.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
