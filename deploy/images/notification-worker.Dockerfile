FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app PATH=/app/.venv/bin:$PATH \
    LIVE_TRADING_ENABLED=false GATESCOPE_EXECUTION_ENVIRONMENT=PAPER
WORKDIR /app
RUN groupadd --gid 10003 gatescope-notification \
    && useradd --uid 10003 --gid gatescope-notification --create-home --shell /usr/sbin/nologin gatescope-notification \
    && chown -R gatescope-notification:gatescope-notification /app
COPY --chown=gatescope-notification:gatescope-notification pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-install-project \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /root/.cache
COPY --chown=gatescope-notification:gatescope-notification src/gatescope ./src/gatescope
COPY --chown=gatescope-notification:gatescope-notification apps/notification_worker ./apps/notification_worker
COPY --chown=gatescope-notification:gatescope-notification apps/__init__.py ./apps/__init__.py
USER 10003:10003
CMD ["python", "-m", "apps.notification_worker.main"]
