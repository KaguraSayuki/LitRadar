# LitRadar 技术方案文档

> 个人化学文献雷达 —— 基于 SciFinder-n KMP 提醒 + 开放 API 富化 + DeepSeek 精排,局域网 Web 界面自用。
>
> 版本 v0.1 · 单用户 · 自托管

---

## ⚠️ 修订记录:v0.1 → 实现版(2026-09)

本文档最初按 **SciFinder-n KMP 邮件** 为主线设计。实际开发中方案发生了几处
重要变更,**代码以变更后的方案为准**(见 `README.md`)。原文保留作为设计推演记录。

| # | 原方案 | 实际情况 | 变更后 |
|---|---|---|---|
| 1 | SciFinder-n KMP 提醒邮件 | 用户明确不用 SciFinder | **移除**。改为 X-MOL 订阅邮件 |
| 2 | 邮件解析杂糅 KMP 模板 | X-MOL 邮件模板完全不同 | 解析器按真实 `.eml` 样本重写,并有回归测试 |
| 3 | OpenAlex 为免费主力 | **2026 年起改为 API Key + 额度制**,未配 key 返回 `Insufficient budget` | 降级为**可选**(需 `OPENALEX_API_KEY`) |
| 4 | Crossref 补摘要 | ACS 系期刊常常没有摘要 | 引入 **Semantic Scholar** 作摘要主力(实测 ACS 4/4 覆盖,免费) |
| 5 | 固定检索式(saved search) | 用户要**关键词查询** | 画像新增 `search_query` 字段,直接驱动 Crossref 检索 |
| 6 | 推送邮件周报 | 用户要局域网网页 | 全部改为 Web 界面,无邮件推送 |
| 7 | HTMX | 为减少依赖 | 改为**原生 fetch**,零前端依赖 |
| 8 | 专利监控(SciFinder) | SciFinder 移除后无专利源 | **已砍掉**。所有现有数据源都不含专利,`/patents` 页面与导航已移除;要接需先解决专利族去重与 18 个月公开延迟 |
| 9 | `auth.enabled` + `password_hash`(用 `litradar hash-password` 生成)+ `/login` 页面 | 未实现账号体系 | 改为 `LITRADAR_TOKEN` 接口口令(访问带 `?k=`);默认只绑回环地址,不设口令也安全。**`hash-password` 命令不存在** |
| 10 | 排序拆成 `rules.py` / `coarse.py` / `llm_rerank.py`,另有 `notify.py` 提醒与 `epo.py` EPO OPS 客户端 | 三阶段合并在单个 `rank.py`;提醒与 EPO 均未实现 | `openalex.py` / `crossref.py` 实际是 `sources/openalex_search.py`、`sources/crossref_search.py` |
| 11 | 画像存 `profiles/default.yaml` | 改为项目根的 `interests.yaml`,网页可在线编辑并自动留备份 | |
| 12 | `scripts/seed_demo.py` 灌 mock 数据 | **从未创建** | 测试改用 pytest fixture 与真实 `.eml` 样本 |
| 13 | `docs/COMPLIANCE.md` 记录合规边界 | **未创建** | 边界写在 README「安全与合规」一节 |
| 14 | `app.host: "0.0.0.0"` + `port: 8080`;`.env` 含 `EPO_KEY` / `EPO_SECRET` | 默认只绑 `127.0.0.1:8090`;EPO 未接入 | 绑 `0.0.0.0` 会绕过 nginx 的 TLS 与口令保护,**勿照抄**;实际密钥清单见 `.env.example` |

保留不变的核心设计:三阶段排序漏斗(规则 → BM25 → LLM)、SQLite 数据模型、
期刊缩写归一匹配、数字核验防幻觉、systemd 调度、单用户 LAN 部署。

**实测验证的关键结论**(可直接采信):
- Crossref 多值 ISSN 过滤必须**重复写过滤器名**(`issn:A,issn:B`),`issn:A,B` 会报错
- Outlook 个人账号 IMAP 返回 `AUTH=XOAUTH2 LOGINDISABLED`,密码登录不可用
- X-MOL `robots.txt` 禁止爬取 `/paper/search`,全站有阿里云人机验证
- X-MOL 邮件只含 2 条精选(teaser),不是全量结果集

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 |
|---|---|
| G1 | 自动汇集**本人 SciFinder 授权范围内**的新文献与专利,无需手工检索 |
| G2 | 用 LLM 做个性化精排 + 中文结构化摘要,输出"为什么推给你"的理由 |
| G3 | 局域网 Web 页面提供未读收件箱、本期精选、专利视图、全文检索 |
| G4 | 反馈(收藏/已读/忽略)可沉淀,用于持续改进排序质量 |
| G5 | 全流程合规:不爬 SciFinder UI、不对外分发、不批量导出 |

### 1.2 非目标(明确不做)

