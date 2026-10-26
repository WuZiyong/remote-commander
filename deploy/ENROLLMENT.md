# 同账号设备配对与自动发现

AIP 是常驻网关。通过当前所有者的私有 ChatGPT 入口签发一次性安装包，新设备运行安装器后，会登记到同一网关账户并自动出现在 `list_devices`。后续断线自动重连，在线状态通过心跳与实际 agent 探测共同确认，不需要逐台编辑 `devices.json`，也不需要重启桥接。

该机制使用私有入口管理的所有者账户和网关的稳定账户 ID，不读取或继承 Desktop Commander 的设备列表，也不扫描局域网。新设备仍须完成一次配对安装。

## 新增设备

在 ChatGPT 中要求“添加设备 xxx”，由已认证的 AIP 管理工具生成该设备的安装包。也可以在 AIP 项目目录执行：

```sh
.venv/bin/python -m remote_commander.registry \
  --db .runtime/registry/devices.sqlite invite \
  --device-id my-laptop \
  --output .runtime/registry/my-laptop-invite.json \
  --bundle .runtime/registry/my-laptop.zip
```

命令只返回文件路径、设备 ID 和有效期，不输出配对密钥。安装包是私密的一次性凭据，默认 30 分钟有效，只能成功兑换一次；同一设备 ID 不会被另一个安装包静默覆盖。

将安装包私下下载到目标设备，解压到用户自己的固定目录。需要 Python 3.11+、OpenSSH 的 `ssh`/`ssh-keygen`，以及访问 PyPI 和 `erebos.aip.de:2222` 的网络。目标设备无需 AIP 密码、原有 SSH 登录密钥或 GitHub 登录。

```sh
python setup_device.py --allow-shell
```

Linux/macOS 按实际环境使用 `python3` 或 `python3.11`；Windows 可使用 `py -3.12 setup_device.py --allow-shell`。安装器创建独立虚拟环境、安装依赖、配对、安装后台启动任务，并在成功后删除解压目录中的配对 JSON。不要移动已安装的目录。

默认文件范围为当前用户主目录。可以增加 `--root /指定目录`（可重复）；不加 `--allow-shell` 时只提供文件工具。若本机已有服务占用 8765，可增加 `--port 18766`。

## 自动连接与启动

设备在本机生成自己的 Ed25519 私钥，该私钥不会上传。一次性引导密钥只能执行固定配对命令，不能转发端口或执行普通命令；配对成功后立即从 AIP 的受管 SSH 公钥区块移除。

正式设备密钥只允许固定心跳命令和分配给该设备的回环 TCP 监听端口。客户端严格核验安装包中的已验证入口公钥；不接受自动替换服务器公钥。AIP 的 2222 入口使用独立 SSH 公钥，应从已有可信连接获得，不能用系统默认 `/etc/ssh` 公钥替代。

设备端连接器持有单实例锁，每 15 秒发送心跳；失联后逐步退避，最多约 30 秒重试间隔。AIP 将超过 90 秒无心跳的设备视为离线，不再把请求发往旧端口。网关只使用自己分配的回环端口，不接受新设备指定任意服务地址。

| 平台 | 自启与恢复 |
| --- | --- |
| Linux | 当前用户独立 cron 标记区块：`@reboot` 和每五分钟检查；supervisor 管理连接器 |
| macOS | 当前用户 launchd：登录时加载，退出后自动恢复 |
| Windows | 当前用户登录任务与每五分钟检查：独立 supervisor，无需保存密码，隐藏运行，无三天运行上限 |

Mac/Windows 在用户登录后启动；Linux 不需要保持 SSH 登录会话。连接器自己的 agent 在连接器正常退出时关闭；若连接器异常退出，重启后核对 PID、进程创建时间及配置路径，接管仍存活的原 agent，避免产生重复服务。

配置、设备私钥及状态在安装目录 `.runtime/device/`。Windows 会为 SSH 私钥设置当前用户专用 ACL；Unix 使用 700 目录和 600 文件权限。

## 查看和撤销

ChatGPT 的 `list_devices` 自动反映当前已登记设备。AIP 本地管理命令：

```sh
.venv/bin/python -m remote_commander.registry --db .runtime/registry/devices.sqlite list
.venv/bin/python -m remote_commander.registry --db .runtime/registry/devices.sqlite revoke my-laptop
```

撤销后，网关立即停止向该设备路由，受管 SSH 公钥移除；正常连接器在下次心跳收到拒绝后进入停用状态。撤销不会退回同名旧静态路径。重新配对使用新的设备 ID，保留旧 ID 和端口以防旧连接或会话被错误复用。

注册表 `.runtime/registry/devices.sqlite` 同时包含路由 token，应与私有 env 一样保护和备份，不进入 Git。注册表初始化只能执行一次，不能通过重新初始化更换账户身份。

## 已验证

2026-10-26，通过完整安装包创建临时 macOS 设备，在不修改网关配置、不重启桥接的情况下自动发现，并通过 ChatGPT 完成文件读写。中断其 SSH 连接后观察到自动重连；撤销后 ChatGPT 立即拒绝访问。随后迁移正式 Mac 与 comput1，并真实配对 Windows ZYWU，均通过当前私有插件实际执行命令。临时设备已撤销，自启服务已清理。

OpenSSH 限制语义参考 [sshd 官方文档](https://man.openbsd.org/sshd.8)；Windows 用户登录触发器参考 [Microsoft 官方文档](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-logontrigger-triggergroup-element)。
