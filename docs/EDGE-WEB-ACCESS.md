# V2 公网网页登录（更新：2026-11-01）

## 用户行为

- 公网入口：`https://copyto.me:8061/`。未登录时进入 `/access`，显示网页密码输入框，不再发送 `WWW-Authenticate: Basic`。
- 输入原来的 V2 访问密码即可，不再要求输入用户名。密码由服务器 `/etc/nginx/.htpasswd-codex-v2` 的单向哈希校验。2026-11-01 已按用户要求轮换 V2 密码；不在源码或文档记录明文，不修改 V1 密码。
- 成功后浏览器保留 30 天登录。Cookie 为 Secure、HttpOnly、SameSite=Lax、Host-only；服务器只保存随机登录令牌的 SHA-256 摘要。
- `https://copyto.me:8061/logout` 是退出确认页，提交后立即撤销此会话令牌。修改原密码文件也会使此前登录全部失效。
- 错误密码在网页内提示。带 CSRF 的登录和退出表单、错误次数限制、防缓存和防嵌入响应头已启用。

## 2026-11-01 登录 403 修复

当时 V2 本地 8055、frpc 与公网登录服务均正常，浏览器提交 `/access` 却收到 403。实际浏览器对照测试确认：登录页使用 `Referrer-Policy: no-referrer` 时，原生表单 POST 会携带 `Origin: null`，与严格同源检查冲突。

修复为 `Referrer-Policy: same-origin`：同源表单携带真实 Origin，而跨站导航不发送 Referrer。仍拒绝 `Origin: null`、外站 Origin 和伪造/缺失 CSRF，不能通过取消来源检查来“修复”。此外，POST 提交一律关闭响应连接，防止提前拒绝后剩余表单正文被解析成下一条 HTTP 请求。

- `deploy/edge-auth/rotate_password.py`：管理员通过 stdin JSON 传入新密码，生成随机盐 SHA-512 哈希，原子替换密码文件并保留 owner/mode。原文件以 0600 备份在服务器 `/var/backups/codex-v2-edge/password-20261101-044328-089573/htpasswd`。不将密码放入 argv 或输出。
- 登录服务修复前源码备份：`/var/backups/codex-v2-edge/form-origin-20261101/auth_server.py`。
- `tests/public_access_smoke.py`：通过 stdin 读取测试密码，验证真实 HTTPS 登录、工作台、JS、WebSocket 和会话列表，最后撤销本次测试登录。只读测试，不发送 Agent 消息。
- `tests/form_origin_fixture.py`：本地回环监听、合成密码与临时数据库，用于浏览器复现旧策略 Origin:null→403 和新策略真实Origin→登录成功；禁止部署到公网。

## 当前链路和启动

```text
浏览器 HTTPS 8061
    → Nginx form login / auth_request
    → 127.0.0.1:18062（服务器上的登录校验进程）
    → 127.0.0.1:18061（frps，仅本机入口可达，现有 nftables 规则保留）
    → frpc
    → 本机 V2 HTTP 8055
```

本机 8050 当前属于 Strata，不能把 V2 隧道指向它。已经将 `C:\frp\frpc-win11.toml` 中 **仅 V2 条目**的 `localPort` 改为 8055，V1 的 8060 条目及认证配置不变。

以后启动公网 V2：

```powershell
Set-Location -LiteralPath "D:\Newproj\202608\CodexHTML\subprojects\codex-session-harness-v2"
.\start-public.ps1
# 等价：.\start.ps1 -Port 8055
```

仍需 frpc 进程运行；公网登录校验进程由服务器 systemd 自动启动，不依赖 V1 业务服务。直接访问本机/局域网的 8055 不经过公网校验层，仍只适合受信任设备。

## 服务器文件

- `/opt/codex-v2-auth/auth_server.py`：Python 3.11 标准库服务，仅监听 `127.0.0.1:18062`。
- `/etc/systemd/system/codex-v2-auth.service`：使用 www-data 低权限账户，启用 systemd 文件/网络限制。
- `/etc/nginx/sites-available/condexmobile-v2`：Nginx 配置，所有 V2 路径统一经过 `auth_request`；登录/退出页面是唯一公开例外。
- `/var/lib/codex-v2-auth/`：会话数据库与 CSRF 签名密钥，权限 0700/0600；需要保留以维持重启后的登录。
- `/etc/nginx/.htpasswd-codex-v2`：原密码哈希文件，root:www-data 0640。不要将明文密码写入源码、日志或此文档。

Nginx 不把用户的 Authorization 头传给 Agent；登录验证服务也不代理或接收会话消息。未登录的页面导航跳转到 `/access`，API、上传、下载、WebSocket 返回 401 且不带 Basic challenge。验证服务失效时入口 5xx，绝不放行。

## 部署与回滚

仓库内源码位于 `deploy/edge-auth/`。部署前先在 Linux Python 3.11 运行 `tests/test_edge_auth.py`，再运行打包暂存目录根部的 `install_edge.py`。安装器先启动并自测校验服务，最后才修改 Nginx；`nginx -t` 或 reload 失败则恢复原配置。

本次配置备份：

- 服务器 `/var/backups/codex-v2-edge/20261030-230430/nginx.conf`
- 本机 `C:\frp\frpc-win11.toml.before-v2-form-20261030-230519.bak`

需要回滚登录方式时，由管理员将备份 Nginx 文件恢复到原位置，执行 `nginx -t` 通过后 reload；这会恢复原 Basic Auth。无需删除密码文件、会话历史、V1 文件或整个 state 目录。

## 验证

- Windows：`python -m unittest discover -s tests`，平台不支持的 crypt 用例跳过。
- Linux：`python3 -m unittest discover -s tests -p test_edge_auth.py`，包含真实 SHA-512/libcrypt 的原算法兼容测试。
- `deploy/edge-auth/smoke_nginx.py`：部署机上启动独立 loopback Nginx 和合成凭据进行完整登录、保护路由、WebSocket 401→101、退出撤销验证。不改生产密码文件、不向 Agent 发送消息。
- 公网生产验证包含页面显示、未登录拦截、错误密码提示；用户显式授权提供测试密码时，可用 stdin 完整验证登录后只读连接，随后撤销测试令牌。不读取浏览器已有 Cookie，也不打印口令和令牌。

## 维护约束

服务器当前为 Debian Python 3.11，复用系统 libcrypt 验证现有 SHA-512 哈希。Python 3.13 起移除了标准库 crypt；升级前需显式迁移验证库并重跑 Linux 测试，不允许在不兼容时降级成放行或默认密码。
