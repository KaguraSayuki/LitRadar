# LitRadar 实现架构

> 本文描述**当前代码**的组织方式、数据流与不变量,面向要读或改这份代码的人。
>
> - 安装、配置、部署:见 [README](../README.md)
> - 设计推演过程(与实现有出入,刻意保留原貌):见 [litradar-design.md](litradar-design.md)
>
> 文中所有模块名、表名、路由、命令都以当前代码为准;不确定的地方请直接读代码,
> 本文不复制字段默认值以免又一次漂移。

---

## 1. 总览

一条单向流水线,全部状态落在一张 SQLite 库里:

```
邮件 / 检索 API ──→ ingest ──→ enrich ──→ rank ──→ summarize ──→ Web
                      │          │          │           │
                    item 表    item_      score 表   summary 表
                   (去重)   enrichment
```

`pipeline.run_all()` 是唯一的编排入口,顺序固定:

| 顺序 | 阶段 | 入口 | 说明 |
|---|---|---|---|
| 1 | 采集 | `pipeline.ingest_mail` / `ingest_keyword_search` | 邮件与检索两条腿 |
| 2 | 富化 | `enrich.run` | 补摘要、引用数、期刊等级 |
| 3 | 排序 | `rank.run` | 三阶段漏斗,写 `score` |
| 4 | 摘要 | `summarize.run` | 中文结构化摘要,写 `summary` |

CLI 的 `litradar run` 就是跑这四步;每步也能单独跑(`ingest` / `enrich` / `rank` /
`summarize`),用于调试或补跑。

## 2. 目录与模块

```
litradar/
├── config.py        配置加载。数据类分层 + 密钥统一走环境变量
├── db.py            SQLite 数据层:建表、迁移、常用读写。单用户,无 ORM
├── lock.py          单实例锁(防止定时任务与网页按钮互抢 API 限流)
├── http.py          统一 UA / 重试 / 退避
├── normalize.py     DOI、标题、作者、日期归一化 —— 去重的地基
├── pipeline.py      流水线编排 + 入库唯一入口 `_store`
├── enrich.py        富化编排
├── rank.py          三阶段排序
├── summarize.py     中文摘要 + 数字核验
├── llm.py           DeepSeek 客户端(OpenAI 兼容)
├── journal_rank.py  期刊等级标签的渲染规则(纯函数,无 IO)
├── web/app.py       FastAPI 应用(无前端构建,模板 + 原生 fetch)
└── sources/         数据源,每个模块自己负责协议细节
    ├── mail.py            邮件接入:folder / maildir / imap
    ├── xmol_email.py      X-MOL 订阅邮件解析
    ├── semanticscholar.py S2:摘要主力、检索、引用滚雪球
    ├── crossref_search.py Crossref:权威元数据 + 关键词检索
    ├── openalex_search.py OpenAlex 检索(可选)
    └── easyscholar.py     期刊等级 / 影响因子 / 分区
```

分层原则:**`sources/` 只讲协议,不碰数据库**;所有入库都经过
`pipeline._store` → `db.upsert_item`。这样去重、洗字段、写 `item_state` 只有一份实现。

## 3. 数据流

### 3.1 采集

**邮件这条腿**(`pipeline.ingest_mail`):

1. `mail.iter_messages` 按 `mail.mode` 从 folder / maildir / IMAP 取信;
2. `xmol_email.parse_bytes` 解析题录;不是 X-MOL 模板的邮件**留档但不入库**;
3. 每封邮件在一个 savepoint 内写 `raw_email` + 该邮件全部 `item`,**全部成功后**
   才 `commit`,再 `acknowledge` 邮件(标已读 / 移入 processed)。

顺序很关键:确认收信必须发生在提交之后。若先 ack 再写库,进程在中间死掉这封
邮件就永远不会再被读到。反过来,失败时回滚本封的写入、不 ack,下一轮重放。

`raw_email` 让这件事可恢复:历史上"原文已提交、条目只落了一半"的邮件,会在下一轮
从库里重放补全 —— 即使它已经被归档或标为已读。

**检索这条腿**(`pipeline.ingest_keyword_search`):按 `interests.yaml` 里的画像查询
并行打 Crossref / Semantic Scholar / OpenAlex,结果同样过 `_store`。

**滚雪球**(`pipeline._ingest_snowball`)与上面不同:它不直接入库,而是先把
"谁引用了种子"累积进 `seed_cite`,再在**全部种子、全部历史轮次**上统计共被引。
这样单次 API 失败不会把共被引计数打散。`snowball_min_cocitations` 默认 1
(即全收),共被引数只作为质量标签展示,不作准入门槛。

### 3.2 去重

只有一个键:`item.dedup_key`,**UNIQUE 约束**。生成规则在 `pipeline._prepare`:

