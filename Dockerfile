# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra aws --extra postgres --extra otel --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra aws --extra postgres --extra otel

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home librarian \
    && mkdir -p /data/registry /data/secrets \
    && chown -R librarian:librarian /data \
    && chmod 700 /data/secrets
WORKDIR /app
COPY --from=build --chown=librarian:librarian /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    LIBRARIAN_HOST=0.0.0.0 \
    LIBRARIAN_PORT=8080 \
    LIBRARIAN_REGISTRY_DIR=/data/registry \
    LIBRARIAN_SECRETS_DIR=/data/secrets \
    LIBRARIAN_STATE_DIR=/data \
    LIBRARIAN_DATABASE_URL=sqlite+aiosqlite:////data/librarian.db \
    PYDANTIC_AI_NO_BANNER=1
USER librarian
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
ENTRYPOINT ["librarian"]
CMD ["serve"]
