# 部署与维护

本文面向安装、升级实例的维护者。日常配置见 [网页设置指南](web-settings.md)。
推荐由 `litradar serve` 启动网页和一个独立调度子进程，部署层只负责让它们常驻。
两者使用相同配置和数据库；计划由网页管理，默认关闭。

## 首次部署与访问保护

完成 [快速开始](../README.md#快速开始) 后，在实例本机签发一次性设置链接：

```bash
.venv/bin/litradar setup-link --url http://127.0.0.1:8090
.venv/bin/litradar serve --host 127.0.0.1 --port 8090
```

`--url` 应填写所有者实际访问的地址，局域网实例通常是 HTTPS 代理地址。将链接仅交给
实例所有者；30 分钟后或完成密码设置后失效。重新签发使上一链接失效。网页建立密码
后可直接接入服务、添加研究方向，无需复制示例配置。

应用面向单用户，有访问密码和登录会话，没有多用户权限管理。建议由反向代理提供
HTTPS，信任的代理地址和转发头须在启动环境配置，不允许客户端伪造代理头。
同源校验使用请求的实际协议、主机和端口。

| 控制项 | 当前行为 |
|---|---|
| 访问密码 | 首次设置后保护网页与 API；PBKDF2 哈希保存于私有文件 |
| 阅读登录 | 12 小时、HttpOnly、SameSite=Strict；HTTPS 下设置 Secure，更换密码使旧会话失效 |
| 设置与操作授权 | 网页内验证后 5 分钟有效，独立签名的 HttpOnly Cookie，仅当前浏览器有效；读取或保存不续期，可主动退出，到期保留输入并重新验证 |
| 接口口令 | 兼容 `X-Token`、`?k=` 与已有 Cookie，可在维护页单独管理 |
| 同源校验 | 拒绝浏览器跨源写入、运行、连接测试与预览 |
| 受限网页阶段 | 需要上述 5 分钟授权；兼容脚本逐次发送 `X-Admin-Password`，阅读登录本身不能替代操作授权 |
| 冷却与每日次数 | 网页、CLI、应用调度共用检查，获得流水线锁后再判断 |

普通访客不能通过设置接口签发链接或抢先设置密码，不根据代理后的客户端 IP 判断
所有者。健康检查 `/healthz` 公开。旧 URL 口令可能进入历史和日志，新脚本优先使用
`X-Token`。已有 `.env` 密码哈希自动兼容。

网页手动运行使用后台线程，进度保存在数据库旁的 `<数据库文件名>.web-jobs.json`，
最近 20 个请求标识用于避免重复提交。所有 worker 与 CLI、调度共用文件锁。
页面断开或 5 分钟授权到期不取消已启动的任务；进程退出会中断后台任务，后续查询
在确认流水线锁已释放后标记中断，不自动重试。进度查询仍要求有效的阅读登录或接口口令。

忘记密码可在本机运行 `litradar admin-password`，新哈希立即生效。`--clear` 用于受控
恢复，会移除应用管理的密码；应先限制实例访问并重新签发设置链接。部署环境管理的
密码需在部署环境修改。

## 自动更新接管与升级

新版本不会关闭旧系统定时入口。流水线锁不能阻止两个定时器先后执行，因此启用网页
计划前必须完成一次明确接管：

1. 检查此实例的启动入口、配置路径和数据库，确认是否有 systemd timer、launchd
   日历任务、Windows 每日任务或自定义脚本。
2. 停用这些定时入口，等待当前流水线结束。保留常驻网页入口。
3. 执行 `litradar schedule-handoff --external-timers-stopped` 记录确认。命令不替维护者
   停用系统任务，也不立即开启计划；它将计划置为关闭，允许随后由网页启用。
4. 使用 `litradar serve`，或分别启动 Uvicorn 和 `litradar scheduler` 常驻服务。
5. 在网页选择时间、时区和方向，保存并确认调度进程有响应。

全新安装没有旧定时器时，也须由安装代理核对后记录确认。若继续使用外部定时器，
保持网页自动更新关闭。原 `deploy/litradar-daily@.service` 和 `.timer` 仍可用，
此时更新时间由系统管理，不能在网页更改。

调度进程通常每 30 秒读取配置，用数据库旁的 `litradar-scheduler.lock` 保证唯一执行者。
多个 Web worker 不注册任务。执行前还需取得 `litradar.lock`，然后以本地日期唯一键
持久认领当天任务。恢复后只补跑当天已到时间且未认领的任务；更早日期不补。失败或
中断的当天任务不自动重试，用户可检查记录后手动更新。夏令时缺失时间顺延，重复时间
取第一次。

次数以 `app.timezone` 的自然日计数，分组阶段按组计算，全局邮件和补全记录计入相关
检查；完整更新也检查受限子阶段。次数不是货币预算，连接测试、预览和关键词建议
不计入流水线次数。

## Linux：systemd 与 nginx

[网页服务模板](../deploy/litradar-web.service) 使用 `litradar serve`。将
`/srv/Work/LitRadar` 替换为安装目录，包括 `WorkingDirectory`、`ExecStart` 和
`ReadWritePaths`；运行用户须能读写设置、凭据、锁、备份和数据目录。

```bash
sudo cp deploy/litradar-web.service /etc/systemd/system/litradar@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "litradar@$(whoami).service"
journalctl -u "litradar@$(whoami).service" -f
```

模板使用 `%i` 作为用户，安装名称须带 `@`。接管旧每日任务时先核对实际名称，例如：

```bash
sudo systemctl disable --now "litradar-daily@$(whoami).timer"
systemctl list-timers 'litradar*'
.venv/bin/litradar schedule-handoff --external-timers-stopped
```

新模板不将 `.env` 作为 `EnvironmentFile` 注入进程；应用自行读取兼容文件，让网页
更换密钥能够生效。若旧服务或 override 仍有 `EnvironmentFile`，凭据会显示为部署
管理。迁移时先确认文件位于配置旁且可读，再移除该注入、重载服务定义并重启。
不要先删除仍被依赖的凭据文件。

[nginx 示例](../deploy/litradar-nginx.conf) 将 HTTPS 的 3090 端口转发到本机 8090。
核对端口和证书路径；示例证书并非新机器上必然存在。只在验证通过后重载：

```bash
sudo cp deploy/litradar-nginx.conf /etc/nginx/conf.d/litradar.conf
sudo nginx -t
sudo systemctl reload nginx
```

客户端须信任所用证书。
示例为邮件上传放宽请求体限制，并在访问日志中省略查询字符串和 Referer。
应用的 Uvicorn 日志同样省略查询字符串，避免一次性设置代码与旧接口口令进入访问日志。

## macOS：launchd

以下 LaunchAgent 在用户登录期间保持网页与调度进程运行。先创建日志目录 `data/`，
替换实际路径，保存为 `~/Library/LaunchAgents/com.litradar.web.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.litradar.web</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/your-user/LitRadar/.venv/bin/litradar</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/your-user/LitRadar</string>
  <key>StandardOutPath</key><string>/Users/your-user/LitRadar/data/web.log</string>
  <key>StandardErrorPath</key><string>/Users/your-user/LitRadar/data/web.err</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.litradar.web.plist
```

若旧 `com.litradar.daily` 已加载，用它的实际文件路径执行 `launchctl bootout` 卸载，
并移走或停用旧启动配置，防止下次登录重新加载，再记录接管确认。新的常驻服务中
不要保留 `StartCalendarInterval`。

## Windows：任务计划程序

示例假设项目在 `C:\LitRadar`，创建登录时启动的常驻任务：

```powershell
$litradarWebAction = New-ScheduledTaskAction `
  -Execute "C:\LitRadar\.venv\Scripts\litradar.exe" `
  -Argument "serve --host 127.0.0.1 --port 8090" `
  -WorkingDirectory "C:\LitRadar"
$litradarWebSettings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "LitRadarWeb" -Action $litradarWebAction `
  -Settings $litradarWebSettings `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Limited
Start-ScheduledTask -TaskName "LitRadarWeb"
```

升级时修改原任务，不创建第二份。若有旧 `LitRadarDaily`，先禁用它，等待正在执行的
更新结束，再运行接管命令。调度进程由 `serve` 启动，不再创建每日任务。需要用户未
登录也运行时，在任务计划程序中设置专用账户和登录选项。

项目包含 `tzdata` 依赖，供缺少系统时区库的平台使用。私有凭据在 POSIX 创建为 600；
Windows 需通过目录 ACL 限制访问。中文输出采用 UTF-8。本次新调度流程的 Windows
实机服务生命周期仍需在目标部署验证，模拟锁测试不能代替它。

## 配置、凭据与监听

默认配置和配置内相对路径基于安装模块所在项目根目录解析。CLI 用
`litradar -c /path/to/config.yaml <command>`，Web 用 `LITRADAR_CONFIG` 指定配置。
`.env` 和 `.litradar-secrets.json` 位于所选配置文件旁；配置中的相对数据路径仍基于
项目根目录，不因 `-c` 自动改为配置所在目录。

凭据优先级为部署环境、应用私有文件、兼容 `.env`。显式环境空值也属于部署管理。
网页清除写入屏蔽标记，防止旧 `.env` 值重新生效。任务使用开始时的配置和凭据快照，
下一次调用读取新值。私有凭据不进入普通配置导出、常规备份或版本控制。

`litradar serve` 默认采用 `app.host/port`，命令行参数可覆盖。直接使用 Uvicorn 时由
其 `--host/--port` 决定监听，并应另开一个 `litradar scheduler` 常驻进程。监听、证书、
文件路径迁移需要独立停机备份、调整部署并重启验证，普通网页保存不执行这些操作。

所有入口共用流水线锁，POSIX 使用 `fcntl.flock`，Windows 使用 `msvcrt` 字节范围锁，
崩溃后由内核释放。设置读写使用短期锁并校验表单版本。相同实例必须使用同一配置
路径和数据库，不能复制配置让多套任务争用同一个数据目录。

## 更新、备份与恢复

升级前检查分支和本地改动，备份数据库、普通设置、方向、邮件及私有凭据。SQLite
使用 WAL，直接复制数据库前应停止该库的全部进程，或使用 SQLite 在线备份。
确认跟踪分支和本地修改后执行：

```bash
git status --short --branch
git pull --ff-only
.venv/bin/python -m pip install -e .
```

Windows 使用 `.venv\Scripts\python.exe`。重启网页与调度服务加载新代码，数据库连接时
自动迁移。旧单方向保留 `default` 身份，网页保存才转换为分组格式。升级代码不会自动
确认外部定时器已停用，首次接管仍须完成迁移。

维护页可恢复最近五份设置或方向备份。恢复运行设置会暂停计划，保留当前访问、监听
和数据库位置。若手工改坏配置导致网页打不开，应停止进程，由代理在本机将有效备份
复制回原路径，检查后再重启。私有凭据从单独的受限备份恢复；忘记密码也可使用本机
密码命令。

`litradar check` 会访问外部服务，模型可用时会验证一个评分与摘要小样例；参数自动
适配可能增加少量请求。日常诊断可在网页按服务进行。升级验证包括登录、保存方向、
连接状态、调度响应和一轮受控更新。回退代码前
确认数据库兼容性，必要时使用与备份匹配的代码和数据。