- ❌ 多用户 / 团队共享 / 权限体系(单用户,JSON 里留字段但不实现)
- ❌ 移动端 App、原生客户端
- ❌ 公网暴露、域名、HTTPS 证书(仅局域网)
- ❌ 全文文本挖掘(TDM)、PDF 批量下载与解析
- ❌ 向非授权用户分发任何 SciFinder 派生内容
- ❌ 预印本(ChemRxiv / arXiv / bioRxiv)—— 明确不纳入

---

## 2. 约束与合规

### 2.1 硬约束

| 约束 | 说明 | 设计应对 |
|---|---|---|
| SciFinder-n 无第三方公开 API | `scifinder-n.cas.org/api/docs/v1/` 仅为其前端内部接口,登录态调用违反授权 | **绝不调用**。只用官方 KMP 邮件提醒作为出口 |
| 授权禁止向非授权用户分发 | 转载、公开群、公众号均违规 | 仅局域网、加口令、不公网暴露 |
| 授权禁止系统性下载 | 批量导出、爬取会连带封禁机构 IP | 不批量导出;库内只存题录 + 链接 + 自生成摘要 |
| 预印本不纳入 | 用户明确不看 | 数据源白名单里显式排除 |

### 2.2 合规检查清单(上线前逐条确认)

- [ ] 服务绑定 `0.0.0.0` 但路由器未做端口转发,公网不可达
- [ ] 已启用接口口令(`LITRADAR_TOKEN`,访问带 `?k=`;原稿写的 `auth.enabled` 未实现)
- [ ] 未实现任何形态的批量导出功能
- [ ] 未存储 SciFinder 原始摘要全文,仅存题录 + 自生成摘要 + 跳转链接
- [ ] 合规边界已在 README「安全与合规」写明(原计划的 `docs/COMPLIANCE.md` 未创建)

### 2.3 待与图书馆确认的三件事

> 这三件事不影响一期开工,但可能显著改变架构,建议并行去问。

1. 是否有**机构级 CAS API 或 TDM 授权**?(有则采集层可大幅简化)
2. KMP 提醒能否配置**推送到指定邮箱**(而非仅个人注册邮箱)?
3. 组内/个人自建工具消费 KMP 邮件的合规边界?

---

## 3. 总体架构

### 3.1 数据流

```
┌─────────────────── 采集层 (Ingest) ───────────────────┐
│                                                        │
│  ① SciFinder KMP 邮件  ← IMAP 轮询   [文献 + 专利]     │
│  ② 期刊 TOC RSS / Crossref            [文献兜底]       │
│  ③ 手动导入 (JSON / BibTeX)           [补齐历史]       │
│                                                        │
└────────────────────────┬───────────────────────────────┘
                         ↓
              归一化 + 去重 (Normalize)
              文献 → dedup_key = doi:<doi>
              专利 → dedup_key = family:<family_id>
                         ↓
                   ┌─────────────┐
                   │  item 池    │  SQLite
                   └──────┬──────┘
                          ↓
              富化 (Enrich)
              Crossref / OpenAlex → 摘要、引用数、OA 链接
              EPO OPS            → 专利族、法律状态
                          ↓
              排序 (Rank) —— 三阶段漏斗
              Stage 1 规则过滤   →  ~100 条
              Stage 2 BM25 粗排  →  top 40
              Stage 3 DeepSeek   →  打分 + 理由(分批 listwise)
                          ↓
              摘要 (Summarize)
              top N 深度摘要 / 其余一句话摘要
                          ↓
                   ┌─────────────┐
                   │   Web UI    │  FastAPI + HTMX
                   │  局域网访问  │
                   └──────┬──────┘
                          ↓
              反馈 (Feedback) ⭐ ✅ ❌
                          ↓
                  回流:调权重 / few-shot 示例
```

### 3.2 调度

```
systemd timer
  ├─ litradar-daily.timer    每天 07:00  ingest → enrich → rank → summarize
  └─ litradar-weekly.timer   每周一 08:00  生成"本期精选"视图(可选手机提醒)
```

单次运行失败不影响下次;每阶段写 `run_log`,可在 Web 上查看。

### 3.3 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 存储 | **SQLite** | 单用户、数据量小(每天数十至数百条)、零运维 |
| 前端 | **服务端渲染 + HTMX** | 零构建步骤,局域网秒开,不值得为一个自用工具养 npm 链 |
| 粗排 | **BM25(rank_bm25)** 而非向量 | DeepSeek 不提供 embedding API;引入本地 torch 模型对单用户过重。BM25 对 200 条/天的规模完全够用,且零依赖 |
| 精排 | **DeepSeek `deepseek-chat`**,分批 listwise | 64k 上下文、支持 JSON 输出、成本可忽略 |
| 推送 | **Web 为主,手机提醒可选** | 有网页后邮件 digest 冗余 |
| 调度 | **systemd timer** | 比 cron 日志/依赖管理更好;必须跑在机构网络内,故不用 GitHub Actions |

