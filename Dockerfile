# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin trading
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY configs ./configs
# The git commit this image was built from; reported by /v1/system/status as release identity.
ARG TRADING_RELEASE_REVISION=unknown
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TRADING_PROJECT_ROOT=/app \
    TRADING_CONFIG_DIR=/app/configs \
    TRADING_RELEASE_REVISION=${TRADING_RELEASE_REVISION}
USER 10001
ENTRYPOINT ["trading"]
CMD ["--help"]
