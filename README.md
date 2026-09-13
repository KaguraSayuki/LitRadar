# LitRadar

个人文献雷达 —— 面向化学研究者的自托管文献追踪工具。聚合订阅邮件与开放学术 API，
使用 LLM 完成个性化排序与中文结构化摘要，在局域网 Web 界面中阅读与反馈。

> A self-hosted, single-user literature radar for chemists: multi-source ingestion,
> LLM re-ranking with explanations, and structured Chinese summaries.

## 功能特性

- **多源采集**：X-MOL 订阅邮件解析、Semantic Scholar 布尔检索、引用滚雪球、Crossref 检索（可选），所有来源按 DOI 去重合并
- **元数据富化**：Crossref 权威元数据、Semantic Scholar 摘要与引用数、easyScholar 期刊等级（影响因子 / 中科院分区 / 北核等）
- **三阶段排序**：规则过滤 → BM25 粗排 → DeepSeek listwise 精排，每篇输出分数与"为什么推荐"的中文理由
- **中文结构化摘要**：问题 / 方法 / 关键结果 / 局限 / 对研究的用处；关键数字与英文原文逐字核验，推断内容显式标注
- **反馈闭环**：收藏 / 已读 / 不感兴趣三态反馈分别落库，手动决定与自动规则解耦，并参与后续精排
- **轻量自托管**：FastAPI + SQLite + 原生前端（零构建），systemd 定时任务 + nginx 反向代理

## 工作原理

```
X-MOL 订阅邮件 (.eml / IMAP) ─┐
Semantic Scholar 布尔检索 ────┤                ┌─ 富化：摘要 / 引用数 / 期刊等级
引用滚雪球（种子文献被引）────┼→ DOI 去重入库 ─┤
Crossref 关键词检索（可选）───┘    (SQLite)    └─ 排序：规则 → BM25 → LLM 精排
                                                        ↓
                                          中文结构化摘要 → Web 界面 / 反馈
```

### 数据源

| 环节 | 数据源 | 说明 | 成本 |
|---|---|---|---|
| 精选 | X-MOL 订阅邮件 | 仅解析用户自己收到的订阅邮件，不抓取网站 | 免费 |
| 检索 | Semantic Scholar `/paper/search/bulk` | 精确布尔查询，召回主力 | 免费（建议申请 Key） |
| 检索 | 引用滚雪球 `/paper/{id}/citations` | 沿种子文献的被引关系，发现关键词覆盖不到的工作 | 免费 |
| 检索 | Crossref 关键词检索 | 模糊匹配，召回高、噪声大，默认关闭 | 免费 |
| 富化 | Crossref | 期刊全称 / ISSN / 作者等权威元数据 | 免费 |
| 富化 | Semantic Scholar `/paper/batch` | 按 DOI 批量补摘要与引用数 | 免费 |
| 富化 | easyScholar 开放接口 | 影响因子、中科院分区、北核、CSCD 等期刊等级 | 按次计额，结果本地缓存 |
| 排序 / 摘要 | DeepSeek `deepseek-chat` | listwise 精排与结构化摘要 | 低 |
| 可选 | OpenAlex | 2026 年起为 API Key + 额度制，默认关闭 | 额度制 |

## 快速开始

环境要求：Python ≥ 3.11。Linux / macOS / Windows 都能运行，差别只在虚拟环境的可执行
文件目录（Windows 是 `Scripts\`，POSIX 是 `bin/`）和几个命令名，下面分开给。

### Linux / macOS

```bash
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar

# 1. 安装
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 配置（三个文件均不入库，仅提交对应的 .example）
cp config.example.yaml   config.yaml
cp interests.example.yaml interests.yaml
cp .env.example          .env && chmod 600 .env
#    编辑 .env：至少填写 DEEPSEEK_API_KEY；邮件接入需 IMAP_PASSWORD

# 3. 初始化数据库
.venv/bin/litradar init-db

# 4. 运行完整流水线（采集 → 富化 → 排序 → 摘要）
.venv/bin/litradar run

# 5. 启动 Web 服务
.venv/bin/uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090
```

### Windows（PowerShell）

Windows 上没有 `python3` / `cp` / `chmod`，虚拟环境的可执行文件也在 `Scripts\` 而不是
`bin/`；照抄下面这份即可：

```powershell
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar

# 1. 安装（`-e .` 末尾那个点是"当前目录"，别漏）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .

