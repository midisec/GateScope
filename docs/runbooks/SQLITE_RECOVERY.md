# SQLite 备份与恢复

## 备份

`scripts/backup_sqlite.py` 使用 SQLite online backup API 生成一致性主备份，同时保留源 DB/WAL/SHM 的取证快照。脚本会校验：

- `PRAGMA integrity_check = ok`；
- 隔离临时目录中的恢复副本可打开；
- 核心表逐表计数与源数据库一致；
- 备份、伴随文件和六个核心源码的 SHA-256 写入 `manifest.json`。

备份目录被 `.gitignore` 和 `.dockerignore` 排除。应由部署平台将其加密复制到独立对象存储，并按保留策略管理。

## 恢复演练

1. 停止 API 和所有 worker，确认没有进程持有目标数据库。
2. 保留故障数据库、WAL 和 SHM，不要原地覆盖；把三者移动到独立故障目录。
3. 校验备份 SHA-256 与 `manifest.json` 一致。
4. 将 `gate_patterns.backup.sqlite3` 复制为新的 `data/gate_patterns.db`。
5. 对新文件执行 `PRAGMA integrity_check`，并与 manifest 逐表计数比对。
6. 运行 `alembic upgrade head`，再以 `GATESCOPE_START_WORKERS=false` 启动 API。
7. 确认登录、`/health/ready`、市场数据计数和审计后，才恢复研究 worker。

恢复过程中不要把数据库、manifest 或任何认证哈希提交到 Git，也不要在工单/聊天中粘贴内容。

