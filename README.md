# LitRadar

LitRadar 是面向化学研究者的个人文献追踪工具。它汇集订阅邮件与学术 API 的文献信息，
按研究方向排序并生成中文摘要，提供自托管的 Web 阅读与反馈界面。

A self-hosted, single-user literature radar for chemists, with multi-source discovery,
personalized ranking, and structured Chinese summaries.

## 文档导航

| 文档 | 内容 |
|---|---|
| [网页设置指南](docs/web-settings.md) | 首次设置、服务接入、研究方向、预览、阅读偏好与自动更新 |
| [订阅组配置](docs/subscription-groups.md) | 添加研究方向、转换旧配置、分组操作与摘要复用 |
| [部署与维护](docs/deployment.md) | Linux、macOS、Windows 部署，以及访问保护和版本更新 |
| [实现架构](docs/architecture.md) | 模块职责、数据流、表结构与开发约定 |
| [设计记录](docs/litradar-design.md) | 主要技术选择、方案调整与尚未实现的功能 |
| [Semantic Scholar API Key 申请参考](docs/s2-api-key-application.md) | 与当前实现相符的英文申请草稿及用量估算方法 |
| [贡献约定](CONTRIBUTING.md) | 提交说明、验证范围与用户体验原则 |
| [网页设置方案与验收](docs/web-settings-plan.md) | 改造目标、已实现范围与部署边界 |

## 主要功能

- **多源采集**：解析 X-MOL 订阅邮件，使用 Semantic Scholar 检索文献、追踪种子文献的被引记录，并可接入 Crossref 和 OpenAlex 检索。
- **多订阅组**：为不同研究方向分别设置检索式、关键词、期刊与排序偏好。文献条目共享，分组保存分数、忽略状态与方向性说明。
- **元数据补全**：补充原文摘要、引用数、作者、期刊与开放获取链接；可选展示 easyScholar 提供的期刊等级。
- **个性化排序**：依次进行规则过滤、BM25 粗排和 LLM 精排，并展示推荐理由。LLM 精排可按组关闭。
- **中文摘要**：提供简要摘要与包含问题、方法、关键结果、局限的深度摘要，另按组生成“对研究的用处”。
- **阅读反馈**：支持收藏、已读、不感兴趣与撤销操作，后续精排可参考收藏和忽略记录。
- **网页设置**：通过表单管理研究方向、密钥、邮箱、查询条件和阅读偏好，支持连接测试、预览与配置恢复。
- **每日自动更新**：在网页选择时间、时区与方向；独立调度进程持久记录当天任务，避免重启后重复执行。
- **轻量部署**：采用 FastAPI、SQLite 和服务端模板，无需前端构建；支持命令行及定时运行。

## 工作流程与数据源

```text
订阅邮件 / 文献检索 / 被引追踪
              ↓
      DOI 去重与分组入库
              ↓
   补全摘要、引用数与期刊信息
              ↓
    规则过滤 → BM25 → LLM 精排
              ↓
      中文摘要 → 阅读与反馈
```

| 数据源 | 在本项目中的用途 | 启用条件 |
|---|---|---|
| X-MOL 订阅邮件 | 解析用户收到的文献推荐邮件 | 默认开启，可读取本地邮件或通过 IMAP 接入 |
| Semantic Scholar | 布尔检索、被引追踪、摘要及引用数补全 | 本项目的检索与被引追踪需要 `S2_API_KEY`；按 DOI 补全可尝试匿名访问 |
| Crossref | 补全期刊、ISSN、作者等元数据，也可进行关键词检索 | 元数据补全默认开启，关键词检索默认关闭 |
| easyScholar | 期刊等级、影响因子等展示信息 | 需要 `EASYSCHOLAR_SECRET_KEY`，结果按刊名缓存 |
| OpenAlex | 可选关键词检索 | 默认关闭，启用时需配置 `OPENALEX_API_KEY` |
| 自定义模型服务 | 精排、中文摘要与方向性说明 | 兼容 OpenAI Chat Completions 的文本 API；在网页填写地址、模型名和密钥，保留 DeepSeek 预设 |

