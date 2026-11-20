# Phase 0 研究服务部署

## 首次部署

1. 在停机窗口或运行中执行一致性备份：

   ```bash
   python scripts/backup_sqlite.py
   ```

2. 安装锁定依赖并运行验收：

   ```bash
   uv sync --frozen --all-extras
   uv run pytest
   uv run python scripts/secret_scan.py
   ```

3. 本地交互创建唯一 OWNER：

   ```bash
   uv run gatescope-admin create-owner --username owner
   ```

   无人值守平台改由秘密管理器在首次启动注入 `GATESCOPE_BOOTSTRAP_USERNAME` 和 `GATESCOPE_BOOTSTRAP_PASSWORD`。成功后移除两者并重启，已有 OWNER 不依赖 bootstrap 环境变量。

4. 设置运行边界：

   ```text
   LIVE_TRADING_ENABLED=false
   GATESCOPE_EXECUTION_ENVIRONMENT=PAPER
   GATESCOPE_PUBLIC_ORIGIN=https://实际访问域名
   GATESCOPE_COOKIE_SECURE=true
   ```

5. 启动后检查：

   - `GET /health/live` 返回 `200`；
   - `GET /health/ready` 返回 `200` 且 trading 显示 `PAPER/false`；
   - 未登录 `GET /api/status` 返回 `401`；
   - 登录后状态不含数据库路径、代理地址或上游响应；
   - 缺少 CSRF 的写请求返回 `403`。

## 本地 Docker Compose

首次启动可临时从秘密存储导出 bootstrap 变量，然后执行 `docker compose up -d --build`。端口固定发布到 `127.0.0.1:8777`。若要从其他机器访问，应使用 TLS 反向代理/VPN，不要把 Compose 端口改成全网卡裸露。

## 失败处理

- `/health/live=200`、`/health/ready=503 authentication:not_initialized`：创建 OWNER 或检查首次启动秘密注入。
- 配置 `LIVE_TRADING_ENABLED=true` 或非 PAPER 后进程退出：这是预期安全门禁，不得绕过。
- 数据库 unavailable：停止服务并按 SQLite 恢复手册恢复；不要删除 WAL/SHM 猜测修复。
- OWNER 锁定：Phase 0 没有不安全的 Web 自助重置。确认审计和访问来源后，在维护窗口使用受控管理流程替换账号；正式恢复 challenge 在 Phase 1 实现。