> **关于 embedding 的取舍**:如果后续发现 BM25 召回不够,可在 `rank/coarse.py` 里增加一个基于 API 的 embedding provider(如 SiliconFlow / 本地 bge-m3),接口已预留。

---

## 4. 数据模型

### 4.1 SQLite Schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── 原始邮件:保留以便解析器升级后重跑 ──────────────────
CREATE TABLE raw_email (
    id              INTEGER PRIMARY KEY,
    message_id      TEXT UNIQUE,
    received_at     TEXT,
    subject         TEXT,
    raw             BLOB NOT NULL,      -- 原始 MIME
    parsed_at       TEXT,
    parse_version   INTEGER DEFAULT 0   -- 解析器版本,用于触发重解析
);

-- ── 条目池:文献与专利共表,用 kind 区分 ─────────────────
CREATE TABLE item (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('paper','patent')),
    dedup_key         TEXT NOT NULL UNIQUE,   -- 'doi:10.xxxx' | 'family:01234567'
    doi               TEXT,
    title             TEXT NOT NULL,
    title_norm        TEXT NOT NULL,          -- 归一化标题,模糊匹配兜底
    abstract          TEXT,
    authors           TEXT,                   -- JSON array
    journal           TEXT,
    issn              TEXT,
    published_at      TEXT,                   -- 在线/公开日 ISO8601
    -- 专利专属
    priority_date     TEXT,
    publication_number TEXT,
    patent_family_id  TEXT,
    assignees         TEXT,                   -- JSON array
    claim_1           TEXT,                   -- 可能为 NULL,见 §6.3
    legal_status      TEXT,
    -- SciFinder 派生(合规:属于授权内容,不外传)
    cas_rn            TEXT,                   -- JSON array
    indexed_terms     TEXT,                   -- JSON array,CAplus 标引词
    -- 来源追踪
    source            TEXT NOT NULL,          -- 'kmp'|'crossref'|'rss'|'manual'
    source_ref        TEXT,                   -- raw_email.id 或 URL
    url               TEXT,                   -- 原文链接
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX idx_item_kind_pub ON item(kind, published_at DESC);
CREATE INDEX idx_item_title_norm ON item(title_norm);

-- ── 富化数据:一个 item 一行,避免 item 表列爆炸 ──────────
CREATE TABLE item_enrichment (
    item_id         INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    cited_by_count  INTEGER,
    is_oa           INTEGER,
    oa_url          TEXT,
    crossref_json   TEXT,
    openalex_json   TEXT,
    epo_json        TEXT,
    enriched_at     TEXT
);

-- ── 打分:保留 profile 列,为将来可能的扩展留门 ──────────
CREATE TABLE score (
    item_id       INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    profile       TEXT NOT NULL DEFAULT 'default',
    rule_score    REAL,
    coarse_score  REAL,
    llm_score     REAL,          -- 0-100
    llm_reason    TEXT,          -- "为什么推给你"
    llm_model     TEXT,
    ranked_at     TEXT,
    PRIMARY KEY (item_id, profile)
);

-- ── AI 摘要 ────────────────────────────────────────────
CREATE TABLE summary (
    item_id       INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    one_liner     TEXT,          -- 一句话结论
    problem       TEXT,          -- 解决什么问题
    method        TEXT,          -- 关键方法
    key_results   TEXT,          -- 关键数据
    limitation    TEXT,          -- 局限
    relevance     TEXT,          -- 对我们哪个课题有用
    depth         TEXT CHECK (depth IN ('short','deep')),
    model         TEXT,
    created_at    TEXT
);

-- ── 阅读状态 ───────────────────────────────────────────
CREATE TABLE item_state (
    item_id      INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'new',   -- 'new'|'read'|'archived'
    starred      INTEGER NOT NULL DEFAULT 0,
    ignored      INTEGER NOT NULL DEFAULT 0,
    notified_at  TEXT
);

-- ── 反馈:一行一个动作,便于统计 ────────────────────────
CREATE TABLE feedback (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('star','read','ignore','unstar')),
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_feedback_item ON feedback(item_id);

-- ── 运行日志 ───────────────────────────────────────────
CREATE TABLE run_log (
    id           INTEGER PRIMARY KEY,
    stage        TEXT NOT NULL,       -- 'ingest'|'enrich'|'rank'|'summarize'
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,                -- 'ok'|'partial'|'failed'
    stats        TEXT,                -- JSON,如 {"new":42,"skipped":8}
    error        TEXT
);
```

### 4.2 去重策略

| 类型 | 主键 | 兜底 |
|---|---|---|
| 文献 | `doi:<小写DOI>` | DOI 缺失时 → `title:<normalize(title)>` + 年份;入库前与已有 `title_norm` 做相似度 ≥ 0.92 的模糊匹配 |
| 专利 | `family:<patent_family_id>`(来自 EPO OPS) | OPS 不可用 → `pub:<国别+公开号>` 去掉版本后缀 |

`normalize(title)`:小写 → 去标点 → 折叠空白 → 去 HTML 实体。

---

## 5. 模块设计

### 5.1 `sources/kmp_email.py` —— KMP 邮件解析(核心)

**职责**:IMAP 拉取 KMP 提醒邮件 → 抽取题录 → 写库。

```python
@dataclass
class KmpMessage:
    message_id: str
    received_at: datetime
    subject: str
    html: str | None
    plain: str | None
    raw: bytes

