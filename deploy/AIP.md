# AIP 常驻部署

2026-10-26 部署到 `erebos.aip.de`，账户 `zywu`，目录 `/z/zywu/remote-commander`。使用现有 AIP 服务器，没有新建收费虚拟机。ChatGPT 的原私有插件、MCP URL 和 OAuth 入口均保持不变。

```text
ChatGPT 私有插件 → Sites 加密队列 → AIP 桥接与动态注册表
                                      ├→ AIP 本地 agent
                                      ├→ comput1 主动连接 → comput1 agent
                                      ├→ Mac 主动连接 → Mac agent
                                      └→ Windows 主动连接 → Windows agent
```

## 服务与凭据

AIP 使用系统 Python 3.11 的独立 `.venv`。服务均使用当前账户权限，不需要 sudo，也不依赖用户级 systemd 的登录状态。

| 组件 | 监听/连接 | 状态与日志目录 |
| --- | --- | --- |
| AIP agent | `127.0.0.1:8765` | `.runtime/service/` |
| 出站桥接 | HTTPS 到原私有 Site | `.runtime/bridge-service/` |
| 动态注册表 | `.runtime/registry/devices.sqlite` | 包含配对、设备身份、心跳及路由 |
| 各设备连接器 | AIP 分配 `127.0.0.1:30000..30999` → 设备 agent | 各设备 `.runtime/device/` |

AIP agent 和桥接由独立 supervisor 管理，退出后约五秒重新启动；`SERVICE`、`BRIDGE` 的 cron 区块各含 `@reboot` 和每五分钟恢复任务。设备端连接器主动建立隧道，独立自启、发送心跳并重连，详见 [接入说明](ENROLLMENT.md)。旧 `TUNNEL` 服务及其 cron 区块已停用并备份。crond 已验证运行，原有科研 cron 任务保留。

配置和密钥在 `.runtime/`，该目录权限为 700，密钥和 env 文件为 600，不进入 Git。桥接 env 包含 Sites 服务凭据和每台 agent 的独立 token。SSH 使用已核验的服务器公钥，禁止交互式密码重试。

每个设备使用本机生成的专用 SSH 密钥，私钥不会上传到 AIP。AIP 公钥条目限定该设备的心跳命令及分配的回环 TCP 监听端口；远程工具另有独立 agent token。Mac、comput1、Windows 均已迁入同一账户的动态注册表。`devices.json` 仅静态保留 AIP 本机，并设置 `registry_path`；新增或撤销设备不再改配置。

## 安装/恢复命令

以下命令从 AIP 项目目录运行，引用已经部署的私有配置；运行前不要打印 env 内容。

```sh
.venv/bin/python scripts/install_linux_service.py --kind agent \
  --config .runtime/agent.json --env-file .runtime/agent.env
.venv/bin/python scripts/install_linux_service.py --kind bridge \
  --site https://YOUR-PRIVATE-SITE.example \
  --gateway-config .runtime/devices.json --env-file .runtime/bridge.env
```

## Mac 可选连接

原 Mac 云桥接、HTTP gateway 和 comput1 隧道已退出并移出 LaunchAgents，备份在 Mac 项目的 `.runtime/aip/disabled-*.plist`。不要同时恢复旧云桥接。

Mac agent 仍保留。Mac 已使用注册后的连接器服务，旧 `local.remote-commander.reverse.aip` 同样已停止，其 plist 备份在 `.runtime/aip/`。不要重新加载旧反向服务。

当前连接器在 Mac 开机并登录后自动运行；检查或重新安装自己的启动项可执行：

```sh
cd "$HOME/remote-commander"
.venv/bin/python -m remote_commander.connect install --profile .runtime/device
```

这只恢复 Mac 设备，不是使用 comput1 的前提。

## 验收记录

- AIP 使用正式 Sites 服务凭据访问 `/relay/poll` 成功，HTTP 200。
- Mac 云桥接、gateway、旧 comput1 隧道和新的 Mac 反向隧道均停止后，ChatGPT 私有插件列出 AIP 与 comput1 在线、Mac 离线。
- 通过该插件在 comput1 实际执行命令，并使用文件工具写入、读取测试内容。
- 单独终止 AIP 的桥接和 SSH 隧道子进程，supervisor 生成新进程；待隧道建连后，ChatGPT 再次成功连接 comput1。
- 随后增加动态注册：新设备完整安装包配对、自动出现、断线重连及撤销均实际验证。Mac 与 comput1 的旧静态隧道退出后，动态连接继续正常工作，Windows 也已注册在线。
- AIP 本地 `.runtime/cutover-recovery.json` 保存初次迁移的历史状态；当前状态以注册表和设备探测为准，秘密不写入报告。
- 没有重启共享科研服务器；开机路径通过已安装 crontab 和运行中的 crond 验证，进程故障恢复通过实际终止/恢复验证。

使用条件为 **AIP 和 comput1 在线**。Mac、手机 Termius 以及最初用于部署的 SSH 会话均不是桥接工作所需的中间环节。
