FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app PATH=/app/.venv/bin:$PATH \
    GATESCOPE_MCP_HOST=0.0.0.0 GATESCOPE_MCP_PORT=8780 \
    LIVE_TRADING_ENABLED=false GATESCOPE_EXECUTION_ENVIRONMENT=PAPER
WORKDIR /app
RUN groupadd --gid 10004 gatescope-mcp \
    && useradd --uid 10004 --gid gatescope-mcp --create-home --shell /usr/sbin/nologin gatescope-mcp \
    && chown -R gatescope-mcp:gatescope-mcp /app
COPY --chown=gatescope-mcp:gatescope-mcp pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.8.22 \
    && uv sync --frozen --no-dev --no-install-project \
    && /usr/local/bin/python -m pip uninstall -y uv \
    && rm -rf /root/.cache
COPY --chown=gatescope-mcp:gatescope-mcp src/gatescope ./src/gatescope
COPY --chown=gatescope-mcp:gatescope-mcp apps/mcp_server ./apps/mcp_server
COPY --chown=gatescope-mcp:gatescope-mcp apps/__init__.py ./apps/__init__.py
USER 10004:10004
EXPOSE 8780
CMD ["python", "-m", "apps.mcp_server.main"]