# 2. 配置
Copy-Item config.example.yaml    config.yaml
Copy-Item interests.example.yaml interests.yaml
Copy-Item .env.example           .env
#    编辑 .env：至少填写 DEEPSEEK_API_KEY；邮件接入需 IMAP_PASSWORD

# 3. 初始化数据库
.venv\Scripts\litradar.exe init-db

# 4. 运行完整流水线（采集 → 富化 → 排序 → 摘要）
.venv\Scripts\litradar.exe run

# 5. 启动 Web 服务
.venv\Scripts\python.exe -m uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090
```

> 想用激活也可以：`.venv\Scripts\Activate.ps1`，之后直接敲 `litradar` / `python`。
> 若报 `running scripts is disabled on this system`，先在当前窗口执行
> `Set-ExecutionPolicy -Scope Process RemoteSigned`。上面那种"写全路径"的写法
> 不需要激活，也就不会撞到执行策略。
>
> 虚拟环境目录名可以自己取（有人习惯就叫 `venv`），但下文一律按 `.venv` 写；
> 换了名字记得把命令里的路径一起替换。
>
> 另外，下文各处的 `litradar xxx` 都按"已激活虚拟环境"来写。没激活就把前缀补全：
> Windows 用 `.venv\Scripts\litradar.exe xxx`，Linux / macOS 用 `.venv/bin/litradar xxx`。

浏览器访问 `http://127.0.0.1:8090`。首次使用建议先运行 `litradar check`
体检配置、密钥、数据库与各 API 连通性。

`.env` 的权限在不同平台上含义不同：POSIX 上请保持 `chmod 600`（`litradar check`
会检查）；Windows 上 `chmod` 只能切换只读位，实际由文件 ACL 决定，请通过资源管理器
或 `icacls .env /inheritance:r /grant:r "%USERNAME%:R"` 限制访问。

## 配置说明

| 文件 | 用途 |
|---|---|
| `.env` | 密钥：DeepSeek / Semantic Scholar / easyScholar / IMAP / 接口口令 |
| `config.yaml` | 运行参数：端口、时间窗、数据源开关、排序权重、期刊等级展示规则 |
| `interests.yaml` | 研究画像：检索式、关键词、期刊白名单、关注作者、滚雪球种子 |

各字段在示例文件中均有详细注释，以下仅列关键约定：

- **时间窗只有一个来源**：`app.pipeline_window_days`（默认 200 天），命令行、定时任务与
  Web 界面按钮共用。它必须 ≥ 抓取窗口 `sources.s2_search_lookback_days`，否则新抓取的
  文献会落在排序窗口之外，始终处于"未评分"状态
- **两套检索词分开配置**：Crossref 使用自然语言（`search_queries`），Semantic Scholar
  必须使用其查询语法（`s2_queries`：`+` 与、`|` 或、双引号短语、`-` 排除）。裸词会被
  当作整句短语匹配，可能静默返回 0 条结果，因此两者不能共用
- **多条查询取并集**：扩大召回优先增加查询条数而非放宽单条精度，精确率交给 LLM 精排兜底
- **排序权重**：使用 `ranking.weights.llm/coarse/rule`，兼容早期的 `w_llm/w_coarse/w_rule`。
  权重须为非负有限数值且总和大于 0；两种写法冲突时会在加载配置时报告错误
- 检索词与偏好可在 Web 界面 `/interests` 直接编辑，保存前校验 YAML 字段及列表元素类型并
  自动备份原文件。旧文件存在字段类型错误时仍可打开编辑页修复

## 邮件接入

X-MOL 的「私人定制」订阅由其网站开通，本项目只解析投递到你邮箱的订阅邮件。
支持三种模式（`mail.mode`）：

- **`folder`（默认）**：将邮件导出为 `.eml` 放入 `data/inbox/`，处理后自动移至 `data/inbox/processed/`
- **`maildir`**：读取标准 Maildir 目录
- **`imap`**：直连收件箱，仅读取匹配 `imap_search` 的邮件

注意事项：

- Outlook 个人账号已禁用 IMAP 密码登录（能力声明含 `LOGINDISABLED`，仅支持 OAuth2）。
  推荐在 Outlook 中设置转发规则，把发件人含 `newsletter.x-mol.com` 的邮件转发至支持
  授权码登录的邮箱（QQ / 163 / 126 / 飞书 / 腾讯企业邮箱均实测可用），再以该邮箱接入
- 配置完成后用 `litradar mail-test` 做只读连通性测试：报告匹配邮件数并实际解析一封
  展示提取结果，不标记已读、不改动任何邮件