本项目只解析 X-MOL 订阅邮件，不抓取其网站。各 API 的访问条件、额度与收费以服务提供方为准。

## 快速开始

需要 Python 3.11 或更高版本，支持 Linux、macOS 和 Windows。

### Linux / macOS

```bash
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar
python3 -m venv .venv
.venv/bin/python -m pip install -e .

.venv/bin/litradar init-db
.venv/bin/litradar setup-link --url http://127.0.0.1:8090
.venv/bin/litradar serve
```

### Windows（PowerShell）

```powershell
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .

.venv\Scripts\litradar.exe init-db
.venv\Scripts\litradar.exe setup-link --url http://127.0.0.1:8090
.venv\Scripts\litradar.exe serve
```

安装和启动可以交给代理完成。以上命令直接调用虚拟环境中的程序，无需先激活环境。
无需复制示例研究方向或编辑配置文件；首次保存会创建实际设置。

打开终端给出的一次性设置链接（30 分钟有效），设置访问密码后，依次在网页连接服务、
创建研究方向、预览检索结果并保存。没有模型密钥也能填写研究方向和筛选条件。
后续使用 [本机 Web 界面](http://127.0.0.1:8090) 登录。

自动更新需要部署代理先确认此实例没有其他系统定时任务，再执行
`litradar schedule-handoff --external-timers-stopped`。该命令只记录接管确认，之后由用户
在网页开启计划；已有实例的迁移步骤见 [部署与维护](docs/deployment.md)。

下文使用简写 `litradar`；
若未激活虚拟环境，请替换为 `.venv/bin/litradar` 或 `.venv\Scripts\litradar.exe`。

## 网页设置

导航中的“设置”包含研究方向、数据与邮箱、AI 与阅读、自动更新、访问与维护五页。
每页保存独立生效，不启动流水线；需要立即处理文献时，在研究方向卡片点击“更新这个方向”。
已有任务继续使用开始时的配置和凭据，新任务读取最新版本。

在“数据与邮箱 → 默认模型连接”填写 API 地址和模型名称，保存连接设置及密钥后，
点击“测试兼容性”验证评分和摘要的小样例。高级选项支持 JSON 输出方式、输出长度
参数及模型默认随机程度；旧 DeepSeek 配置和凭据仍可使用。
填写地址并保存密钥后，也可点击“获取模型”，从服务返回的列表中选择模型；不提供
列表接口的服务仍可手动填写。

密钥留空表示保持原值，清除使用独立按钮。网页不回传原始密钥；由部署环境传入的凭据
显示为部署管理，需维护代理调整。应用管理的密钥可以直接在网页更换，无需重启。

完整操作说明见 [网页设置指南](docs/web-settings.md)。下列文件保留作存储与兼容用途：

| 文件 | 用途 | 示例 |
|---|---|---|
| `.litradar-secrets.json` | 网页管理的凭据、密码哈希和首次设置状态，不包含在普通设置导出中 | 应用自动创建 |
| `.env` | 兼容旧部署的凭据来源，可不创建 | [.env.example](.env.example) |
| `config.yaml` | 数据源开关、运行窗口、排序参数与期刊等级展示 | [config.example.yaml](config.example.yaml) |
| `interests.yaml` | 研究方向、检索式、关键词、期刊、作者与种子文献 | [interests.example.yaml](interests.example.yaml) |

设置时需要注意以下几点：

- **运行窗口**：`app.pipeline_window_days` 默认 200 天，CLI、定时任务和网页按钮共用。
  建议不小于检索回溯窗口 `sources.s2_search_lookback_days`（默认 180 天），以便新采集
  的文献进入排序与摘要范围；临时运行可用 `--days` 覆盖。
- **检索条件**：填写全部包含、任意包含和排除的术语，程序生成相应查询。同一来源的
  多条查询取并集。旧复杂条件会保留，不能转换的部分可逐条重建并预览。
- **排序权重**：使用 `ranking.weights.llm/coarse/rule`。权重必须是非负有限数值，且
  总和大于零；兼容早期的 `w_llm/w_coarse/w_rule`，两种写法冲突时会报错。
- **备份与冲突**：普通设置保存前备份，分别保留最近 5 份；过期表单不能覆盖另一页面
  或本机程序的修改。维护页可以查看恢复范围和恢复备份。

### 多订阅组

在“设置 → 研究方向”点击“新增研究方向”，填写名称、关键词、期刊和检索条件。
空库或只有一个方向时也有新增入口；卡片支持复制、停用、恢复和调整顺序。

旧的单方向配置可以继续使用，网页保存时自动保留其 `default` 身份与历史记录。
改名不会改变方向标识，停用不会删除数据。完整说明与兼容配置示例见
[订阅组配置](docs/subscription-groups.md)。

## 邮件接入

在 X-MOL 网站开通订阅后，在“数据与邮箱”选择接入方式：

| 模式 | 行为 |
|---|---|
| `folder`（默认） | 读取 `data/inbox/` 中的 `.eml` 文件，成功处理后默认移入 `data/inbox/processed/` |
| `maildir` | 读取标准 Maildir 目录 |
| `imap` | 连接邮箱，按 `imap_search` 筛选邮件；默认只采集未读邮件，并在处理成功后标为已读 |

IMAP 客户端使用用户名和密码或授权码登录，当前未实现 OAuth2。若邮箱仅支持 OAuth2，
可将订阅邮件转发到支持 IMAP 授权码登录的邮箱。网页“测试已保存的连接”只检查登录、
文件夹和匹配数量，不修改邮箱；也可由维护代理运行 `litradar mail-test`，
查看匹配数量和最近最多 5 封邮件的解析结果；该命令不标记已读、不移动邮件、不写入数据库。

IMAP 连接会验证服务器证书和主机名，退出时不会清除已标记删除的邮件。
`imap_mark_seen: false` 时以只读方式选中邮箱，每轮仍需读取符合搜索条件的邮件并去重。

每封邮件的原文、解析条目与完成标记在同一事务中写入，提交成功后才确认邮件。
入库失败会回滚本封写入并留待重试。升级后的采集也会重放数据库中尚未标记完成的原文，
以补齐旧版处理不完整的记录。

`sources.xmol_enabled: false` 会关闭邮件采集及历史原文重放。显式运行 `mail-test`
或 `parse` 仍可用于诊断。

## 常用命令

| 命令 | 用途 |
|---|---|
| `litradar init-db` | 初始化数据库 |
| `litradar setup-link --url <实例地址>` | 本机签发一次性设置链接，30 分钟有效 |
| `litradar serve` | 启动网页及独立调度进程；支持 `--host`、`--port` |
| `litradar scheduler` | 只启动独立调度进程，供分开部署使用 |
| `litradar schedule-handoff --external-timers-stopped` | 停用外部定时入口后，记录应用接管确认；不会立即开启计划 |
| `litradar ingest all` | 采集邮件、检索文献并追踪被引记录；可用 `mail` 或 `search` 选择采集类型 |
| `litradar enrich` | 补全文献元数据与期刊等级 |
| `litradar rank` | 运行规则、BM25 与可选的 LLM 排序 |
| `litradar summarize` | 补齐缺失或原文已变化的摘要；`--force` 重做本轮范围内的摘要 |
| `litradar run` | 依次执行采集、补全、排序和摘要 |
| `litradar stats` | 查看全库统计 |
| `litradar mail-test` | 只读检查邮件接入 |
| `litradar admin-password` | 设置管理员密码；`--clear` 清除已设置的密码 |
| `litradar check` | 检查配置、密钥、数据库与外部服务连通性 |
| `litradar parse` | 解析邮件并输出结果，不入库 |

`ingest`、`rank`、`summarize`、`run` 支持 `--group <slug>`。该选项限定检索、排序与
摘要使用的组；邮件采集和元数据补全仍是全局阶段。网页“统计”页也可手动运行这些阶段，
显示阶段、处理数量和耗时；任务在后台执行，刷新或离开页面后仍可回来查看进度。
设置与受保护的运行操作共用一次 5 分钟的网页内密码验证，到期保留未保存的表单输入。

检索、排序或摘要中的某个组出现异常时，其余组仍可继续。上述命令在阶段汇总的
`errors` 非零时返回退出码 1；正常完成及配置性跳过返回 0。部分接口或 LLM 批次失败
可能只记录告警，因此排查缺失结果时还应查看输出和运行记录。

## 排序、摘要与反馈

默认排序权重为 `0.85 × LLM + 0.10 × BM25 + 0.05 × 规则`。规则层识别排除条件，
并根据关键词、期刊和关注作者等信息加分；BM25 决定候选顺序，默认不截断通过规则的
候选（`llm.rerank_top_k: 0`）。LLM 以批次评分并给出中文理由。

LLM 未启用或未配置密钥时使用关键词和规则分，界面会说明当前状态。已启用精排但条目
缺少 LLM 结果时，界面会标记；失败批次不会覆盖已成功的批次。

同一篇文献的中性摘要由各组共享，“对研究的用处”按组生成。关闭某组的 `llm_rank`
只停止该组的精排，摘要阶段仍可能产生调用费用。原文摘要补齐或变化后会刷新生成内容；
仅更新引用数不会触发重做。

关键结果中的数字会与原文摘要核对，无法匹配的数字标为待核对；模型推断须明确标注。
这些检查不能保证摘要完全准确，阅读时仍应参考原文。

收藏与已读状态全局共享，不感兴趣和规则排除按组保存。收藏条目不会被后续规则自动
排除；忽略的条目可恢复，规则排除的条目也可在对应视图中查看。

## 部署与维护

长期运行可使用 [部署与维护](docs/deployment.md) 中的 systemd、launchd 或任务计划
程序方案。Web 服务默认监听 `127.0.0.1:8090`；局域网访问可通过反向代理提供 HTTPS，
并设置访问密码。接口口令可供已有自动化脚本使用。

`.env`、`.litradar-secrets.json`、`config.yaml`、`interests.yaml` 和 `data/` 均已排除在版本控制之外。
它们包含本机配置或个人数据，需要单独备份。项目面向个人使用，没有多用户账号与权限体系。

## 限制与验证

- 订阅邮件只覆盖部分文献；检索结果也受来源收录范围、摘要可用性和 API 限流影响。
- 当前没有专利采集或专利分析功能，也没有专门的预印本订阅源；学术 API 的返回结果仍需结合研究偏好筛选。
- 自动摘要不能替代原文阅读；数字核验无法识别所有内容错误。
- Windows 已验证安装与 Web 启动；任务计划、完整流水线和锁的 Windows 分支仍主要依赖模拟测试。

开发测试使用临时数据库及模拟网络、邮件和 LLM，不需要真实 API 密钥：

```bash
python -m pip install pytest
python -m pytest tests/ -q
```

安装 Node.js 18 或更高版本后，pytest 还会运行前端反馈、撤销与重试脚本的回归用例；
未安装时跳过相应脚本用例。

## 致谢与许可证

感谢 [Crossref](https://www.crossref.org/)、[Semantic Scholar](https://www.semanticscholar.org/)、
[OpenAlex](https://openalex.org/) 和 [easyScholar](https://www.easyscholar.cc/) 提供数据服务，
以及 [DeepSeek](https://www.deepseek.com/) 提供模型服务。

本项目基于 [MIT License](LICENSE) 开源。
