# 部署与维护

本文适用于已经完成 [快速开始](../README.md#快速开始) 的安装。
Web 服务与每日流水线是两个独立进程：前者负责阅读和手动操作，后者定时执行
`litradar run`。两者应使用同一份配置和数据库。

默认访问地址为 `127.0.0.1:8090`，流水线窗口统一取自
`config.yaml` 的 `app.pipeline_window_days`。示例中的项目路径与运行用户需要按部署环境调整。

## 访问保护

应用面向单用户自托管，没有账号和多用户权限体系。局域网访问建议由反向代理提供
HTTPS，并在 `.env` 中设置 `LITRADAR_TOKEN`。首次访问可使用 `?k=<token>`，之后由
Cookie 携带凭据；API 请求也支持 `X-Token` 请求头。

接口口令可能出现在浏览器历史或访问日志中，应妥善保管。反向代理负责 HTTPS 等连接
设置，LitRadar 的接口口令负责应用访问校验，仅启用代理并不会自动启用口令保护。

| 控制项 | 作用与范围 |
|---|---|
| `LITRADAR_TOKEN` | 配置后校验页面与操作请求的凭据；健康检查不受此限制 |
| 同源校验 | 拒绝浏览器跨源写请求，防止其他网页触发本机操作 |
| 非回环地址检查 | `app.host` 为非回环地址且未配置口令时，拒绝 `/admin/run/*` 请求 |
| 管理员密码 | 对 `admin.guarded_stages` 中的网页操作再次验证密码，默认保护 `rank`、`summarize`、`all` |
| 冷却与每日上限 | 网页手动运行前检查 `run_log`，默认冷却 60 秒、每阶段每日上限 3 次 |

管理员密码通过以下命令设置，程序仅将其 PBKDF2 哈希写入 `.env`，不保存明文：

```bash
litradar admin-password
```

使用 `litradar admin-password --clear` 可清除。修改已有密码或环境变量后，应重启 Web
进程以加载新值。管理员密码属于额外的密码验证，不构成双因素认证。

运行次数按组检查；`all` 检查其子阶段的记录，邮件和元数据补全的全局记录计入每个组。
CLI 和定时任务的执行也写入这些记录，但冷却与上限的拦截只发生在 Web 入口，因此
不能作为整个系统的费用硬上限。将某项限制设为 `0` 可关闭该项检查。

启动服务时，Uvicorn 的 `--host`、`--port` 决定实际监听地址，应与 `config.yaml`
中的 `app.host`、`app.port` 保持一致。Web 模块不会用这两个配置字段替代 Uvicorn 参数。

## Linux：systemd 与 nginx

[deploy/](../deploy/) 提供模板单元和 nginx 示例。先将单元文件中的
`/srv/Work/LitRadar` 修改为实际安装目录，包括 `WorkingDirectory`、`EnvironmentFile`、
`ExecStart` 与 `ReadWritePaths`；确保运行用户能访问配置和写入所需数据。

以下命令将当前用户作为服务用户。若使用专用账户，请替换实例名中的用户名。

```bash
sudo cp deploy/litradar-web.service /etc/systemd/system/litradar@.service
sudo cp deploy/litradar-daily@.service /etc/systemd/system/
sudo cp deploy/litradar-daily@.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now "litradar@$(whoami).service"
sudo systemctl enable --now "litradar-daily@$(whoami).timer"

systemctl list-timers 'litradar*'
journalctl -u "litradar@$(whoami).service" -f
```

三个文件必须以带 `@` 的模板名称安装，`%i` 才能解析为运行用户。
每日任务默认在系统时区的 07:00 触发，附加最多 10 分钟的随机延迟；机器关机时错过的
任务在恢复后补跑。修改时间请编辑 timer 的 `OnCalendar`。

[nginx 示例](../deploy/litradar-nginx.conf) 将 HTTPS 的 3090 端口转发到
`127.0.0.1:8090`。安装前须核对监听端口和证书路径；示例中的 `/etc/nginx/ssl/dsh.*`
是既有环境的路径，不能假定在新机器上存在。文件末尾提供了生成独立证书的参考命令，
使用自签证书时应在客户端核对并信任相应证书。

```bash
sudo cp deploy/litradar-nginx.conf /etc/nginx/conf.d/litradar.conf
sudo nginx -t
sudo systemctl reload nginx
```

仅在 `nginx -t` 检查通过后重载。局域网用户通过代理地址访问，应用本身仍监听回环地址。

## macOS：launchd

以下 LaunchAgent 每天 07:05 运行流水线，适用于用户登录期间运行的任务。
将 `/Users/your-user/LitRadar` 全部替换为实际项目路径，并预先创建日志目录 `data/`。

保存为 `~/Library/LaunchAgents/com.litradar.daily.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.litradar.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/your-user/LitRadar/.venv/bin/litradar</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/your-user/LitRadar</string>
  <key>StandardOutPath</key><string>/Users/your-user/LitRadar/data/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/your-user/LitRadar/data/launchd.err</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>5</integer>
  </dict>
</dict>
</plist>
```

加载任务，或手动触发一次：

```bash
launchctl load ~/Library/LaunchAgents/com.litradar.daily.plist
launchctl start com.litradar.daily
```

Web 服务可使用另一个 plist：使用独立的 `Label`（如 `com.litradar.web`）和日志路径，
将 `ProgramArguments` 改为 Python 的绝对路径及 `-m`、`uvicorn`、
`litradar.web.app:app`、`--host`、`127.0.0.1`、`--port`、`8090`；
删除 `StartCalendarInterval`，加入 `<key>RunAtLoad</key><true/>` 与
`<key>KeepAlive</key><true/>`。

## Windows：任务计划程序

在 PowerShell 中创建任务。示例假设项目位于 `C:\LitRadar`，需要按实际路径替换。

```powershell
$litradarWebAction = New-ScheduledTaskAction `
  -Execute "C:\LitRadar\.venv\Scripts\python.exe" `
  -Argument "-m uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090" `
  -WorkingDirectory "C:\LitRadar"
Register-ScheduledTask -TaskName "LitRadarWeb" -Action $litradarWebAction `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Limited

$litradarDailyAction = New-ScheduledTaskAction `
  -Execute "C:\LitRadar\.venv\Scripts\litradar.exe" `
  -Argument "run" -WorkingDirectory "C:\LitRadar"
Register-ScheduledTask -TaskName "LitRadarDaily" -Action $litradarDailyAction `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 07:05) -RunLevel Limited