- 每封邮件的原文、条目和完成标记在同一事务中写入，全部成功后才确认邮件；入库失败会
  回滚本封的写入，留待下次重试。升级后的首次邮件采集会从数据库原文重放旧版未标记
  完成的邮件，即使邮件已移入 processed 或被 IMAP 标为已读，也能补齐遗漏条目

## 命令行

```bash
litradar init-db        # 初始化数据库
litradar ingest all     # 采集：邮件 + 检索 + 滚雪球
litradar enrich         # 富化：补摘要、引用数、期刊等级
litradar rank           # 三阶段排序
litradar summarize      # 生成中文摘要（默认补缺失及原文已变化的摘要，--force 全量重做）
litradar run            # 完整流水线（以上全部）
litradar stats          # 数据统计
litradar mail-test      # IMAP 连通性测试（只读）
litradar check          # 体检:配置 / 密钥 / 数据库 / 网络 / LLM 连通性
litradar parse          # 仅解析邮件（调试解析器用）

python -m pytest tests/ -q   # 运行测试
```

Web 界面的「统计」页也可手动触发各阶段。

## 设计要点

**排序：LLM 主导的三阶段漏斗。** 最终分 = 0.85 × LLM + 0.10 × BM25 + 0.05 × 规则。
规则层负责硬性过滤（排除词）与加分（核心词 / 期刊白名单 / 关注作者）；BM25 粗排仅
提供送入 LLM 的批次顺序，默认不截断候选（`llm.rerank_top_k: 0`），避免弱信号对强信号
行使否决权；DeepSeek 分批 listwise 打分，各批共用同一评分标准。期刊匹配做了缩写归一
（`Org. Lett.` ↔ `Organic Letters` 等，有测试覆盖）。
单批 LLM 请求失败时继续处理后续批次，保留已成功的结果，失败批次使用既有的规则与粗排降级逻辑。

**DOI 一致性。** 检索、富化和入库统一使用小写 DOI。旧数据库首次连接时会自动合并仅
DOI 写法不同的条目，保留关联元数据、评分、摘要和反馈；有冲突的手动状态按最新反馈决定。

**引用滚雪球：增量累积 + 共被引标注。** 以 `interests.yaml` 中 `seed_dois` 为种子，
沿"引用了种子的论文"方向发现换了说法、关键词覆盖不到的新工作。每轮只刷新最久未查的
少量种子以规避限流，引用关系持久化于 `seed_cite` 表，共被引数跨全部种子与历史轮次累积；
共被引作为质量标注展示（`滚雪球 ×2`），默认不作为准入门槛。种子宜选被引仍活跃的文献，
过新的论文被引数不足，滚不出结果。

**摘要防幻觉。** 摘要中 `key_results` 出现的每个数字与英文原文逐字比对，未命中者标注
⚠️ 提示核对；允许模型合理推断（如原文未明说的研究动机），但推断内容显式标注"（推断）"，
与原文事实区分。
摘要同时保存原文指纹；原文补齐或改变后会刷新简要或深度摘要，单独更新引用数不会触发重做。

**反馈三态分流。** 收藏免疫后续规则过滤——之后收紧检索词也不会移除明确的手动决定；
不感兴趣移入独立页签、可逐条恢复；被规则否决的条目以"已否决"状态可见而非静默消失。
任何条目都能追溯"为什么在 / 不在这个列表里"。

**期刊等级本地缓存。** easyScholar 按刊名查询且按次计额，结果缓存于 `journal_rank` 表，
按配置中的别名目标查询、去重及复用缓存；只有接口明确返回无结果才缓存为空，网络、限流、
认证或协议错误留待下次重试。旧版无法区分故障的空缓存会在升级时清理一次。
展示字段与标签压缩规则（`化学1区` → `化1`）在
`config.yaml` 的 `journal_rank` 段配置；刊名在入库时统一清洗（HTML 实体、换行符）。

这一项**可选**：不配 `EASYSCHOLAR_SECRET_KEY`（见 `.env.example`）就整段跳过，一个请求
都不发，卡片上只是没有影响因子/分区标签，其余流程照常，不会报错。所以 `litradar check`
会把这一项单独列出来——缺密钥时给明确告警，而不是让你对着一个全绿的体检结果猜为什么
卡片上没有分区。密钥变量名可用 `journal_rank.api_key_env` 改名。

