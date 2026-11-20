FROM node:22-bookworm-slim AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app \
    PATH=/app/.venv/bin:$PATH \
    HOST=0.0.0.0 \
    PORT=8777 \
    NO_BROWSER=1 \
    LIVE_TRADING_ENABLED=false \
    GATESCOPE_EXECUTION_ENVIRONMENT=PAPER

WORKDIR /app
RUN groupadd --gid 1000 gatescope \
    && useradd --uid 1000 --gid gatescope --create-home --shell /usr/sbin/nologin gatescope \
    && mkdir -p /app/data /app/static/vendor \
    && chown -R gatescope:gatescope /app

COPY --chown=gatescope:gatescope pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-install-project \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /root/.cache

# Explicit allow-list: runtime data, VCS metadata, docs, tests and secrets are not copied.
COPY --chown=gatescope:gatescope app.py database.py gate_client.py scanner.py opportunity_scanner.py pattern_engine.py ./
COPY --chown=gatescope:gatescope alembic.ini ./
COPY --chown=gatescope:gatescope migrations ./migrations
COPY --chown=gatescope:gatescope src/gatescope ./src/gatescope
COPY --chown=gatescope:gatescope apps/api ./apps/api
COPY --chown=gatescope:gatescope apps/__init__.py ./apps/__init__.py
COPY --chown=gatescope:gatescope static ./static
COPY --from=web-build --chown=gatescope:gatescope /web/dist ./web/dist

USER 1000:1000
EXPOSE 8777
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8777/health/ready', timeout=3)"
CMD ["python", "-c", "from apps.api.main import main; main()"]