Get-ScheduledTaskInfo -TaskName "LitRadarDaily"
```

上述任务按当前用户配置。若需在用户未登录时运行，应在任务计划程序中另行设置账户
和登录选项。可用 `Start-ScheduledTask -TaskName "LitRadarDaily"` 手动执行一次。

CLI 已将标准输出与错误输出设为 UTF-8，以支持重定向后的中文日志。自编包装脚本时，
也可设置 `PYTHONUTF8=1`。当前 Windows 的完整流水线与定时运行仍需在实际部署环境验证。

## 配置加载与并发运行

默认配置文件、`.env` 和相对路径均以项目根目录解析，根目录由已安装模块的位置确定。
CLI 可使用 `litradar -c /path/to/config.yaml <command>` 指定配置，Web 服务可通过
`LITRADAR_CONFIG` 指定。调度器仍建议设置工作目录，便于日志和自定义脚本使用相对路径。

进程中已有的非空环境变量优先于 `.env`，空占位值不会覆盖有效凭据。
更改 `.env` 中已有的密钥或口令后，重启常驻进程以确保新值生效。

CLI 与网页操作共用数据库目录中的 `litradar.lock`。POSIX 使用 `fcntl.flock`，
Windows 使用 `msvcrt` 字节范围锁；进程结束后由操作系统释放。已有流水线运行时，
新的 CLI 流水线跳过，网页手动运行返回冲突提示。

## 更新与备份

更新前检查当前分支和本地改动，并备份数据库、`.env`、`config.yaml`、`interests.yaml`
以及需要保留的邮件文件。SQLite 使用 WAL；直接复制数据库文件时，应先停止使用该库
的进程，避免遗漏尚未合并的写入，也可以使用 SQLite 的在线备份功能。

在确认检出的分支正确、跟踪目标已配置且本地改动已妥善保存后，可在项目目录执行：

```bash
git status --short --branch
git pull --ff-only
.venv/bin/python -m pip install -e .
```

Windows 将最后一条命令的 Python 路径替换为 `.venv\Scripts\python.exe`。
随后重启 Web 服务，使其加载新代码；数据库在连接时自动执行尚未完成的迁移。
升级后可运行 `litradar check` 并查看日志。该检查会访问外部服务，LLM 可用时也会发起
一次小型测试调用。

`git pull --ff-only` 在分支分叉时会停止，应先确认差异再处理。切换旧代码前，也应确认
它是否兼容已升级的数据库；需要回退时使用与备份匹配的代码和数据。
