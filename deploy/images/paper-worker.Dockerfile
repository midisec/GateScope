FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app PATH=/app/.venv/bin:$PATH \
    LIVE_TRADING_ENABLED=false GATESCOPE_EXECUTION_ENVIRONMENT=PAPER
WORKDIR /app
RUN groupadd --gid 10006 gatescope-paper \
    && useradd --uid 10006 --gid gatescope-paper --create-home --shell /usr/sbin/nologin gatescope-paper \
    && chown -R gatescope-paper:gatescope-paper /app
COPY --chown=gatescope-paper:gatescope-paper pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-install-project \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /root/.cache
COPY --chown=gatescope-paper:gatescope-paper src/gatescope ./src/gatescope
COPY --chown=gatescope-paper:gatescope-paper apps/paper_worker ./apps/paper_worker
COPY --chown=gatescope-paper:gatescope-paper apps/__init__.py ./apps/__init__.py
USER 10006:10006
CMD ["python", "-m", "apps.paper_worker.main"]
