# Remote Commander

自托管、多设备的 Desktop Commander 核心功能替代。文件读写、精确编辑、后台搜索、命令执行、进程输入、增量输出和终止均通过标准 MCP 提供。没有第三方中转账号或按调用计费服务。

## 结构

```text
MCP 客户端 ── stdio/HTTP ── gateway ── MCP HTTP + 每台独立 token ── agent
                                                               ├─ Windows
                                                               ├─ macOS
                                                               └─ Linux
```

每台机器运行一个 agent；gateway 用 `deviceId` 路由。也可以直接连接单个 agent。所有网络传输和协议协商使用官方 Python MCP SDK；当前锁定支持维护中的 SDK 1.x，避免自动升级到接口不同的 2.x。

## 安装

需要 Python 3.11+。在每台 agent 和 gateway 机器上安装：

```sh
git clone git@github.com:WuZiyong/remote-commander.git
cd remote-commander
python3 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[dev]'
```

Windows 对应 `.venv\Scripts\python.exe` 和 `.venv\Scripts\remote-commander.exe`。运行命令无需激活环境。

## 启动一台远程机器

创建私有配置 `config.local.json`，roots 必须是已经存在的目录：

```json
{
  "name": "comput1",
  "roots": ["/home/your-user/work"],
  "allow_shell": true,
  "read_only": false
}
```

生成独立密钥并启动（勿把密钥提交到 Git）：

```sh
umask 077
export RC_TOKEN="$(.venv/bin/remote-commander token)"
.venv/bin/remote-commander agent --config config.local.json
```

默认只监听 `127.0.0.1:8765`，MCP 地址为 `http://127.0.0.1:8765/mcp`。HTTP 启动强制要求至少 32 字符的 token；每次请求均检查 Bearer header。建议通过 SSH 隧道连接，token 仍要保留。