- 有 DOI → `doi:<归一化后的 DOI>`;
- 否则 → `title:<归一化标题的前 120 字符>`。

DOI 归一化后再生成键,而不是只修 `item.doi` —— 不同来源对大小写的处理不一致,
若按原始写法建键,`10.1234/ABC` 与 `10.1234/abc` 会变成两条。

`db.upsert_item` 命中已有键时**只补空字段,不覆盖已有内容**,避免低质量来源盖掉
高质量来源。入库时统一洗刊名(HTML 实体、换行)、拒绝非 http(s) 链接。

### 3.3 富化

`enrich.run` 只处理"缺摘要或未富化过"的条目,顺序是:

1. **Semantic Scholar 批量补摘要 / 引用数 / OA**(摘要主力,实测覆盖最好);
2. **Crossref 补权威元数据**(期刊全称、ISSN、作者),可覆盖过时字段;
3. **easyScholar 补期刊等级**,结果按刊名缓存进 `journal_rank` 表。

第 3 步是按次计额的接口,所以有两道保护:缓存表让同一本刊只查一次;单轮
`max_lookups` 上限防止异常数据打爆额度。接口明确说"查不到"才缓存 `hit=0`,
网络 / 认证 / 协议错误不写缓存,留待下次重试。

`item_enrichment.abstract_attempts` 记录"富化过但仍没摘要"的次数,攒够上限就不再
进重试队列 —— 来源里真没有摘要的条目否则会每轮陪跑 S2 + Crossref,永远烧请求。

### 3.4 排序

`rank.run` 是漏斗,三阶段各自写自己的分数,**最终分**是加权和:

```
final = w_llm * llm + w_coarse * coarse + w_rule * rule
```

| 阶段 | 函数 | 做什么 |
|---|---|---|
| 1 | `rule_filter` | 按画像的期刊白名单、关键词、类型做硬过滤;被否的标记 `excluded=1` |
| 2 | `coarse_rank` | BM25 粗排(`rank-bm25`),**只给顺序,不做硬截断** |
| 3 | `llm_rerank` | DeepSeek listwise 精排,产出分数 + 中文理由 |

LLM 失败时的降级行为值得注意:没拿到 LLM 分的条目,`final` 由粗排 + 规则分归一到
满量程,并**封顶在 `w_coarse + w_rule`**。早期版本除以 `w_coarse+w_rule` 补回满量程,
结果"精排失败"的条目反而拿到和真高分一样的分 —— 界面上无法区分。现在这个缺陷
由前端用红色警示标签显式标出。

精排 prompt 会带少量**收藏 / 忽略的标题**做少样本校准(`feedback_examples`),
让排序跟着用户的反馈走。

### 3.5 摘要

`summarize.run` 分两档:窗口内排名靠前的条目做**深度摘要**(问题 / 方法 / 关键结果 /
局限 / 对研究的用处),其余做**简要摘要**(中文标题 + 一句话结论)。

防幻觉有两道:

- `verify_numbers`:摘要 `key_results` 里出现、但英文原文中找不到的数字,标为待核对;
- `summary.abstract_hash`:原文摘要的指纹。原文补齐或改变后摘要会重做,而单纯更新
  引用数不会触发重做。

## 4. 数据模型

| 表 | 职责 | 关键约束 |
|---|---|---|
| `item` | 题录主表 | `dedup_key` UNIQUE;`kind` 区分 paper / patent |
| `item_state` | 每条目的展示与反馈状态 | `state`(new/read/archived)、`starred`、`ignored`、`excluded` |
| `feedback` | 用户动作的**事件流** | 追加写;状态由事件推导,便于合并重复条目时回溯 |
| `item_enrichment` | 富化结果 | 1:1 于 item;`abstract_attempts` 控制重试 |
| `score` | 三阶段分数与理由 | 1:1 于 item |
| `summary` | 中文摘要 | 1:1 于 item;`abstract_hash` 指纹 |
| `journal_rank` | 期刊等级缓存 | 主键是归一化刊名;`hit=0` 表示接口明确无结果 |
| `raw_email` | 邮件原文 + 处理标记 | `processed_at` 是"这封处理完了"的唯一依据 |
| `seed_cite` / `seed_query` | 滚雪球累积的引用关系与刷新时间 | 关系跨轮次累积 |
| `run_log` | 每阶段运行记录 | CLI 与网页都写 |
| `item_fts` | FTS5 全文检索 | `content='item'`,由三个触发器同步 |

迁移用 `PRAGMA user_version` 记进度,只补跑缺的步骤(`db._migrations`)。新库直接按
最新 `SCHEMA` 建表并盖章,不走历史迁移。**加字段时两处都要改**:`SCHEMA` 与一个新的
迁移函数。

## 5. 关键不变量

