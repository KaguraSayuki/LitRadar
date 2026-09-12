# LitRadar

个人文献雷达 —— 面向化学研究者的自托管文献追踪工具。聚合订阅邮件与开放学术 API，
使用 LLM 完成个性化排序与中文结构化摘要，在局域网 Web 界面中阅读与反馈。

> A self-hosted, single-user literature radar for chemists: multi-source ingestion,
> LLM re-ranking with explanations, and structured Chinese summaries.

## 功能特性

- **多源采集**：X-MOL 订阅邮件解析、Web of Science 提醒自动获取完整题录、Semantic Scholar 布尔检索、引用滚雪球、Crossref 检索（可选），所有来源按 DOI 去重合并
- **元数据富化**：Crossref 权威元数据、Semantic Scholar 摘要与引用数、easyScholar 期刊等级（影响因子 / 中科院分区 / 北核等）
- **三阶段排序**：规则过滤 → BM25 粗排 → DeepSeek listwise 精排，每篇输出分数与"为什么推荐"的中文理由
- **中文结构化摘要**：问题 / 方法 / 关键结果 / 局限 / 对研究的用处；关键数字与英文原文逐字核验，推断内容显式标注
- **反馈闭环**：收藏 / 已读 / 不感兴趣三态反馈分别落库，手动决定与自动规则解耦，并参与后续精排
- **轻量自托管**：FastAPI + SQLite + 原生前端（零构建），systemd 定时任务 + nginx 反向代理

## 工作原理

```
X-MOL 订阅邮件 (.eml / IMAP) ─┐
WoS 提醒邮件 → 完整题录导出 ──┤
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
| 检索 | Web of Science 搜索提醒 | 收信后自动打开提醒结果页，分批导出完整题录 | 需可用的 WoS 访问权限 |
| 检索 | Semantic Scholar `/paper/search/bulk` | 精确布尔查询，召回主力 | 免费（建议申请 Key） |
| 检索 | 引用滚雪球 `/paper/{id}/citations` | 沿种子文献的被引关系，发现关键词覆盖不到的工作 | 免费 |
| 检索 | Crossref 关键词检索 | 模糊匹配，召回高、噪声大，默认关闭 | 免费 |
| 富化 | Crossref | 期刊全称 / ISSN / 作者等权威元数据 | 免费 |
| 富化 | Semantic Scholar `/paper/batch` | 按 DOI 批量补摘要与引用数 | 免费 |
| 富化 | easyScholar 开放接口 | 影响因子、中科院分区、北核、CSCD 等期刊等级 | 按次计额，结果本地缓存 |
| 排序 / 摘要 | DeepSeek `deepseek-chat` | listwise 精排与结构化摘要 | 低 |
| 可选 | OpenAlex | 2026 年起为 API Key + 额度制，默认关闭 | 额度制 |

## 快速开始

环境要求：Python ≥ 3.11。

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
#    编辑 .env：至少填写 DEEPSEEK_API_KEY；邮件按下方密码或 OAuth2 方式配置

# 3. 初始化数据库
.venv/bin/litradar init-db

# 4. 运行完整流水线（采集 → 富化 → 排序 → 摘要）
.venv/bin/litradar run

# 5. 启动 Web 服务
.venv/bin/uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090
```

浏览器访问 `http://127.0.0.1:8090`。首次使用建议先运行 `litradar check`
体检配置、密钥、数据库与各 API 连通性。

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
Web of Science 搜索提醒会自动触发完整结果获取，邮件中的前 5 条预览不会被当成全部结果。
支持三种模式（`mail.mode`）：

- **`folder`（默认）**：将邮件导出为 `.eml` 放入 `data/inbox/`，处理后自动移至 `data/inbox/processed/`
- **`maildir`**：读取标准 Maildir 目录
- **`imap`**：直连收件箱，仅读取匹配 `imap_search` 的邮件

注意事项：

- Outlook 个人账号使用下方 OAuth2 配置直接收信；QQ / 163 等邮箱继续使用应用专用密码。
- 升级旧配置时，把 `mail.imap_search` 改为
  `OR FROM "newsletter.x-mol.com" FROM "alerts-noreply@clarivate.com"`。
  WoS 默认发件人为 `alerts-noreply@clarivate.com`；专用文件夹可只搜索该发件人。
