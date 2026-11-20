FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app PATH=/app/.venv/bin:$PATH \
    LIVE_TRADING_ENABLED=false GATESCOPE_EXECUTION_ENVIRONMENT=PAPER
WORKDIR /app
RUN groupadd --gid 10002 gatescope-worker \
    && useradd --uid 10002 --gid gatescope-worker --create-home --shell /usr/sbin/nologin gatescope-worker \
    && mkdir -p /app/data \
    && chown -R gatescope-worker:gatescope-worker /app
COPY --chown=gatescope-worker:gatescope-worker pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-install-project \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /root/.cache
COPY --chown=gatescope-worker:gatescope-worker database.py gate_client.py scanner.py opportunity_scanner.py pattern_engine.py alembic.ini ./
COPY --chown=gatescope-worker:gatescope-worker migrations ./migrations
COPY --chown=gatescope-worker:gatescope-worker src/gatescope ./src/gatescope
COPY --chown=gatescope-worker:gatescope-worker apps/market_worker ./apps/market_worker
COPY --chown=gatescope-worker:gatescope-worker apps/strategy_worker ./apps/strategy_worker
COPY --chown=gatescope-worker:gatescope-worker apps/research_worker ./apps/research_worker
COPY --chown=gatescope-worker:gatescope-worker apps/__init__.py ./apps/__init__.py
USER 10002:10002
CMD ["python", "-m", "apps.research_worker.main"]
