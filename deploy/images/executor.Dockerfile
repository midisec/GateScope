# Isolated credential-validation / explicit Gate TESTNET execution image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app PATH=/app/.venv/bin:$PATH \
    LIVE_TRADING_ENABLED=false GATESCOPE_EXECUTION_ENVIRONMENT=PAPER \
    GATESCOPE_EXECUTOR_WORKLOAD=credential-validation
WORKDIR /app
RUN groupadd --gid 10001 gatescope-executor && useradd --uid 10001 --gid gatescope-executor --create-home --shell /usr/sbin/nologin gatescope-executor
COPY --chown=gatescope-executor:gatescope-executor pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-install-project \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /root/.cache
COPY --chown=gatescope-executor:gatescope-executor src/gatescope ./src/gatescope
COPY --chown=gatescope-executor:gatescope-executor src/gatescope_executor ./src/gatescope_executor
COPY --chown=gatescope-executor:gatescope-executor apps/executor ./apps/executor
COPY --chown=gatescope-executor:gatescope-executor apps/__init__.py ./apps/__init__.py
USER 10001:10001
CMD ["python", "-m", "apps.executor.main"]
