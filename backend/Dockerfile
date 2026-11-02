FROM python:3.12-slim

ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# 先建好目录并改属主，Docker 新建命名卷时会沿用这个属主
RUN useradd --create-home desk && mkdir -p /data/attachments && chown -R desk /app /data
USER desk

CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
