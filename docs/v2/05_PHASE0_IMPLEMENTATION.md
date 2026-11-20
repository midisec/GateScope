# Phase 0 实施记录

状态：`代码与本地测试完成；容器构建验收等待具备 Docker 的 CI runner`  
实施日期：`2026-09-07`（Asia/Shanghai）

## 交付结论

Phase 0 已落地，现有形态算法和机会评分行为由黄金测试冻结，没有接入账户或交易所私有 API。服务只允许 `PAPER` 环境；`LIVE_TRADING_ENABLED=true`、`TESTNET` 或 `LIVE` 配置都会在导入/启动阶段失败，private executor 构造函数也始终抛出关闭异常。

当前仍不是实盘系统。Phase 1 不能开始部署切换，直到 CI runner 实际完成 API 镜像构建、非 root UID、镜像文件清单和 SBOM 验收。

## 任务状态

| ID | 实施结果 | 验证 |
| --- | --- | --- |
| FND-001 | 初始化 Git；对 SQLite 做在线备份，同时快照 DB/WAL/SHM；记录六个核心源码与数据库 SHA-256 | 备份通过 `integrity_check`、隔离复制恢复和逐表计数比对；产物位于忽略目录 `artifacts/baseline/<UTC>/`，改动前摘要归档于 `PHASE0_BASELINE_SHA256SUMS.txt` |
| FND-002 | `.gitignore`、`.dockerignore`、`pyproject.toml`、`uv.lock`；显式 COPY 白名单；非 root 镜像；Compose 只读根文件系统 | `uv lock --check` 通过；静态镜像边界检查通过；实际 Docker build 待 CI |
| FND-003 | pytest、coverage、mypy、ruff、JS syntax、迁移/契约/secret scan CI | 本地 36 项测试通过（若新增测试，以最新 CI 计数为准） |
| FND-004 | Gate 合成 contract fixture、26 形态目录、统计与完整扫描输出 digest | 黄金测试固定 480 根合成 K 线的完整结果 SHA-256 |
| FND-005 | 四轴枚举和值对象、合法组合校验、Phase 0 runtime fail-closed、private adapter guard | 合法/非法组合与执行器负向测试通过 |
| API-000 | OpenAPI 快照/生成器/兼容检查、事件 envelope、WS V2、MCP 空工具 manifest | JSON Schema 与生成快照一致性检查通过 |
| ARC-000 | 七个独立命令入口、API/executor 镜像边界、Kubernetes 默认拒绝网络策略 | import/image 静态边界通过；其他 workload 在 Phase 0 以退出码 78 明确关闭 |
| SEC-001 | 临时单 OWNER、Argon2id、随机 session、token hash、CSRF、SameSite、安全 Cookie、锁定、限流、安全头、append-only audit | 匿名业务 API、错误登录、CSRF、退出/撤销和审计不可改负向测试通过 |
| OBS-001 | JSON 结构化脱敏日志、服务端 trace ID、`/health/live`、`/health/ready`、依赖状态脱敏 | status 不返回 DB 路径；上游异常不返回 URL、代理或响应内容 |

另外使用 23 MiB 基线数据库的隔离副本执行了真实 upgrade：`integrity_check=ok`，Alembic revision 为 `0001_phase0_security`，原有 `3,323` 个 instruments 与 `109,135` 根 candles 均保持不变，三个认证/审计表创建成功。

## 安全边界

- Phase 0 登录只解决当前研究服务的公网裸露问题，不等同于正式多用户身份系统。
- Bootstrap 密码只可由终端交互或部署秘密管理器输入；创建 OWNER 后应从环境移除。
- Session 原值只存在客户端 HttpOnly Cookie，数据库只保存 SHA-256；CSRF 使用单独的 double-submit token 和服务端 hash。
- 登录失败对不存在账号和错误密码返回同一消息；单账号连续失败会持久锁定。
- `/health/*` 可匿名读取但只返回稳定状态；其他业务 API 和 OpenAPI 元数据默认要求登录。
- 当前 SQLite 业务表仍是单租户兼容层。正式 users/memberships、MFA、recent-auth、PostgreSQL `FORCE RLS` 属于 Phase 1。

## 已知限制和下一门禁

1. 当前执行环境没有 Docker 命令，因此本地不能诚实声称镜像构建、UID 和运行时只读挂载已经实测；这些检查已写入 CI。
2. Git 仓库是新初始化的工作区，尚无远端和首次提交；提交/推送由仓库所有者决定。
3. FastAPI 旧式 startup/shutdown hook 保持兼容，Phase 1 composition root 重构时迁到 lifespan。
4. 进程内 rate limiter 只适用于 Phase 0 单实例；Phase 1 使用共享限流存储并执行 tenant/subject 维度策略。
5. 当前形态黄金基线有意冻结旧算法，包括已知的未来枢轴可见性问题；Phase 1 必须以新版本修复，不能静默改写 V1 结果。

## 复验命令

```bash
uv sync --frozen --all-extras
uv run ruff check src/gatescope src/gatescope_executor apps tests scripts app.py database.py gate_client.py scanner.py opportunity_scanner.py pattern_engine.py
uv run mypy src/gatescope
uv run pytest --cov=src/gatescope --cov-report=term-missing
uv run python scripts/verify_contracts.py
uv run python scripts/check_generated_contracts.py
uv run python scripts/verify_boundaries.py
uv run python scripts/secret_scan.py
python -m py_compile app.py database.py gate_client.py scanner.py opportunity_scanner.py pattern_engine.py
node --check static/app.js
node --check static/login.js
```