- 推荐 `imap_mark_seen: false`，已读提醒也会采集，避免用户先看邮件造成遗漏。
  处理过的邮件靠 Message-ID 去重，同一 WoS 提醒的多次投递靠 alert ID 去重。
- 配置完成后用 `litradar mail-test` 做只读连通性测试：报告匹配邮件数并实际解析一封
  展示提取结果，不标记已读、不改动任何邮件
- X-MOL 邮件的原文、条目和完成标记在同一事务中写入，全部成功后才确认邮件；入库失败会
  回滚本封的写入，留待下次重试。WoS 使用下方持久队列。升级后的首次邮件采集会从数据库原文重放旧版未标记
  完成的邮件，即使邮件已移入 processed 或被 IMAP 标为已读，也能补齐遗漏条目

### WoS 自动采集

日常流程为：**定时收信 → 保存提醒任务 → 后台浏览器获取全部题录 → 数量校验 → 入库、补全与评分**。
RIS 由后台自动下载和解析，无需用户导出或上传文件。

安装可选运行依赖（在运行 LitRadar 的环境中）：

```bash
python -m pip install -e '.[wos,outlook]'
python -m playwright install chromium
# Linux 缺少浏览器系统库时，由管理员安装 Playwright 所需系统依赖：
# python -m playwright install-deps chromium
```

`config.example.yaml` 的 `wos` 段已给出全部配置；不使用 WoS 时可设 `wos.enabled: false`。
自动任务使用 `data/wos-browser/` 专用浏览器目录保存会话，需要机构访问时，在同一用户、
同一配置的桌面环境运行一次 `litradar wos-login`，完成常规登录后回到终端确认。
运行机器必须能通过机构网络或有效的机构会话访问 WoS；只有邮件订阅并不保证数据库访问权限。
无桌面的服务器需要先具备机构网络访问，或在有显示环境时初始化专用会话。
该命令不会关闭或接管用户日常浏览器，也不会自动解决验证码或跳过证书错误。

Outlook 直连的一次性设置：

1. 在 Microsoft Entra 注册自己的公共客户端应用。个人账号需支持个人 Microsoft 账户；
   启用公共客户端流，并添加 Office 365 Exchange Online 的委托权限
   `IMAP.AccessAsUser.All`。授权只用于收信，不需要 SMTP 或发信权限。
2. 在 `.env` 填 `OUTLOOK_CLIENT_ID`（应用的 Application/client ID）。在 `config.yaml` 设置：

   ```yaml
   mail:
     mode: imap
     imap_host: outlook.office365.com
     imap_port: 993
     imap_user: "你的 Outlook 邮箱"
     imap_auth: oauth2
     oauth_tenant: consumers
     oauth_client_id_env: OUTLOOK_CLIENT_ID
     oauth_token_cache: ./data/outlook-token-cache.json
     imap_folder: "Web of Science"   # 以实际 IMAP 文件夹名称为准
     imap_search: 'FROM "alerts-noreply@clarivate.com"'
     imap_mark_seen: false
   ```

3. 运行 `litradar mail-login`，按微软设备码提示完成一次授权，再运行 `litradar mail-test`
   验证文件夹和订阅匹配。后续由 MSAL 自动刷新令牌；授权被撤销时明确报错，不阻塞后台等待。

如果原先 X-MOL 在其他文件夹，可将两类订阅归入同一专用收信文件夹并使用上述 OR 搜索式。
当前配置对应一个 IMAP 账号和文件夹；示例中的文件夹不会自动覆盖用户邮箱规则。
OAuth 缓存文件权限为 `0600`，浏览器会话和下载缓存都属于私人运行数据，不要提交或共享。

