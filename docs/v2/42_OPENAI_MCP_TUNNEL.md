# OpenAI MCP 隧道部署与验收

## 当前状态（2026-09-13）

已收到操作者创建的 Tunnel ID，并安装官方客户端 v0.0.14 和独立的 `gatescope-tunnel.service`。
操作者已通过隐藏输入保存运行密钥。17:55（北京时间）短时隧道连接验证通过后曾停止；
**后续收到“你启动吧”的授权，已启动并保持 active/running，已启用开机启动。
本轮修正了真实 ChatGPT OAuth 请求触发的 Harpoon `unsupported_channel` 配置问题；
OAuth 目标目录与严格控制面轮询检查通过。用户随后确认插件已添加，但连接时报 `invalid_target`；
已为此部署该隧道的精确资源别名兼容，尚待用户重试确认网页授权登录和工具调用。**
密钥不在代码、文档、命令行或诊断输出中展示。

原公网 Web、MCP 与 OAuth 均保留；不修改 issuer/resource，不修改数据库或租户权限。
本机 MCP `/mcp` 返回带 OAuth 发现地址的 401；本机 PRM 返回 200。
生产服务器至 OpenAI 443 的 TLS 可达性之前已验证，但它不等同于隧道业务可用。

## 依据与边界

- [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [ChatGPT MCP 连接测试](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- 实际下载客户端的 `help quickstart`、`help oauth`、`run --help`、`doctor --help`，以及 `sample_mcp_with_dcr`。

隧道客户端从服务器向 OpenAI 发起出站连接，main 转发至本平台现有 `https://43.108.44.74/mcp`，
再由原 nginx 转给 `127.0.0.1:17800/mcp`。main 必须与本部署所声明的 OAuth resource/PRM/AS 同源；
不能改回 loopback main 而继续声明公网 OAuth 元数据，原因见下方故障修正记录。
它不是公共插件发布入口，也不自动代理公网授权服务器；现有授权服务器仍为平台当前 HTTPS origin。
即使隧道 ready，也必须单独确认 ChatGPT 能完成 OAuth 授权和按用户访问数据。

控制面运行密钥和用户的 MCP OAuth access token 是不同凭据：

- `CONTROL_PLANE_API_KEY` 只用于隧道控制面。不得使用 Admin API key；运行主体需要 Tunnels Read + Use。
- 用户访问 MCP 继续使用用户自己的 Authorization。不得给所有连接注入 OWNER PAT，也不得关闭 MCP 鉴权。
- 仅允许由同源 OAuth 元数据注册的 Harpoon 精确 URL 目标；不启用任意 HTTP 目标、
  自动私网目标、第三方 Cloudflare 入口或演示 MCP stub。不将轮询通道限制为 main。
- 隧道管理端口仅 `127.0.0.1:17801`；不经 nginx 暴露，不新增安全组入站端口。

## 安装来源与固定版本

通过官方文档链接的 OpenAI 发布仓库解析当前版本，再固定安装实际验证过的版本；不要在服务启动时下载 latest。

- 发布版本：`v0.0.14`，客户端报告 git SHA `0f870e50a973fa820d4c409000059e181e8d242b`。
- Linux amd64 发布包：`tunnel-client-v0.0.14-linux-amd64.zip`。
- 发布包 SHA-256：`15bd17e805cad39d412199115bb9e10a978dd35258a114cdf25dd2ae6681c7d3`。
- 本地和生产副本均与发布方 HTTPS 元数据中的摘要一致；未声称进行了独立签名验证。
- 仅安装 `tunnel-client` 与 LICENSE/NOTICE；不运行发布包中可选的 cloudflared。
- 固定二进制：`/opt/gatescope-tools/tunnel-client/v0.0.14/tunnel-client`，root 所有、0555。

部署前确认所有新增目标不存在。该次新增部署的归档目录：
`data/release-backups/openai-tunnel-YWfLcyWN`，保持受限访问。

## 配置与权限

- `/usr/local/lib/gatescope-tunnel/client.yaml`：基于官方 `init --sample sample_mcp_with_dcr` 配置，
  含操作者的 Tunnel ID、固定同源 HTTPS MCP 地址和环境变量引用，不含密钥。
- `/etc/gatescope-tunnel/`：root 所有、0700。
- `/etc/gatescope-tunnel/control-plane.env`：由操作者交互创建，root 所有、0600；不纳入项目、归档或 Git。
- `/usr/local/lib/gatescope-tunnel/configure-secret.py`：root 所有、0555，仅 Python 标准库。
- `/etc/systemd/system/gatescope-tunnel.service`：独立动态 UID，不加载任何业务数据库或 OWNER 凭据。

服务限制 MemoryHigh 128M / MemoryMax 256M、CPUQuota 50%、MCP 并发 4、控制面缓冲 8；
保护系统和 home 目录，禁止提权，关闭原始 HTTP/负载日志与远程管理 UI。
这些是部署保护值，尚未经过真实隧道负载测试，后续应根据 RSS 和请求耗时调整。

部署的单元定义已通过 `systemd-analyze verify`；系统存在的 snapd RestartMode 警告不来自本单元。
执行 `daemon-reload` 不会启动隧道或重启业务服务。
当前单元已加入 nginx/API/MCP 的启动顺序依赖及 30 秒 MCP 启动等待。
客户端的 30 秒等待只覆盖连接前失败，不能解决 nginx 已启动但上游返回 502 的竞态，
因此另加 45 秒硬时限的 `ExecStartPre` 同源 readiness gate；`TimeoutStartSec=60`。
该脚本仅无凭据 GET 检查 PRM、AS、MCP 的 401 发现头，验证 resource/issuer/endpoints/S256，
不接受跳转、错误 TLS、错误元数据，不读取密钥或使用 HTTP 代理；502/429/网络错误有限重试并遵守 Retry-After。
这些启动保护应用于后续启动；更新单元、reload、enable 时没有再次中断当前已运行的隧道。

初次安装验证：63 项离线配置/凭据文件测试及 Ruff 通过。生产 SSH 的真实 TTY
隐藏输入检查使用不符合密钥格式的无效测试文本，文本未回显、输入被拒绝，
没有创建密钥文件。部署后原公网 OAuth 只读 SDK 发现链仍通过；
nginx/API/MCP 均 active，隧道明确为 inactive/disabled、MainPID=0。
未发送运行密钥或进行隧道控制面注册、未执行付费模型调用。

## 短时连接验证结果（2026-09-13 17:55）

本轮授权仅为隧道验证，不包括常驻运行或模型 API 调用。

1. 运行密钥由受限进程从 root-only 文件读取，仅在子进程环境中传给官方客户端。
   使用隔离环境，未继承业务数据库凭据、OWNER 令牌或其他 API key。
2. 实际 `doctor --json` 退出 0：配置、Tunnel ID、运行密钥引用、本机 MCP 可达性、OAuth 元数据、健康监听检查通过。
   doctor 的密钥引用检查本身不代表控制面接受了密钥；以下真实轮询才是连接证据。
3. 创建有界临时 systemd 单元 `gatescope-tunnel-validation-20260913`：RuntimeMaxSec=90s、Restart=no，
   保留独立动态 UID、256M 内存限制、本机目标、关闭原始 HTTP 日志和私网 HTTP 自动代理。
4. `/healthz` 与 `/readyz` 均返回 200。启动初期尚无完成的控制面轮询，不能仅凭这两个 200 判成功。
   随后 `tunnel-client health --port 17801 --require-control-plane-poll --json` 退出 0、result=ok，
   `control_plane_poll.ok=true`；最后成功轮询时间戳为 `1789293300`（17:55:00 北京时间）。
   当次采样显示成功轮询延迟计数 1、轮询循环数 2、队列长度 0。
5. 停止前服务 cgroup MemoryCurrent=11005952 字节，仅为短时样本，不是满载容量结论。
6. 已主动停止测试单元，MainPID=0、inactive；正式 `gatescope-tunnel` 仍 disabled/inactive。
   健康端口 17801 已关闭，nginx/API/MCP 仍全部 active。

结论：服务器到 OpenAI 隧道控制面的真实连接/轮询通过，本机 OAuth 元数据可发现。
**没有访问用户的 ChatGPT 账号，不能把它说成 ChatGPT 网页完整接入成功。**
没有发起模型 API 请求、没有下单、没有修改用户 OAuth grants 或共享注入 OWNER token。
本轮不声称隧道服务免费；服务计费仍应依据官方政策和账号账单核实。

## 常驻启动与网页 OAuth 辅助通道修正（2026-09-13）

用户授权启动后，正式服务于 18:15 启动。控制面轮询成功，但真实网页请求产生了两次
`command_unsupported_channel_total{channel="harpoon",command_type="jsonrpc"}`。
这证明请求已到隧道客户端，不能再笼统归因于公网 IP 不可达。

核对固定版本官方运行时源码后确认：

- 网页网关的兼容 OAuth 流程会经 Harpoon JSON-RPC 执行发现；只检查 main 或 health 不够。
- Harpoon 只有目标目录非空才可路由；原本 main 是 loopback HTTP，而 resource、PRM 和 AS 是公网 HTTPS。
  `oauth/hostbundle.go` 将跨 main origin 的记录标为限制注册，
  `runtimeharpoon/host_registration.go` 无法建立可信同源 OAuth 目标集合。
- 修正只把 main 改为平台已存在且证书正常的公网 HTTPS `/mcp`。不修改 issuer、resource、用户授权、
  API scope 校验或 nginx 业务配置，不共享注入 OWNER token，不开放内网自动代理。
- 下载用于核对的官方 runtime source v0.0.14 SHA-256：
  `a62aa5e129d3ef308236f671b47da06464474c272442c1bd267a3ecffec96590`。

重启隧道应用 main URL 后的实际只读检查：

1. `health --port 17801 --require-control-plane-poll --json` 退出 0；health、ready、成功轮询均通过。
2. 本机 `/api/harpoon/status`：enabled=true、capture_payloads=false、allow_plaintext_http=false。
3. `/api/harpoon/targets` 有 8 个目标，全部 source/category=oauth，全部是现有公网 HTTPS 同源精确 URL。
   资源与 PRM 来源不带组；其余 6 个目标属于同一个授权服务器组，角色计数符合官方协议。
4. `/api/oauth` 的 PRM 状态 200；resource、唯一授权服务器和原有 3 个 scope 均保持正确。
5. 64 项隧道配置/凭据测试与 Ruff 通过，生产单元验证通过；系统既有 snapd 配置警告与本单元无关。
6. 正式服务保持 active/running、enabled，采样内存约 11 MiB；这不是满载容量结论。

只读状态端口仍仅绑定 loopback。诊断只输出计数、固定状态与布尔结论，
不打印目标分组标识、完整原始日志、请求正文、令牌或控制面密钥。
没有启动调试 Harpoon HTTP transport、没有调用模型 API、没有下单。

**这是已验证的服务端配置修正，不等于用户的 ChatGPT 网页 OAuth 已验收成功。**
还需用户在同一工作区 Tunnel 入口重试：进入平台授权页后完成登录/授权，再确认工具发现。
不要把 OpenAI 内部 `tunnel-service.gateway...` 地址设为本平台 issuer/resource 或自行探测其内部接口。

回退前的无密钥配置与单元备份位于既有受限目录 `data/release-backups/openai-tunnel-YWfLcyWN/`，
文件为 `before-public-origin-client.yaml` 和 `before-public-origin-gatescope-tunnel.service`。
回退 main 到旧配置会恢复上述 OAuth 辅助通道问题，不应作为常规解决方式。

## 插件已添加后的 invalid_target 兼容修正（2026-09-13）

用户实际反馈已从“不支持 OAuth”推进到“插件添加成功，连接时报 invalid_target”。
此错误来自本平台授权边界的 resource 精确校验，不是隧道断线。
平台不记录授权 query，未取得这次浏览器实际 resource 参数；不能声称已从日志捕获该值。
兼容配置使用用户此前已提供、且 ID 与当前运行隧道一致的那一条完整隧道资源 URI。

- API 可通过单个 `GATESCOPE_MCP_OAUTH_TUNNEL_RESOURCE` 设置部署方明确批准的传输别名，默认关闭。
  示例见 `deploy/systemd/gatescope-api-tunnel-oauth.example.conf`，生产仅在 API 的独立 systemd drop-in 配置。
  不读取/复制控制面密钥，不改业务凭据环境文件。
- 别名必须严格符合固定 OpenAI 隧道 URI 结构，且授权时只接受配置中的这一个精确值。
  不动态信任其他 Tunnel ID，不信任 Host/Forwarded 或 DCR 传入的地址，不接受查询、片段、显式端口或路径变体。
- 授权、授权码兑换和 refresh 都把该别名视为同一 canonical MCP 资源的传输入口。
  pending/grant 仍只保存原公网 resource；issuer、MCP token verifier、PKCE、回调、scope 和租户绑定均不变。
  不访问 OpenAI 内部 URI，不把它当本平台新 issuer，不对外签发通用 OpenAI audience。
- 无数据库迁移。取消别名配置后该别名立即不再被接受，已有 canonical 授权不因此自动撤销；
  需要撤销授权时仍应使用平台已有的连接撤销操作。

验证结果：43 项资源别名单元测试、23 项真实隔离 PostgreSQL OAuth 集成测试通过，
覆盖错误资源不消耗有效 code/refresh、错误 PKCE/客户端/回调、租户绑定和重放撤销。
另有 77 项 HTTP/发现/部署相关测试通过，包含别名不改变公网 OAuth 元数据的回归检查。
最终合并后的相关单元测试为 213 项通过（含 29 项 readiness gate 测试）；PostgreSQL 集成测试为另计 23 项。

生产已只重启 API 应用修正，未重启市场工作进程或停止隧道。
无凭据 GET 探测使用确定不存在的客户端：canonical 与已配置别名均通过 resource 校验后以 invalid_client 拒绝，
其他 Tunnel ID、查询参数、末尾斜杠仍以 invalid_target 拒绝；没有创建 pending、授权或令牌。
公网 PRM/AS metadata 仍返回 200 且保持原 resource/issuer。

这不替代用户真实授权验收。用户可对已添加插件重新“连接”，无需删除重建；
若还报 invalid_target，需仅确认 `resource` 参数值，不能索取包含 state/code 的完整授权地址。

本次三个业务文件的无密钥回退归档：`data/release-backups/oauth-tunnel-alias-doisNI8f/before-code.tgz`。
若回退，应同时撤下本次 API 别名 drop-in 并重启 API；不要改 canonical issuer 或撤销其他用户连接。

## 操作者输入密钥

在 [OpenAI Runtime API keys](https://platform.openai.com/settings/organization/api-keys) 创建适用于该隧道组织的运行密钥。
不要创建/提供 Admin API key。ChatGPT 工作区关联和隧道 Read + Use 权限需要在账号侧正确设置。

从自己的电脑执行：

```bash
ssh -t root@43.108.44.74 'python3 /usr/local/lib/gatescope-tunnel/configure-secret.py'
```

按提示隐藏输入两次。密钥不应出现在 shell 参数、历史、聊天、截图或日志里。
已存在的密钥文件必须显式确认 OVERWRITE；脚本拒绝符号链接、不安全权限、重定向输入和含控制字符的密钥。
保存工具只保存一个 root-only 文件，不联网、不启动服务、不判断密钥实际权限。
保存后告知“已保存”即可，不要贴密钥内容。

## 后续完整接入验收（部分待执行）

1. 在受限进程中读取环境文件，仅向官方控制面传递运行密钥；执行实际客户端 doctor。
   输出只保留检查 ID/状态，不打印完整原始诊断、环境或异常文本。
2. 当前已获启动授权并启用正式常驻服务。新部署仍应在 doctor 与权限检查通过且操作者同意后，
   才 `systemctl enable --now gatescope-tunnel`；仅授权短时验证时不启用常驻服务。
3. 检查进程 active、`/healthz` 健康和 `/readyz` ready。进程 active 不是连接成功。
4. 在目标 ChatGPT 工作区选择 Tunnel，选择该 Tunnel ID，确认工具发现与初始化。
5. 真实浏览器完成原有 OWNER/用户 OAuth 流程；隧道不是绕过 MFA/用户授权的方式。
6. 用同一连接按参数查询 BTC_USDT 1m 和龙虾_USDT 15m；检查最新已收盘 K 线、指标和 Coinglass 数据质量标签。
7. 验证 OAuth 不能调用受限交易/个人信号工具；没有实盘订单行为。
8. 验证断连重连、令牌刷新和注销撤销。仅在这些实际通过后宣告接通。

如控制面无权限、workspace 未关联或 OAuth 仍失败，保留失败边界，不自动换入口、不共享注入令牌。
不要因为缺少运行密钥持续重启或用伪造密钥尝试注册。

## 停用与回退

已运行后可 `systemctl disable --now gatescope-tunnel`，仅停隧道，不停交易平台。
本次没有改动原有业务配置或 OAuth 数据，无数据库回退需求。
密钥需要在 OpenAI 账号侧撤销；删除本地文件不等于撤销账号侧凭据。