1. **入库只有一个入口** —— `pipeline._store` → `db.upsert_item`。任何新来源都必须
   走这里,才能共享去重与洗字段逻辑。
2. **`dedup_key` 决定幂等** —— 同一条文献无论从几个来源、被采集几次,都收敛到一行。
3. **收信确认在事务提交之后** —— 见 3.1;崩溃只会导致重复处理,不会丢数据。
4. **单实例锁** —— `lock.py` 保证定时任务与网页按钮不会同时跑同一阶段。两个 enrich
   并发会互抢 Semantic Scholar 的限流,表现为"批量全空、补不到摘要",且极难排查。
   CLI 与 Web 共用同一个 `data/litradar.lock`。
5. **密钥只走环境变量** —— 统一经 `config.read_secret()` 读取(去首尾空白、空值视为
   未配置),绝不写进 `config.yaml`,也不进日志。
6. **只绑回环地址** —— 对外由反向代理负责 TLS 与访问控制;绑 `0.0.0.0` 会绕过它们。

## 6. Web 层

`litradar/web/app.py` 是单文件 FastAPI 应用,模板 + 原生 `fetch`,无前端构建步骤。

| 方法与路径 | 用途 |
|---|---|
| `GET /` | 雷达页(收件箱),按最终分排序 |
| `GET /week` | 本期精选 |
| `GET /search` | 全库全文检索(FTS5) |
| `GET /item/{item_id}` | 详情页 |
| `POST /item/{item_id}/action` | 收藏 / 已读 / 忽略等反馈 |
| `GET/POST /interests` | 在线编辑画像(保存为原子替换 + 轮转备份) |
| `GET /stats` | 来源 / 期刊命中率、反馈统计、运行记录 |
| `POST /admin/run/{stage}` | 手动触发单阶段 |
| `GET /healthz` | 健康检查 |

两道闸:

- `require_token`:设了 `LITRADAR_TOKEN` 就要求 `?k=`、`X-Token` 或 cookie
  (`hmac.compare_digest` 比较,避免时序侧信道)。未设则放行 —— 默认只绑回环,
  本就访问不到。
- `require_same_origin`:写操作要求同源。**这不是多余的**:`/admin/run/*` 是不带
  CSRF token 的简单 POST,你在浏览器里打开的任意网页都能往 `127.0.0.1:8090`
  发跨源 POST,把 DeepSeek 额度烧掉,即使服务只绑本机。

## 7. 配置

`config.py` 里按用途分数据类:`AppConfig`、`LLMConfig`、`MailConfig`、
`SourceConfig`、`RankingConfig`、`JournalRankConfig`,合成一个 `Config`。

- 路径统一用 `_expand` 解析成绝对路径(相对项目根),这样从任何工作目录启动、
  或在 systemd / 任务计划里启动结果都一致;
- 项目根由 `__file__` 推导,不依赖当前工作目录;
- `.env` 的加载规则是"**真实环境优先,空值不回填**":`override=True` 会让 `.env`
  里的空占位符清掉命令行传入的口令(实测踩过),`override=False` 又让改了 `.env`
  不重启不生效。

## 8. 怎么加一个新来源

1. 在 `sources/` 下写一个模块,只负责协议:暴露 `search(...)` 或 `fetch(...)`,
   返回 dict(至少要有 `title`),**不要碰数据库**;
2. 把它接进 `pipeline.ingest_keyword_search` 或 `enrich.run`;
3. `dedup_key` 不用自己拼 —— 有 DOI 就填 `doi`,`_prepare` 会生成规范键;没 DOI 时
   留空,它会退化成 `title:` 键;
4. 在 `SourceConfig` 加开关(默认关,或明确写清代价),并在 `config.example.yaml`
   里给注释;
5. 补测试:解析用真实样本,网络层用打桩,不要依赖线上接口。

## 9. 容易踩的坑

- **排序窗口必须 ≥ 抓取窗口**(`app.pipeline_window_days`)。抓回来却落在排序窗口
  之外的条目会永远"未评分",在收件箱里长成一片噪声。网页按钮读的是同一个值,
  早期写死 30 天而抓取 180 天的组合,实测让 51 条里有 26 条从未进过排序器。
- **期刊等级的 `hit=0` 是有意义的负缓存**,不要把网络故障也写进去,否则一次抖动
  就会让某本刊永远查不到等级。
- **`excluded` 是规则结果,不是用户决定**。用户明确收藏的条目应让 `excluded` 让路,
  下一轮按最新画像重算。
- **`raw_email` 的留档不等于"已处理"** —— 只有 `processed_at` 才是。补解析逻辑的
  时候这是重放的基础。
- **改 schema 要同时改 `SCHEMA` 和加迁移函数**;只改前者,老库不会更新。
