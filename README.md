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
#    编辑 .env：至少填写 DEEPSEEK_API_KEY；邮件接入需 IMAP_PASSWORD

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
- 检索词与偏好可在 Web 界面 `/interests` 直接编辑，保存前做 YAML 结构校验并自动备份原文件

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

## 命令行

```bash
litradar init-db        # 初始化数据库
litradar ingest all     # 采集：邮件 + 检索 + 滚雪球
litradar enrich         # 富化：补摘要、引用数、期刊等级
litradar rank           # 三阶段排序
litradar summarize      # 生成中文摘要（--force 全量重做，默认只补缺失）
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

**引用滚雪球：增量累积 + 共被引标注。** 以 `interests.yaml` 中 `seed_dois` 为种子，
沿"引用了种子的论文"方向发现换了说法、关键词覆盖不到的新工作。每轮只刷新最久未查的
少量种子以规避限流，引用关系持久化于 `seed_cite` 表，共被引数跨全部种子与历史轮次累积；
共被引作为质量标注展示（`滚雪球 ×2`），默认不作为准入门槛。种子宜选被引仍活跃的文献，
过新的论文被引数不足，滚不出结果。

**摘要防幻觉。** 摘要中 `key_results` 出现的每个数字与英文原文逐字比对，未命中者标注
⚠️ 提示核对；允许模型合理推断（如原文未明说的研究动机），但推断内容显式标注"（推断）"，
与原文事实区分。

**反馈三态分流。** 收藏免疫后续规则过滤——之后收紧检索词也不会移除明确的手动决定；
不感兴趣移入独立页签、可逐条恢复；被规则否决的条目以"已否决"状态可见而非静默消失。
任何条目都能追溯"为什么在 / 不在这个列表里"。

**期刊等级本地缓存。** easyScholar 按刊名查询且按次计额，结果缓存于 `journal_rank` 表，
全库期刊查询一遍后不再消耗额度。展示字段与标签压缩规则（`化学1区` → `化1`）在
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
└── tests/                      单元测试（pytest，80 个）
```

## 致谢

- [Crossref](https://www.crossref.org/)、[Semantic Scholar](https://www.semanticscholar.org/)、[OpenAlex](https://openalex.org/) 提供开放学术元数据
- [easyScholar](https://www.easyscholar.cc/) 提供期刊等级数据
- 排序与摘要由 [DeepSeek](https://www.deepseek.com/) 模型驱动