def fetch_unseen(cfg) -> Iterator[KmpMessage]: ...
def parse(msg: KmpMessage) -> list[ParsedRecord]: ...
def persist(records, msg) -> IngestStats: ...
```

**实现要点**

1. **先存原始邮件,再解析。** `raw_email.raw` 存完整 MIME,`parse_version` 记录解析器版本。KMP 邮件模板会变,格式一改只需 bump 版本号重跑,不丢数据。
2. **解析容错优先级**:HTML 表格 → HTML 纯文本 → `text/plain`。三条路都失败时,至少提取所有 DOI 正则(`10\.\d{4,9}/[-._;()/:A-Z0-9]+`)和 URL,保证有最小可用结果。
3. **KMP 邮件通常只给题录**(标题、作者、来源、日期),**摘要一般没有** —— 这正是富化层的职责:解析出 DOI 后走 Crossref 补摘要。
4. **无 DOI 时的匹配**:用 `标题 + 第一作者 + 年份` 查 Crossref `query.bibliographic`,取相似度最高且 ≥ 0.90 的结果;低于阈值则标记 `needs_review`,不自动富化,在 Web 上以"待确认"样式显示。
5. **幂等**:以 `message_id` 唯一约束防重复处理;IMAP 标记 `\Seen` 只在成功入库后执行。

> ⚠️ **待验证**:KMP 邮件的确切模板需拿到真实样本后校准。建议先用 1–2 封真实邮件存到 `fixtures/` 作为回归测试基准。

### 5.2 `sources/crossref.py` / `sources/rss.py` —— 兜底源

- **Crossref**:按 ISSN 白名单增量拉取
  ```
  GET https://api.crossref.org/works
      ?filter=issn:{ISSN},from-created-date:{YYYY-MM-DD}
      &rows=100&select=DOI,title,author,container-title,abstract,issued,URL
      &mailto={你的邮箱}      # 加入 polite pool,限流更宽松
  ```
  所有化学相关期刊都有 ISSN 白名单,数量可控(建议 20–50 本)。
- **期刊 TOC RSS**:ACS / RSC / Wiley / Nature / Science 原生支持,作为 Crossref 的补充。用 `feedparser`。

### 5.3 `enrich/` —— 富化

| 模块 | 输入 | 输出 | 备注 |
|---|---|---|---|
| `crossref.py` | DOI | 摘要、期刊、作者 | 摘要常为 JATS XML,需清洗 |
| `openalex.py` | DOI | 引用数、OA 链接、主题标签 | `api.openalex.org/works/doi:{doi}` |
| `epo.py` | 公开号 | 专利族 ID、法律状态、引用 | **OAuth2 需注册** [developers.epo.org](https://developers.epo.org/) |

**限流**:Crossref 1 req/s、OpenAlex 10 req/s、EPO OPS 免费档有配额。统一在 `enrich/http.py` 里做令牌桶 + 指数退避 + 本地缓存(同一 DOI 7 天内不重复请求)。

### 5.4 `rank/` —— 三阶段漏斗

**Stage 1 规则过滤** (`rules.py`)

```python
def rule_filter(items, profile) -> list[Item]:
    # 命中 negative 词 → 直接丢弃(不进入后续,省成本)
    # 命中 journals_core / keywords_core / cas_rn / assignees_watch → 加分
    # 返回全部未被否定项,附带 rule_score