在客户端机器上建立隧道：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18765:127.0.0.1:8765 your-user@remote-host
```

第二台机器使用另一个本地端口，例如 28765。已有 SSH 跳板可使用 `-J`。如果使用 Tailscale，可将 agent 绑定到实际 VPN IP，并通过 `--allowed-host '实际VPN-IP:8765'` 明确允许 Host；不需要公网开放端口。

## 多机器入口

复制 `examples/devices.json` 为 `devices.local.json`，将 URL 指向相应 SSH 隧道端口，设置每台机器的 `RC_COMPUT1_TOKEN`、`RC_MAC_TOKEN`。所有密钥环境变量请以 `RC_` 开头。

```sh
.venv/bin/remote-commander gateway --config devices.local.json
```

gateway 默认走 stdio。客户端通用配置如下；替换为真实绝对路径，并通过客户端的安全环境变量机制注入 token：

也可以将密钥存入权限为 600 的私有 `.env` 文件，用 `--env-file /绝对路径/gateway.env` 加载。文件仅允许 `RC_` 前缀的变量。这样客户端配置无需包含密钥本身。

```json
{
  "mcpServers": {
    "remote-commander": {
      "command": "/absolute/path/remote-commander/.venv/bin/remote-commander",
      "args": ["gateway", "--config", "/absolute/path/devices.local.json"],
      "env": {
        "RC_COMPUT1_TOKEN": "set-your-comput1-token",
        "RC_MAC_TOKEN": "set-your-mac-token"
      }
    }
  }
}
```

也可在单台机器以 `agent --config ... --transport stdio` 运行，无需 HTTP token。

支持自定义 HTTP headers 的 MCP 客户端可直接连接 agent 的 `/mcp`，发送 `Authorization: Bearer <token>`。gateway 也可用 `--transport http --port 8875` 提供统一 HTTP 入口（另设 `RC_TOKEN`）。这些私有 agent 端点使用静态 Bearer 鉴权，浏览器 Origin 请求被拒绝。

### 在 ChatGPT 中使用

ChatGPT 入口采用 **owner-private Sites MCP 插件**，OAuth、资源发现与登录由 Sites 平台管理，不另建密码系统。源码在 `cloud/`；已部署入口和插件信息见交付说明。通过 Sites 发布会生成对应私有插件，可以在 ChatGPT 中启用它来选择设备和调用工具。

comput1 的常驻桥接只通过出站 HTTPS 连接私有入口；Mac 通过独立的反向 SSH 隧道作为另一台设备接入。comput1 的使用不依赖 Mac 在线。云端队列中的请求和结果使用 AES-256-GCM 加密，五分钟后过期；桥接失联的已领取操作不会自动重新执行。慢操作通过新增的 `get_operation_result` 获取，避免重复写文件或启动命令。

不要扩大 Site 的共享范围；Site 访问权同时决定谁可以调用远程工具。

## 工具

| 功能 | 工具 |
| --- | --- |
| 设备和权限 | `list_devices`、`who_am_i`、`get_config` |
| 文件 | `list_directory`、`get_file_info`、`read_file`、`write_file`、`edit_block`、`create_directory`、`move_file` |
| 命令和进程 | `start_process`、`read_process_output`、`interact_with_process`、`force_terminate`、`list_sessions`、`list_processes` |
| 搜索 | `start_search`、`get_more_search_results`、`stop_search` |

先 `list_devices`，再在其他工具里指定 `deviceId`。多设备配置不允许省略 `deviceId`；单设备可省略。

`start_process` 返回 `session_id`、初始输出和 `cursor`。之后每次读取传回上次 cursor，直至 `state=finished`。cursor 是绝对字节偏移，允许重放；输出超出上限会丢弃最旧部分并返回 `truncated=true`。初次等待和轮询等待最多 10 秒，不限制命令运行时长。stdout/stderr 合并。输入是管道，无 PTY；交互程序须使用无缓冲模式（如 Python `-u`）。

文件文本以 UTF-8 处理，`offset` 是从零开始的行偏移。二进制使用 `encoding=base64`，整文件受大小上限约束。写入默认 `mode=create`，覆盖须指定 `overwrite`；追加须指定 `append` 且文件已存在。`edit_block` 在匹配数量不符时不修改文件。移动只支持同一文件系统内的文件，目标已存在时拒绝。

搜索支持字面量内容和文件名 glob，不执行正则表达式。每个文件最多检查 1 MiB、最多 20000 个文件，跳过 `.git`、`.venv`、`node_modules`；读取失败、二进制或超限文件计入 `skipped`。结果限制 5000 条、每条文本 1000 字符。请检查 `complete`、`truncated` 和 `skipped`。

## 权限与运行边界

- 文件工具解析真实路径，限制在 roots 内，拒绝符号链接越界及 Windows NTFS alternate streams。roots 目录应只允许可信 OS 用户修改；路径检查不是对抗并发恶意目录替换的 OS 沙箱。
- `allow_shell` 默认关闭；`read_only=true` 同时禁止文件修改和命令执行。启用 shell 后，命令具有整个 OS 账户权限，roots 只限制文件工具及初始 cwd。需要更强隔离时用独立账户/容器。
- shell 子进程不会继承 `RC_` 前缀的环境变量。运行账户本身仍可读取其可访问的配置文件。
- 仅能终止本服务创建的 session。没有任意 PID kill、递归删除或开放式 HTTP 文件代理。
- HTTP 校验 Host、拒绝浏览器 Origin、限制请求体、限制并发。诊断和调用审计仅写 stderr，审计不记录命令、文件正文或 token。
- 进程/搜索会话保存在服务内存中，跨 MCP 请求有效；服务重启后丢失，正常关闭会清理受管任务。需要独立于服务存活的科学作业请提交调度系统。
- 文件、输出、会话数均有限制。服务面向一个受信任所有者，共用 token 的客户端会共享会话；不是多租户权限系统。

## 持续运行

Linux 可将 `deploy/remote-commander.service` 复制到 `~/.config/systemd/user/`，按模板的路径创建 agent.json 与只读给自己的 agent.env（内容 `RC_TOKEN=...`），然后：

```sh
systemctl --user daemon-reload
systemctl --user enable --now remote-commander
journalctl --user -u remote-commander -f
```

用户级 systemd 不可用时，可安装当前用户的开机和故障恢复任务，不需要 sudo：

```sh
.venv/bin/python scripts/install_linux_service.py --config .runtime/agent.json --env-file .runtime/agent.env
```

安装器保留已有 crontab 并保存备份，仅管理自己的标记区块。`@reboot` 启动单实例 supervisor；agent 退出后约五秒恢复，cron 每五分钟检查 supervisor。文件锁避免重复启动，日志在 `.runtime/service/` 中轮转。

同一安装器使用 `--kind bridge --site https://你的私有Site地址 --gateway-config .runtime/cloud-devices.json --env-file .runtime/cloud-bridge.env` 安装独立桥接服务，状态和日志在 `.runtime/bridge-service/`，不影响 agent 的区块。

macOS 可用 `scripts/install_launchd.py` 安装用户级服务。Windows 可在任务计划程序中用当前用户运行 `.venv\Scripts\remote-commander.exe agent --config 绝对路径`，配置环境变量并隐藏窗口。

macOS 安装示例：`.venv/bin/python scripts/install_launchd.py --config /绝对路径/agent.json --env-file /绝对路径/agent.env`；统一 HTTP 网关增加 `--mode gateway --port 8875`。安装器拒绝覆盖现有同名服务。

公网部署需要自己的域名、TLS 和网络访问控制；`deploy/Caddyfile` 是可选反向代理模板。没有自动申请外部主机或改变既有远程访问设置。

## 验证

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

测试用官方 MCP 客户端验证真实 HTTP/stdio 协议、gateway 路由、鉴权、路径边界、文件编辑、进程轮询/输入/终止和搜索。`scripts/smoke.py --url URL --token-env RC_TOKEN` 可对已启动服务进行只读检查；加 `--exercise` 在允许根目录内创建独立临时目录，验证文件及命令操作后清理。

与 Desktop Commander 不同的接口：不模拟提示符智能识别、PDF 编辑、任意系统配置修改或原服务计费/账号接口。以上 19 个工具覆盖本项目确认的远程文件、命令及进程管理范围。