日常运行继续使用原有 `litradar run` 和 `deploy/litradar-daily@.timer`。邮件原文及 WoS 任务
在同一事务持久化后才确认收信；只有该提醒的**全部题录**成功入库才标记处理完成。
最多每批导出 1000 条；邮件总数、页面总数、下载记录数必须一致，少导或重复标识都会失败。
原文、下载缓存和提醒成员关系保存在 SQLite 中；失败按退避时间重试，重启或邮件归档不丢任务。
退避时间表示最早重试时间，实际重试随下一轮定时采集执行。试满 `max_attempts`（默认 8）仍失败
的提醒会停在 `failed`，不再每天自动重试 —— 统计页和 `wos-status` 都会标出，修正原因后用
`wos-sync --retry-now` 手工重跑，避免一个坏任务无限重试并一直把 `litradar run` 的退出码拖成 1。

```bash
litradar ingest mail             # 收信并自动处理 WoS 完整结果
litradar wos-status              # 查看数量、完成状态、重试时间及错误
litradar wos-sync                # 只处理已入队的到期任务，邮箱离线也可执行
litradar wos-sync --retry-now     # 访问恢复后立即重试（含已失败的提醒）
```

> 在 systemd 下运行时，`deploy/` 的单元带 `NoNewPrivileges` / `PrivateDevices` /
> `ProtectSystem=strict`。Chromium 自带沙箱在这些限制下可能无法启动，表现为反复
> `无法启动 WoS Chromium`。首次启用后请用 `litradar wos-sync` 实测一次；若确认是加固
> 所致，可为该单元单独放宽（或改用带机构会话的桌面环境运行 `wos-login` 后再采集）。

网页统计页也会显示采集进度和需要重新登录的任务。`max_alerts_per_run` 控制每轮补采量；
超过 `max_records_per_alert` 的大提醒会明确报错，调大上限后可重试，绝不静默截断。
目前支持已验证模板的 WoS **搜索提醒**，不把引用提醒、作者提醒或营销邮件混入此解析器。
“新增检索记录”可能对应多年前发表的论文，排序始终使用题录发表日期，不能用收件日期替代。

开发时对一次实际导出的 32 条记录做了只读验证：32 条均有 DOI，30 条有摘要。
公开测试使用合成样本与浏览器替身；机构登录、网络条件及无人值守浏览器仍需在部署机器完成验证。

接口依据：[WoS 搜索提醒](https://webofscience.zendesk.com/hc/en-us/articles/20016493256721-Saved-Searches-and-Alerts)、
[微软 IMAP OAuth](https://learn.microsoft.com/zh-cn/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)、
[Playwright 下载](https://playwright.dev/python/docs/downloads)。

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

完整设计记录与实测数据见 [docs/litradar-design.md](docs/litradar-design.md)。

## 部署（systemd + nginx）

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

## 安全与合规

- 本项目**不抓取 X-MOL 网站**（其 `robots.txt` 禁止爬取检索页），仅解析用户自己
  收到的订阅邮件；X-MOL 站内订阅照常使用
- 定位为单用户自托管，无账号体系，默认仅绑定回环地址。如需绑定非回环地址，
  应设置 `LITRADAR_TOKEN` 接口口令（访问时带 `?k=<token>`），否则同网段任何人
  都能触发 `/admin/run/*` 消耗你的 LLM 额度
- 不建议将服务暴露于公网：订阅邮件内容面向订阅者本人，公网暴露构成对非授权用户的再分发
- 密钥仅通过环境变量传入（`.env`，建议权限 600），不写入配置文件；
  `.env`、`config.yaml`、`interests.yaml`、`data/` 均已被 `.gitignore` 排除

## 已知限制

| 限制 | 说明 |
|---|---|
| X-MOL 邮件仅含少量精选 | 订阅邮件为 teaser（通常每封 2 条），全量召回依赖检索与滚雪球 |
| Semantic Scholar 限流 | 约 1 req/s 且偶发 429；免费申请 API Key 可获独立配额（申请材料见 `docs/`） |
| Crossref 摘要覆盖不全 | ACS 系期刊常缺摘要，故以 Semantic Scholar 为摘要主力 |
| 数字核验非完备 | 可标记多数数字不一致，但不保证捕获全部幻觉 |
| 不含专利与预印本 | 现有数据源均不提供；数据模型已预留 `item.kind` 维度供将来扩展 |

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
├── deploy/                     systemd 单元与 nginx 配置
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