```

**Stage 2 BM25 粗排** (`coarse.py`)

- 语料 = 每个 item 的 `title + abstract + indexed_terms + cas_rn`
- 查询 = profile 的 `keywords_core + keywords_bonus + direction`(拼接)
- 取 top 40 进入 Stage 3
- `journals_core` 命中项**保送**,不参与裁剪

**Stage 3 LLM 精排** (`llm_rerank.py`)

- 分批 listwise,`rerank_batch_size = 20`
- 输出 `llm_score` (0–100) + `llm_reason`(≤30 字中文)
- 失败重试 2 次,仍失败则该批降级为 `coarse_score` 映射值,不阻塞流程

**排序终值**:`final = 0.6 * llm_score + 0.25 * coarse_score_norm + 0.15 * rule_score_norm`
(权重放 `config.yaml`,方便调)

### 5.5 `summarize.py` —— 摘要生成

- **深度摘要**:`final` 排名前 `deep_summary_top_n`(默认 8)条
- **一句话摘要**:其余条目,批量生成(一次 10 条),成本更低
- 文献与专利使用**两套不同 prompt**(见 §7)

### 5.6 `web/` —— Web 界面

| 方法 | 路由 | 说明 |
|---|---|---|
| GET | `/` | 雷达页:仍在考虑范围内的条目,按 `final` 降序 |
| GET | `/week` | 本周新增,3 篇必读 + 其余折叠 |
| GET | `/patents` | 专利视图,按专利族聚合 |
| GET | `/search?q=` | 全历史池检索(SQLite FTS5) |
| GET | `/item/{id}` | 详情页 |
| POST | `/item/{id}/action` | HTMX 局部更新:`star` / `read` / `ignore` / `unstar` |
| GET/POST | `/profile` | 画像在线编辑,保存即写入 `profiles/default.yaml` |
| GET | `/stats` | 来源/期刊命中率、反馈统计 |
| POST | `/admin/run/{stage}` | 手动触发单阶段(调试用) |
| GET | `/healthz` | 健康检查 |
| GET | `/login` · POST | 口令认证(若 `auth.enabled`) |

**页面要点**

- **相关度理由**必须显著展示 —— 这是你判断"画像该加什么词"的主要依据
- 三个按钮 `⭐ ✅ ❌` 走 HTMX,无整页刷新
- 详情页展示:`abstract` / `llm_reason` / `summary` / `cas_rn` / `indexed_terms` / 原文链接 / 富化元数据
- 专利详情页额外展示:权利要求1(若获取到)、法律状态徽章、专利族成员列表、申请人
- `needs_review` 条目标黄并在收件箱顶部单独分组

### 5.7 `notify.py` —— 可选手机提醒

默认关闭。开启后,仅当 `final ≥ 阈值` **且** 命中 `journals_core` 时推送一条。

- 推荐 **ntfy**(可自建,也可用公共实例,手机装 App 订阅 topic)
- 备选:Bark(iOS)、Server酱(微信)
- **限频**:每人/每天最多 1 条,`item_state.notified_at` 去重

---

## 6. 关键技术风险与应对

### 6.1 KMP 邮件模板变更

**风险**:CAS 调整邮件格式导致解析全面失效。
**应对**:原始邮件全量留档 + `parse_version` 机制;解析产出条数骤降 > 50% 时,在 `/stats` 页面告警。回归测试用 `fixtures/*.eml`。

### 6.2 KMP 邮件不含 DOI

**风险**:富化链路断裂,拿不到摘要。
**应对**:三级降级 —— ① DOI 正则 → ② 标题+作者+年份 查 Crossref 模糊匹配(≥0.90)→ ③ 标记 `needs_review`,Web 上人工确认后手动关联。**任何情况下条目本身(标题+期刊+日期+链接)都是可用的**,不会白跑。

### 6.3 专利权利要求全文拿不到

**风险**:EPO OPS 只提供**书目数据、专利族、法律状态**,**不提供权利要求全文**。
**应对**(按优先级降级):

1. 美国专利:USPTO 开放数据接口可获取权利要求,自动填充 `claim_1`
2. 其他局:不自动获取,**在详情页给出醒目的"到 SciFinder 查看权利要求"跳转链接**
3. 专利摘要通常过于宽泛,**不要用摘要代替权利要求做判断** —— 摘要字段在 UI 上弱化,并加提示语

> 这是本方案里最重要的一个诚实妥协:权利要求是专利判断的核心,但受接口能力所限,一期只能做到"跳转查看",不要假装能自动覆盖。

### 6.4 专利 18 个月公开延迟

专利"新公开"不等于"新发明",优先权日可能早在两年前。**UI 上同时显示优先权日和公开日**,并按专利族折叠,避免同一发明多局重复出现。

### 6.5 LLM 幻觉

**风险**:编造实验数据、作者、单位。
**应对**:prompt 中硬性约束 + "信息不足填`摘要未提及`";摘要中任何数字必须能在 `abstract` 原文中字符串匹配到,否则丢弃该字段并记日志。

### 6.6 合规风险

见 §2。额外措施:`auth.enabled` 默认 `true`;README 顶部放合规声明;不实现导出功能。

---

## 7. Prompt 设计

### 7.1 文献精排(listwise,每批 20 条)

```
你是化学文献筛选助手。根据用户画像,为每篇候选文献打相关度分。

【用户画像】
研究方向:{direction}
核心关键词:{keywords_core}
加分关键词:{keywords_bonus}
关注的 CAS 号:{cas_rn}
核心期刊:{journals_core}
排除方向:{negative}

【候选文献】
{items}
每篇格式: [序号] 标题 | 期刊 | 发表日期 | 摘要前 400 字

【打分标准】
90-100 直接相关,方法与方向高度匹配
70-89  明显相关,值得一读
40-69  沾边,可有可无
0-39   不相关

【硬性要求】
1. 只依据给定信息判断,不得推测
2. reason 用中文,不超过 30 字,说明"为什么推给这位用户"
3. 命中排除方向的,分数必须低于 30
4. 严格输出 JSON,不要任何额外文字

输出格式:
{"scores":[{"id":1,"score":85,"reason":"光氧化还原C–H活化,与在研项目直接相关"}]}
```

### 7.2 文献深度摘要

```
基于以下题录信息,输出结构化中文摘要。

【题录】
标题:{title}
期刊:{journal}  发表日期:{published_at}
作者:{authors}
摘要原文:{abstract}

【硬性要求 —— 违反将导致输出被丢弃】
1. 只能使用上面给出的信息。不得推测、不得补全、不得编造
2. 任何数值、化合物名、产率、选择性,必须能在"摘要原文"中找到原文
   找不到就填 "摘要未提及",绝不编造
3. 信息不足的字段一律填 "摘要未提及"
4. 全部使用中文,专业术语保留英文原词(如 photoredox)
5. 严格输出 JSON

输出格式:
{"one_liner":"一句话结论,不超过 40 字",
 "problem":"解决了什么问题",
 "method":"关键方法与条件(催化剂/配体/底物范围)",
 "key_results":"关键数据(产率/ee/TOF 等,必须来自原文)",
 "limitation":"作者自述或可推断的局限",
 "relevance":"对用户的{direction}方向有什么用"}
```

### 7.3 专利摘要(维度与文献不同)

```
基于以下专利信息,输出中文摘要。

【专利信息】
标题:{title}
公开号:{publication_number}   申请人:{assignees}
优先权日:{priority_date}   公开日:{published_at}
法律状态:{legal_status}
专利族成员:{family_members}
权利要求1:{claim_1}

【特别注意】
- 专利摘要通常写得宽泛,判断价值很低,不要过度依赖
- 如果"权利要求1"为"摘要未提及",必须在 scope 字段明确写
  "权利要求未获取,需到 SciFinder 查看",不得用标题推测保护范围
- 不要评估技术先进性,重点评估"是否可能影响用户方向的自由实施"

输出格式:
{"one_liner":"一句话说明这个专利在保护什么",
 "scope":"权利要求1的实际保护范围;未获取则明确说明",
 "assignee_note":"申请人是谁,是否是用户方向的活跃玩家",
 "legal_status_note":"法律状态及其含义(失效专利可能是自由实施机会)",
 "relevance":"对用户方向的影响"}
```

### 7.4 一句话摘要(批量,每批 10 条)

```
为下列文献各写一句话中文结论(不超过 40 字),只依据标题和摘要。
不得编造数据。严格输出 JSON: {"summaries":[{"id":1,"one_liner":"..."}]}

{items}
```

### 7.5 DeepSeek 调用参数

```python
{
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "response_format": {"type": "json_object"},
    "temperature": 0.2,          # 打分需稳定,低温度
    "max_tokens": 2048,
}
```

- 精排/摘要统一用 `deepseek-chat`(V3),性价比最高
- 如后续要加"方向脉络趋势分析",可单独用 `deepseek-reasoner`(R1)
- 注意 DeepSeek **不提供 embedding API** —— 这是 §3.3 选择 BM25 的原因

---

## 8. 配置文件

> 下面是设计稿的字段划分。实际可用字段以 `config.example.yaml` 为准:没有 `auth`
> 段,`app.host` / `app.port` 默认是回环地址与 8090,密钥一律只从环境变量读
> (见 `.env.example`)。

### 8.1 `config.yaml`

```yaml
app:
  host: "0.0.0.0"
  port: 8080
  db_path: "./data/litradar.db"
  timezone: "Asia/Shanghai"
  auth:
    enabled: true
    password_hash: ""        # 用 `litradar hash-password` 生成

llm:
  provider: deepseek
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"
  api_key_env: "DEEPSEEK_API_KEY"     # 只读环境变量,不写进 yaml
  rerank_batch_size: 20
  rerank_top_k: 0     # 0 = 不截断,窗口内每条都过 LLM;>0 才按粗排名次截断
  deep_summary_top_n: 8
  temperature: 0.2

sources:
  kmp:
    enabled: true
    imap:
      host: "imap.example.edu"
      port: 993
      use_ssl: true
      user: "you@example.edu"
      password_env: "IMAP_PASSWORD"
      folder: "INBOX"
      search: 'UNSEEN SUBJECT "Keep Me Posted"'
    parse_version: 1

  crossref:
    enabled: true
    mailto: "you@example.edu"
    issns: []                # 期刊 ISSN 白名单,建议 20-50 本
    lookback_days: 7

  rss:
    enabled: false
    feeds: []                # [{name, url}]

  manual:
    enabled: true
    import_dir: "./data/import"

enrich:
  crossref: { enabled: true, rate_limit_rps: 1 }
  openalex: { enabled: true, rate_limit_rps: 5 }
  epo:
    enabled: false           # 需注册 OAuth2
    consumer_key_env: "EPO_KEY"
    consumer_secret_env: "EPO_SECRET"
    rate_limit_rps: 1

ranking:
  weights:
    llm: 0.60
    coarse: 0.25
    rule: 0.15
  coarse:
    method: "bm25"           # 'bm25' | 'embedding'(预留)

notify:
  enabled: false
  provider: "ntfy"           # 'ntfy' | 'bark' | 'serverchan'
  ntfy:
    server: "https://ntfy.sh"
    topic: "litradar-xxxx"
  min_score: 80              # 只有高分才即时提醒
  daily_limit: 1

schedule:
  daily: "*-*-* 07:00:00"
  weekly: "Mon *-*-* 08:00:00"
```

### 8.2 `profiles/default.yaml`

```yaml
name: "me"
direction: "不对称催化 / 光氧化还原催化"

keywords:
  core:                       # 命中即显著加分
    - photoredox
    - asymmetric catalysis
    - C–H activation
  bonus:                      # 命中适度加分
    - enantioselective
    - earth-abundant metal
    - ligand design

cas_rn:                       # SciFinder 强项,直接从检索式里取
  - "7440-44-0"
  - "..."

authors_watch:                # 重点关注作者
  - "..."
assignees_watch:              # 专利申请人监控
  - "..."

journals:
  core:                       # 保送 Stage 3,并触发即时提醒
    - "J. Am. Chem. Soc."
    - "Angew. Chem. Int. Ed."
    - "Nature Chemistry"
    - "ACS Catalysis"
    - "Chemical Science"
  ok:                         # 正常参与排序
    - "Org. Lett."
    - "Chem. Commun."

negative:                     # 命中直接丢弃
  - "computational study only"
  - "review"
  - "retraction"

patent:
  enabled: true
  cpc_prefixes: []            # 如 ["C07B", "C07D"]
```

---

## 9. 目录结构

```
litradar/
├── README.md
├── docs/
│   ├── litradar-design.md        ← 本文档
│   └── COMPLIANCE.md             ← 合规边界声明
├── pyproject.toml
├── config.example.yaml
├── profiles/
│   └── default.yaml
├── litradar/
│   ├── __init__.py
│   ├── config.py                 # pydantic-settings 加载 config.yaml
│   ├── db.py                     # 连接、迁移、WAL
│   ├── models.py                 # dataclass / pydantic 模型
│   ├── normalize.py              # 标题/DOI 归一化、模糊匹配
│   ├── sources/
│   │   ├── kmp_email.py          # ★ 核心
│   │   ├── crossref.py
│   │   ├── rss.py
│   │   └── manual.py
│   ├── enrich/
│   │   ├── http.py               # 令牌桶 + 退避 + 缓存
│   │   ├── crossref.py
│   │   ├── openalex.py
│   │   └── epo.py
│   ├── rank/
│   │   ├── rules.py              # Stage 1
│   │   ├── coarse.py             # Stage 2 (BM25)
│   │   └── llm_rerank.py         # Stage 3
│   ├── summarize.py              # 深度 / 一句话摘要
│   ├── llm.py                    # DeepSeek 客户端(OpenAI 兼容)
│   ├── notify.py
│   └── web/
│       ├── app.py                # FastAPI 实例 + 静态文件
│       ├── routes.py
│       ├── auth.py
│       ├── templates/
│       │   ├── base.html
│       │   ├── inbox.html
│       │   ├── week.html
│       │   ├── patents.html
│       │   ├── item.html
│       │   ├── profile.html
│       │   ├── stats.html
│       │   └── partials/
│       │       ├── item_row.html
│       │       └── action_buttons.html
│       └── static/
│           ├── style.css
│           ├── htmx.min.js
│           └── app.js
├── scripts/
│   ├── run_pipeline.py           # 串起 ingest→enrich→rank→summarize
│   ├── run_stage.py              # 单阶段执行(调试)
│   └── seed_demo.py              # 灌入 fixture 数据
├── fixtures/
│   ├── sample_kmp.eml            # ★ 真实 KMP 邮件样本(回归测试基准)
│   └── sample_items.json
├── deploy/
│   ├── litradar-web.service
│   ├── litradar-daily.service
│   ├── litradar-daily.timer
│   └── litradar-weekly.timer
└── tests/
    ├── test_kmp_parser.py
    ├── test_dedup.py
    └── test_ranking.py
```

---

## 10. 部署

> **本节保留设计原稿,不要照抄。** 可执行的安装与部署步骤在 README 的「部署」一节
> (含 macOS launchd 与 Windows 任务计划的等价方案),单元文件以 `deploy/` 为准。
> 10.1 的 `0.0.0.0:8080` 会绕过 nginx 的 TLS 与口令保护;10.3 的原稿命令里
> `hash-password` 不存在、`seed_demo.py` 从未创建。差异汇总见文首修订记录。

### 10.1 systemd unit

`deploy/litradar-web.service`
```ini
[Unit]
Description=LitRadar Web
After=network-online.target

[Service]
Type=simple
User=%i
WorkingDirectory=/srv/Work/LitRadar
EnvironmentFile=/srv/Work/LitRadar/.env
ExecStart=/srv/Work/LitRadar/.venv/bin/uvicorn litradar.web.app:app \
    --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`deploy/litradar-daily.timer`
```ini
[Unit]
Description=LitRadar daily pipeline

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### 10.2 环境变量(`.env`,权限 600)

```
DEEPSEEK_API_KEY=sk-xxxx
IMAP_PASSWORD=xxxx
EPO_KEY=xxxx
EPO_SECRET=xxxx
```

### 10.3 启动步骤

原稿里的 `litradar hash-password`(无此命令)与 `scripts/seed_demo.py`(从未创建)
已删除;口令改由 `LITRADAR_TOKEN` 承担,演示数据改用 pytest fixture。可执行的步骤
以 README 为准,当前等价命令是:

```bash
python -m venv .venv && .venv/bin/pip install -e .
cp config.example.yaml config.yaml
cp interests.example.yaml interests.yaml
cp .env.example .env && chmod 600 .env
.venv/bin/litradar check        # 先体检:配置 / 密钥 / 数据库 / 网络 / LLM
.venv/bin/litradar init-db
.venv/bin/litradar run
.venv/bin/uvicorn litradar.web.app:app --host 127.0.0.1 --port 8090
```

systemd 单元必须以**模板名**安装(原稿写的 `litradar-web` / `litradar-daily.timer`
已被 systemd 拒绝:`%i` 为空):

```bash
sudo cp deploy/litradar-web.service    /etc/systemd/system/litradar@.service
sudo cp deploy/litradar-daily@.service /etc/systemd/system/
sudo cp deploy/litradar-daily@.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now litradar@$(whoami).service litradar-daily@$(whoami).timer
```

访问:`http://127.0.0.1:8090`(原稿写的 `http://<局域网IP>:8080` 已改为只绑回环,
局域网访问经反向代理,见 `deploy/litradar-nginx.conf`)

---

## 11. 分期计划

| 期 | 范围 | 验收标准 | 预估 |
|---|---|---|---|
| **一期** | 骨架 + mock 数据全链路跑通:DB schema、Web 收件箱/详情/三按钮、DeepSeek 精排+摘要、`seed_demo.py` | 浏览器打开局域网 IP,看到按相关度排序的真实页面并能点反馈 | 1–2 天 |
| **二期** | 接真实数据:KMP IMAP 解析、Crossref 兜底、富化、systemd 定时 | 每天早上自动更新,无需人工干预 | 2–3 天 |
| **三期** | 专利完善:EPO OPS 专利族+法律状态、USPTO 权利要求、专利视图、申请人监控 | 专利按族聚合去重,法律状态可见 | 2–3 天 |
| **四期** | 反馈闭环消费:权重自动调整 / few-shot、趋势分析、`needs_review` 处理流 | 排序质量随使用可观测提升 | 按需 |

**一期就要把 `⭐/✅/❌` 做进去**,哪怕后端只存表不消费 —— 反馈数据攒得越早越有价值。

---

## 12. 待确认事项

| # | 事项 | 阻塞? |
|---|---|---|
| 1 | 拿到 1–2 封**真实 KMP 邮件样本**放 `fixtures/`,用于校准解析器 | 阻塞二期 |
| 2 | 图书馆三问(§2.3) | 不阻塞,但可能改变架构 |
| 3 | 核心期刊 ISSN 白名单(建议 20–50 本) | 阻塞二期 |
| 4 | 真实画像:方向、关键词、CAS RN、关注作者/申请人 | 阻塞打分质量 |
| 5 | 是否开启手机提醒,选哪个 provider | 不阻塞 |
| 6 | 部署机器(本机?实验室常开的机器?)与局域网端口 | 阻塞部署 |

---

## 附录 A:关键外部接口速查

| 用途 | 端点 |
|---|---|
| Crossref 检索 | `https://api.crossref.org/works?filter=issn:{issn},from-created-date:{date}&mailto={email}` |
| OpenAlex 单篇 | `https://api.openalex.org/works/doi:{doi}` |
| EPO OPS 认证 | `https://ops.epo.org/3.2/auth/accesstoken` |
| EPO 专利族 | `https://ops.epo.org/3.2/rest-services/family/publication/epodoc/{num}` |
| DeepSeek Chat | `https://api.deepseek.com/chat/completions` |

## 附录 B:参考实现(可借鉴,不直接依赖)

- [jannisborn/paperscraper](https://github.com/jannisborn/paperscraper) —— 多源元数据抓取(含 chemrxiv),虽不用预印本,其 API 封装可参考
- [magedbekheet/ai-literature-feed-automation](https://github.com/magedbekheet/ai-literature-feed-automation) —— Crossref + LLM 摘要 + digest 的完整参考
- [chemrxiv-dashboard](https://github.com/chemrxiv-dashboard/chemrxiv-dashboard.github.io) —— 元数据批量下载思路
- [PubChem-MCP-Server](https://github.com/augmented-nature/pubchem-mcp-server) —— 若后续要加化合物信息补充
