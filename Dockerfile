FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app app
COPY config config
RUN useradd -u 10001 -m gateway
USER gateway
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