完整设计记录与实测数据见 [docs/litradar-design.md](docs/litradar-design.md)。

## 部署

`deploy/` 里的单元文件与 nginx 示例是 **Linux/systemd 专用**，其中的项目路径需要按你的
实际部署目录改写；macOS 与 Windows 请用下面各自的等价方案。三者的共同点是：
Web 服务只绑 `127.0.0.1:8090`，每日流水线在固定时间跑 `litradar run`
（时间窗来自 `config.yaml` 的 `app.pipeline_window_days`，不要在调度器里另设窗口）。

### Linux（systemd + nginx）

`deploy/` 提供 systemd 模板单元（`%i` 为运行用户，单元文件不含具体用户名）与 nginx
反代示例：

```bash
sudo cp deploy/litradar-web.service    /etc/systemd/system/litradar@.service
sudo cp deploy/litradar-daily@.service /etc/systemd/system/
sudo cp deploy/litradar-daily@.timer   /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now litradar@$(whoami).service      # Web 服务
sudo systemctl enable --now litradar-daily@$(whoami).timer  # 每日流水线

systemctl list-timers 'litradar*'
journalctl -u litradar@$(whoami).service -f
```

> 三个单元必须以模板名（带 `@`）安装。`litradar-daily@.service` 使用 `User=%i`，
> 以非模板名安装会使 `%i` 为空，systemd 拒绝启动。

应用默认仅绑定 `127.0.0.1:8090`；局域网访问建议经 nginx 反向代理
（见 `deploy/litradar-nginx.conf`）。

### macOS（launchd）

`~/Library/LaunchAgents/com.litradar.daily.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.litradar.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/你的用户名/LitRadar/.venv/bin/litradar</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/你的用户名/LitRadar</string>
  <key>StandardOutPath</key><string>/Users/你的用户名/LitRadar/data/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/你的用户名/LitRadar/data/launchd.err</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>5</integer></dict>
</dict></plist>
```

```bash
launchctl load  ~/Library/LaunchAgents/com.litradar.daily.plist
launchctl start com.litradar.daily          # 立刻手动跑一次
launchctl unload ~/Library/LaunchAgents/com.litradar.daily.plist
```

Web 服务同样可以用 launchd 常驻（把 `ProgramArguments` 换成
`.venv/bin/uvicorn`、`litradar.web.app:app`、`--host`、`127.0.0.1`、`--port`、`8090`，
并加 `<key>KeepAlive</key><true/>`）。macOS 的 `launchd` 在进程重载时会自动重启，
`WorkingDirectory` 必须写在项目根，否则 `config.yaml` 找不到。

### Windows（任务计划程序）

用 PowerShell 的 `*-ScheduledTask` cmdlet 注册比 `schtasks /TR` 少一层引号转义，
且能直接指定工作目录（**必须指定**，否则读不到 `config.yaml`）：

```powershell
# Web 服务:登录时启动,只绑回环地址
$web = New-ScheduledTaskAction -Execute "C:\LitRadar\.venv\Scripts\python.exe" `
  -Argument "-m uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090" `
  -WorkingDirectory "C:\LitRadar"
Register-ScheduledTask -TaskName "LitRadarWeb" -Action $web `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Limited

# 每日流水线:每天 07:05
$daily = New-ScheduledTaskAction -Execute "C:\LitRadar\.venv\Scripts\litradar.exe" `
  -Argument "run" -WorkingDirectory "C:\LitRadar"
Register-ScheduledTask -TaskName "LitRadarDaily" -Action $daily `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 07:05) -RunLevel Limited

