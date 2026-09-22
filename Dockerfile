# Lenovmail — authored by satuapps
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci || npm install
COPY web/ ./
RUN npm run build

FROM python:3.13-slim-trixie AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Dependency layer is kept separate so app rebuilds don't repeat resolution.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

COPY --from=web /web/dist /app/static

EXPOSE 8000
CMD ["uvicorn", "lenovmail.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
