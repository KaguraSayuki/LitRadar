# LitRadar

LitRadar 是面向化学研究者的个人文献追踪工具。它汇集订阅邮件与学术 API 的文献信息，
按研究方向排序并生成中文摘要，提供自托管的 Web 阅读与反馈界面。

A self-hosted, single-user literature radar for chemists, with multi-source discovery,
personalized ranking, and structured Chinese summaries.

## 文档导航

| 文档 | 内容 |
|---|---|
| [订阅组配置](docs/subscription-groups.md) | 添加研究方向、转换旧配置、分组操作与摘要复用 |
| [部署与维护](docs/deployment.md) | Linux、macOS、Windows 部署，以及访问保护和版本更新 |
| [实现架构](docs/architecture.md) | 模块职责、数据流、表结构与开发约定 |
| [设计记录](docs/litradar-design.md) | 主要技术选择、方案调整与尚未实现的功能 |
| [Semantic Scholar API Key 申请参考](docs/s2-api-key-application.md) | 与当前实现相符的英文申请草稿及用量估算方法 |

## 主要功能

- **多源采集**：解析 X-MOL 订阅邮件，使用 Semantic Scholar 检索文献、追踪种子文献的被引记录，并可接入 Crossref 和 OpenAlex 检索。
- **多订阅组**：为不同研究方向分别设置检索式、关键词、期刊与排序偏好。文献条目共享，分组保存分数、忽略状态与方向性说明。
- **元数据补全**：补充原文摘要、引用数、作者、期刊与开放获取链接；可选展示 easyScholar 提供的期刊等级。
- **个性化排序**：依次进行规则过滤、BM25 粗排和 LLM 精排，并展示推荐理由。LLM 精排可按组关闭。
- **中文摘要**：提供简要摘要与包含问题、方法、关键结果、局限的深度摘要，另按组生成“对研究的用处”。
- **阅读反馈**：支持收藏、已读、不感兴趣与撤销操作，后续精排可参考收藏和忽略记录。
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
| DeepSeek | 精排、中文摘要与方向性说明 | 需要 `DEEPSEEK_API_KEY`，调用费用取决于模型和用量 |

本项目只解析 X-MOL 订阅邮件，不抓取其网站。各 API 的访问条件、额度与收费以服务提供方为准。

## 快速开始

需要 Python 3.11 或更高版本，支持 Linux、macOS 和 Windows。

### Linux / macOS

```bash
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar
python3 -m venv .venv
.venv/bin/python -m pip install -e .

cp config.example.yaml config.yaml
cp interests.example.yaml interests.yaml
cp .env.example .env
chmod 600 .env
```

按下方“配置说明”填写密钥、检索式和研究偏好，然后运行：

```bash
.venv/bin/litradar init-db
.venv/bin/litradar check
.venv/bin/litradar run
.venv/bin/python -m uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090
```

### Windows（PowerShell）

```powershell
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .

Copy-Item config.example.yaml config.yaml
Copy-Item interests.example.yaml interests.yaml
Copy-Item .env.example .env
```

完成配置后运行：

```powershell
.venv\Scripts\litradar.exe init-db
.venv\Scripts\litradar.exe check
.venv\Scripts\litradar.exe run
.venv\Scripts\python.exe -m uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090
```

以上命令直接调用虚拟环境中的程序，无需先激活环境。Windows 上可通过文件属性中的
“安全”页限制 `.env` 的访问权限。

启动后访问 [本机 Web 界面](http://127.0.0.1:8090)。下文使用简写 `litradar`；
若未激活虚拟环境，请替换为 `.venv/bin/litradar` 或 `.venv\Scripts\litradar.exe`。

## 配置说明

| 文件 | 用途 | 示例 |
|---|---|---|
| `.env` | API 密钥、IMAP 授权码、接口口令与管理员密码哈希 | [.env.example](.env.example) |
| `config.yaml` | 数据源开关、运行窗口、排序参数与期刊等级展示 | [config.example.yaml](config.example.yaml) |
| `interests.yaml` | 研究方向、检索式、关键词、期刊、作者与种子文献 | [interests.example.yaml](interests.example.yaml) |

首次使用时，将示例中的占位内容替换为自己的研究方向，并检查示例期刊、排除词和种子
文献是否适用。使用默认的 Semantic Scholar 检索需要填写 `S2_API_KEY`；使用 LLM
精排与摘要需要填写 `DEEPSEEK_API_KEY`。IMAP 和期刊等级密钥仅在使用对应功能时填写。

配置时需要注意以下几点：

- **运行窗口**：`app.pipeline_window_days` 默认 200 天，CLI、定时任务和网页按钮共用。
  建议不小于检索回溯窗口 `sources.s2_search_lookback_days`（默认 180 天），以便新采集
  的文献进入排序与摘要范围；临时运行可用 `--days` 覆盖。
- **检索式**：`search_queries` 用于 Crossref 和 OpenAlex；`s2_queries` 用于 Semantic
  Scholar，使用 `+`、`|`、双引号和 `-` 表达与、或、短语及排除条件。两类检索式分别配置，
  同一来源的多条查询取并集。
- **排序权重**：使用 `ranking.weights.llm/coarse/rule`。权重必须是非负有限数值，且
  总和大于零；兼容早期的 `w_llm/w_coarse/w_rule`，两种写法冲突时会报错。
- **在线编辑**：网页“检索词与偏好”（`/interests`）可编辑整份 `interests.yaml`。
  保存时校验格式并备份原文件，保留最近 5 份备份。

### 多订阅组

添加分组的入口也是“检索词与偏好”：将配置改为 `groups:` 列表，为每个方向填写一组
设置。当前界面通过 YAML 编辑分组，没有单独的“新增分组”按钮；配置多个可用组后，
页面顶部会显示切换器。

旧的单方向配置可以继续使用，对应的固定标识是 `default`。从旧配置转换时，保留原组
的 `slug: default`，即可继续访问原有成员、分数与反馈。完整示例及操作步骤见
[订阅组配置](docs/subscription-groups.md)。

## 邮件接入

在 X-MOL 网站开通订阅后，可通过 `mail.mode` 选择接入方式：

| 模式 | 行为 |
|---|---|
| `folder`（默认） | 读取 `data/inbox/` 中的 `.eml` 文件，成功处理后默认移入 `data/inbox/processed/` |
| `maildir` | 读取标准 Maildir 目录 |
| `imap` | 连接邮箱，按 `imap_search` 筛选邮件；默认只采集未读邮件，并在处理成功后标为已读 |

IMAP 客户端使用用户名和密码或授权码登录，当前未实现 OAuth2。若邮箱仅支持 OAuth2，
可将订阅邮件转发到支持 IMAP 授权码登录的邮箱。配置后运行 `litradar mail-test`，
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
摘要使用的组；邮件采集和元数据补全仍是全局阶段。网页“统计”页也可手动运行这些阶段。

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
并设置接口口令与管理员密码。

`.env`、`config.yaml`、`interests.yaml` 和 `data/` 均已排除在版本控制之外。
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