Start-ScheduledTask      -TaskName "LitRadarDaily"   # 立刻跑一次
Get-ScheduledTaskInfo    -TaskName "LitRadarDaily"   # 上次结果 / 下次运行时间
Unregister-ScheduledTask -TaskName "LitRadarDaily" -Confirm:$false
```

不传 `-User` / `-Password` 时任务默认**只在当前用户登录后**才会运行；要完全无人
值守，需要补上凭据，或把 Web 服务注册成 Windows 服务（例如用 NSSM 包装
`.venv\Scripts\python.exe -m uvicorn ...`）。

关于 Windows 的两点实测注意：

- **控制台编码**：任务计划把输出重定向时，Python 会退回 ANSI 代码页（英文系统
  cp1252），中文日志会抛 `UnicodeEncodeError`。`litradar` 已在入口把标准输出/错误
  固定为 UTF-8 并把编码错误降级为替换字符；若你自己写包装脚本，建议同时设
  `PYTHONUTF8=1`。
- **单实例锁**：Windows 上用的是 `msvcrt` 字节范围锁，POSIX 上是 `fcntl.flock`，
  两者都由系统在进程结束时释放，所以任务被强杀不会留下需要手工清理的死锁。
  两个平台都以 `data/litradar.lock` 为同一把锁，Web 按钮与定时任务因此不会互相
  抢 API 限流。

局域网访问仍然建议在前面放一层反向代理（Windows 可用 Caddy/IIS，macOS 可用
Caddy/nginx），不要直接把服务绑到 `0.0.0.0`。

## 安全与合规

- 本项目**不抓取 X-MOL 网站**（其 `robots.txt` 禁止爬取检索页），仅解析用户自己
  收到的订阅邮件；X-MOL 站内订阅照常使用
- 定位为单用户自托管，无账号体系，默认仅绑定回环地址。如需绑定非回环地址，
  应设置 `LITRADAR_TOKEN` 接口口令（访问时带 `?k=<token>`），否则同网段任何人
  都能触发 `/admin/run/*` 消耗你的 LLM 额度
- 不建议将服务暴露于公网：订阅邮件内容面向订阅者本人，公网暴露构成对非授权用户的再分发
- 密钥仅通过环境变量传入（`.env`，POSIX 建议权限 600，Windows 用 ACL 限制），不写入配置文件；
  `.env`、`config.yaml`、`interests.yaml`、`data/` 均已被 `.gitignore` 排除

## 已知限制

| 限制 | 说明 |
|---|---|
| X-MOL 邮件仅含少量精选 | 订阅邮件为 teaser（通常每封 2 条），全量召回依赖检索与滚雪球 |
| Semantic Scholar 限流 | 约 1 req/s 且偶发 429；免费申请 API Key 可获独立配额（申请材料见 `docs/`） |
| Crossref 摘要覆盖不全 | ACS 系期刊常缺摘要，故以 Semantic Scholar 为摘要主力 |
| 数字核验非完备 | 可标记多数数字不一致，但不保证捕获全部幻觉 |
| 不含专利与预印本 | 现有数据源均不提供；数据模型已预留 `item.kind` 维度供将来扩展 |
| Windows 支持仅部分实机验证 | 已在真实 Windows 上验证：安装、`pip install -e .`、Web 服务正常启动；锁的 `msvcrt` 分支、任务计划程序、完整流水线尚未在实机跑过，只有模拟测试覆盖 |

## 目录结构

```
litradar/
├── litradar/
│   ├── cli.py                  命令行入口
│   ├── config.py               配置加载（密钥仅走环境变量）
│   ├── db.py                   SQLite schema 与读写
│   ├── http.py                 统一 UA、限流、重试
│   ├── lock.py                 流水线互斥锁
│   ├── normalize.py            DOI / 标题 / 日期 / 作者归一化
│   ├── sources/
│   │   ├── xmol_email.py       X-MOL 邮件解析（含回归测试）
│   │   ├── mail.py             邮件接入：folder / maildir / imap
│   │   ├── crossref_search.py  Crossref 检索与富化
│   │   ├── semanticscholar.py  S2 检索 / 摘要 / 滚雪球
│   │   ├── easyscholar.py      easyScholar 期刊等级客户端
│   │   └── openalex_search.py  OpenAlex 检索（可选）
│   ├── enrich.py               富化编排
│   ├── journal_rank.py         期刊等级缓存与标签压缩
│   ├── rank.py                 三阶段排序
│   ├── summarize.py            结构化中文摘要与数字核验
│   ├── llm.py                  DeepSeek 客户端
│   ├── pipeline.py             流水线编排
│   └── web/                    FastAPI + Jinja2（无前端构建）
├── config.example.yaml         运行配置示例
├── interests.example.yaml      研究画像示例
├── deploy/                     systemd 单元与 nginx 配置（Linux 专用）
├── docs/                       设计文档与 API Key 申请材料
├── fixtures/                   邮件解析回归样本（真实 .eml）
└── tests/                      单元与回归测试（pytest）
```

## 致谢

- [Crossref](https://www.crossref.org/)、[Semantic Scholar](https://www.semanticscholar.org/)、[OpenAlex](https://openalex.org/) 提供开放学术元数据
- [easyScholar](https://www.easyscholar.cc/) 提供期刊等级数据
- 排序与摘要由 [DeepSeek](https://www.deepseek.com/) 模型驱动

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
