# 部署与维护

本文供安装和维护实例的人使用。日常服务接入与研究偏好由用户在网页管理，见
[使用指南](web-settings.md)。LitRadar 面向单用户，没有多人账户与权限分配。

## 首次部署与访问保护

按 [README](../README.md#快速开始) 安装，然后使用 `litradar serve` 启动网页和独立调度进程。
默认只监听本机 `127.0.0.1:8090`。本文命令假设已激活虚拟环境；也可使用可执行文件的完整路径。

在实例本机签发一次性设置链接，将地址换成所有者实际使用的地址：

```bash
litradar setup-link --url http://127.0.0.1:8090
```

链接 30 分钟有效，完成密码设置后失效；重新签发使上一链接失效。只交给实例所有者。
需要从手机或其他设备访问时，配置可信的 HTTPS 反向代理和访问密码，再交付访问地址。
代理的转发头信任范围应限于实际代理。

忘记密码可在实例本机运行 `litradar admin-password` 重设，不需要重建数据库。
阅读登录有效期为 12 小时，设置与受保护操作需当前浏览器的 5 分钟授权。
网页、命令行和调度共用运行次数检查；接口口令用于脚本兼容，不替代受保护操作的授权。

## 配置位置

默认配置及配置中的相对路径以项目根目录为基准。指定独立实例时，建议明确使用绝对数据路径：

```bash
litradar -c /path/to/config.yaml serve
```

直接启动 Web 模块时用 `LITRADAR_CONFIG` 指定配置，并另外运行 `litradar scheduler`。
常驻进程、命令行和调度应使用同一实例配置。

| 文件或目录 | 备份用途 |
| --- | --- |
| `config.yaml` | 运行与连接设置 |
| `interests.yaml` | 私人研究偏好与检索条件 |
| `.litradar-secrets.json` | 应用管理的密钥、密码哈希与访问状态，须单独受限备份 |
| 配置旁的 `.env` | 旧部署的兼容凭据来源 |
| 数据目录 | 文献数据库、原始邮件和任务状态 |

默认路径可以调整，核对网页维护页显示的实际位置。配置参考
[config.example.yaml](../config.example.yaml)；研究偏好使用[空模板](../interests.example.yaml)。
不要将运行实例的文件写回这些公开示例。

凭据优先级为部署环境、应用私有文件、兼容 `.env`。要让用户在网页更换密钥，不要将
同名凭据固定注入服务环境。迁移旧服务时先确认兼容文件可读，再移除环境注入并重启。
保留仍被依赖的凭据文件。POSIX 私有凭据权限为 600，Windows 应限制目录 ACL。

## 自动更新

推荐由应用管理每天时间，部署层只负责让 `serve` 常驻。启用前需完成一次调度接管：

1. 检查该实例是否已有 systemd timer、launchd 日历任务、Windows 每日任务或外部脚本。
2. 停用旧的定时更新入口，等待当前任务结束，保留网页常驻服务。
3. 在实例本机执行以下命令，记录已核对并停用外部定时器：

```bash
litradar schedule-handoff --external-timers-stopped
```

4. 确认 `serve` 正常运行，在网页选择时间、时区和方向并启用计划。
5. 检查页面显示的调度响应与下次时间。

该命令不会替你停用系统任务，也不会立即开始更新。全新安装没有旧任务时也应核对后确认。
如果继续使用外部定时器，保持网页计划关闭。不要同时使用两个每日入口。

应用每天最多自动运行一次，重启只补当天错过的更新；失败或中断后当天不自动重试。
网页任务关闭页面后继续运行，服务器进程退出则会中断。普通排序可续跑未完成的评分。

## Linux 常驻服务

使用仓库内的 [systemd 模板](../deploy/litradar-web.service)。先将模板中的项目路径替换为
实际安装目录，并确认运行用户可读写设置、凭据、备份和数据目录，再安装服务：

```bash
sudo cp deploy/litradar-web.service /etc/systemd/system/litradar@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "litradar@$(whoami).service"
journalctl -u "litradar@$(whoami).service" -f
```

模板使用 `%i` 作为运行用户，因此服务名包含 `@`。旧每日任务的名称可能不同，停用前应核对。
[nginx 示例](../deploy/litradar-nginx.conf)提供 HTTPS 代理；使用前替换证书和端口，
运行 `nginx -t` 验证后再重载。

## macOS 常驻服务

使用用户 LaunchAgent，在登录期间保持 `litradar serve` 运行。创建 `data` 日志目录，
将以下路径全部替换为实际目录，保存到 `~/Library/LaunchAgents/com.litradar.web.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.litradar.web</string>
  <key>ProgramArguments</key><array>
    <string>/Users/your-user/LitRadar/.venv/bin/litradar</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/your-user/LitRadar</string>
  <key>StandardOutPath</key><string>/Users/your-user/LitRadar/data/web.log</string>
  <key>StandardErrorPath</key><string>/Users/your-user/LitRadar/data/web.err</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.litradar.web.plist
```

已有服务应更新原定义并重新加载，不要创建第二份。常驻网页任务不配置每日时间；
旧 `StartCalendarInterval` 更新任务须先卸载并停用。

## Windows 常驻服务

在任务计划程序中建立“登录时”启动的任务，使用以下配置：

| 项目 | 示例值，需替换安装路径 |
| --- | --- |
| 程序 | `C:\LitRadar\.venv\Scripts\litradar.exe` |
| 参数 | `serve --host 127.0.0.1 --port 8090` |
| 起始目录 | `C:\LitRadar` |
| 运行策略 | 允许持续运行，失败后重启 |

先停用原有的每日更新任务，再完成应用调度接管。需要未登录时运行，应使用合适的专用账户
并检查目录权限。在目标机器验证重启后的服务与调度状态。

## 升级、备份与恢复

1. 检查当前分支和本地修改，等待运行任务结束并停止实例进程。
2. 备份数据库、设置、研究偏好、邮件和私有凭据。SQLite 使用 WAL；运行中备份应使用
   SQLite 在线备份，不能只复制一个正在写入的数据库文件。
3. 确认跟踪分支正确后更新并安装依赖：

```bash
git status --short --branch
git pull --ff-only
.venv/bin/python -m pip install -e .
```

4. 重启网页与调度服务。数据库会自动迁移；检查登录、设置、文献列表和调度响应。

Windows 使用 `.venv\Scripts\python.exe`。运行模型兼容性测试会产生少量调用费用，
部署验证不必自动执行完整文献更新。

增量评分升级保留旧分数，并将无法确认偏好版本的结果显示为历史评分。旧 `rerank_top_k`
不再截断候选；在方向页选择新的历史重评策略。仍未获得 AI 分数的旧文献也会进入首次评分，
所以下一次运行可能补评一批历史积压。此后没有新文献、偏好变化或待完成任务时不重复精排。

维护页可恢复最近五份普通设置或方向备份，凭据和数据库需单独恢复。恢复运行设置会暂停
自动更新。若配置损坏导致网页无法打开，在停机后从有效备份恢复并重启。
回退版本时使用与该版本匹配的代码和数据库备份，不直接用旧代码打开已升级的数据。
