# AIP 常驻部署

2026-10-26 部署到 `erebos.aip.de`，账户 `zywu`，目录 `/z/zywu/remote-commander`。使用现有 AIP 服务器，没有新建收费虚拟机。ChatGPT 的原私有插件、MCP URL 和 OAuth 入口均保持不变。

```text
ChatGPT 私有插件 → Sites 加密队列 → AIP 桥接 → AIP 本地 SSH 隧道 → comput1 agent
                                        ├→ AIP 本地 agent
                                        └→ Mac 反向隧道（可选，Mac 在线时）
```

## 服务与凭据

AIP 使用系统 Python 3.11 的独立 `.venv`。服务均使用当前账户权限，不需要 sudo，也不依赖用户级 systemd 的登录状态。

| 组件 | 监听/连接 | 状态与日志目录 |
| --- | --- | --- |
| AIP agent | `127.0.0.1:8765` | `.runtime/service/` |
| 出站桥接 | HTTPS 到原私有 Site | `.runtime/bridge-service/` |
| comput1 隧道 | AIP `127.0.0.1:18765` → comput1 `127.0.0.1:8765` | `.runtime/tunnel-service/` |

每个组件均由单实例 supervisor 管理，退出后约五秒重新启动；SSH 建连还需要额外网络时间。当前用户 crontab 中的独立 `SERVICE`、`BRIDGE`、`TUNNEL` 标记区块各含 `@reboot` 和每五分钟的 supervisor 恢复任务。crond 已验证运行。安装器保存旧 crontab，不覆盖其他任务。

配置和密钥在 `.runtime/`，该目录权限为 700，密钥和 env 文件为 600，不进入 Git。桥接 env 包含 Sites 服务凭据和每台 agent 的独立 token。SSH 使用已核验的服务器公钥，禁止交互式密码重试。

AIP 到 comput1 使用在 AIP 本机生成的专用 SSH 密钥，私钥没有从 AIP 导出。comput1 中的公钥条目禁止执行命令、PTY 和 agent 转发，并将本地端口转发目标限制为 `127.0.0.1:8765`。远程工具本身仍需 agent Bearer token。

## 安装/恢复命令

以下命令从 AIP 项目目录运行，引用已经部署的私有配置；运行前不要打印 env 内容。

```sh
.venv/bin/python scripts/install_linux_service.py --kind agent \
  --config .runtime/agent.json --env-file .runtime/agent.env
.venv/bin/python scripts/install_linux_service.py --kind tunnel \
  --ssh-config .runtime/ssh_config --ssh-host comput1
.venv/bin/python scripts/install_linux_service.py --kind bridge \
  --site https://remote-commander-wuzy.alight-elm-8729.chatgpt.site \
  --gateway-config .runtime/devices.json --env-file .runtime/bridge.env
```

## Mac 可选连接

原 Mac 云桥接、HTTP gateway 和 comput1 隧道已退出并移出 LaunchAgents，备份在 Mac 项目的 `.runtime/aip/disabled-*.plist`。不要同时恢复旧云桥接。

Mac agent 仍保留。新的 `local.remote-commander.reverse.aip` 用户服务将 AIP 的 `127.0.0.1:28765` 反向连接到 Mac agent，仅用于访问 Mac。其专用公钥在 AIP 上只允许指定回环监听端口，不能执行命令。

独立性验收时，这条反向连接也已停止，因此设备列表显示 `mac.online=false`。下次 Mac 开机并登录时，保留在 LaunchAgents 的 plist 会自动加载。若要在当前登录会话立即恢复 Mac 设备，可在 Mac 运行：

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/local.remote-commander.reverse.aip.plist"
```

这只恢复 Mac 设备，不是使用 comput1 的前提。

## 验收记录

- AIP 使用正式 Sites 服务凭据访问 `/relay/poll` 成功，HTTP 200。
- Mac 云桥接、gateway、旧 comput1 隧道和新的 Mac 反向隧道均停止后，ChatGPT 私有插件列出 AIP 与 comput1 在线、Mac 离线。
- 通过该插件在 comput1 实际执行命令，并使用文件工具写入、读取测试内容。
- 单独终止 AIP 的桥接和 SSH 隧道子进程，supervisor 生成新进程；待隧道建连后，ChatGPT 再次成功连接 comput1。
- AIP 本地 `.runtime/cutover-recovery.json` 保存恢复后的状态；秘密不写入报告。
- 没有重启共享科研服务器；开机路径通过已安装 crontab 和运行中的 crond 验证，进程故障恢复通过实际终止/恢复验证。

使用条件为 **AIP 和 comput1 在线**。Mac、手机 Termius 以及最初用于部署的 SSH 会话均不是桥接工作所需的中间环节。
