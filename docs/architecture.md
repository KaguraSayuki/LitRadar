# LitRadar 实现架构

本文面向阅读和修改代码的开发者，描述当前模块职责、数据流与关键约束。
安装和日常操作见 [README](../README.md)，配置与使用多个研究方向见
[订阅组配置](subscription-groups.md)，技术选择的背景见 [设计记录](litradar-design.md)。

## 模块与入口

`pipeline.run_all()` 编排完整流水线，依次运行邮件采集、按组检索、全局元数据补全、
按组排序和摘要。CLI 与 Web 也可单独调用各阶段。

| 模块 | 职责 |
|---|---|
| `cli.py` | 参数解析、阶段调用、诊断命令与退出码 |
| `config.py` | 运行配置、路径解析与环境变量读取 |
| `settings.py` / `settings_fields.py` | 按页合并、字段校验、版本冲突、锁、原子写入和备份恢复 |
| `credentials.py` | 凭据来源、私有保存及任务快照 |
| `query_builder.py` / `connection_checks.py` | 普通条件编译、旧查询保留、只读预览和连接测试 |
| `execution.py` | 网页、CLI 与调度共用的阶段运行频率限制 |
| `scheduler.py` | 独立进程、每日持久认领、状态和错过任务处理 |
| `pipeline.py` | 采集编排、题录预处理与分组入库 |
| `db.py` | SQLite schema、迁移、读写及分组状态 |
| `normalize.py` | DOI、标题、日期、作者与链接归一化 |
| `sources/` | 邮件解析、各数据源的请求协议与结果转换 |
| `enrich.py` | 批量补全文献元数据、管理期刊等级缓存 |
| `rank.py` | 画像校验、订阅组加载、规则过滤、BM25 和 LLM 精排 |
| `summarize.py` | 共享摘要、方向性说明、原文指纹与数字核验 |
| `llm.py` | OpenAI Chat Completions 兼容客户端，支持自定义地址、参数适配与结构化结果校验 |
| `http.py` | 通用 HTTP 请求、重试与退避支持；部分数据源另有专用请求逻辑 |
| `journal_rank.py` | 期刊等级标签的字段选择与文本转换 |
| `lock.py` | CLI 与 Web 共用的流水线互斥锁 |
| `passwords.py` | 管理员密码的 PBKDF2 哈希与验证 |
| `web/` | FastAPI 路由、Jinja2 模板及原生 JavaScript |

`sources/` 负责协议和记录转换，不直接写入数据库。新增题录通过
`pipeline._store()` → `db.upsert_item()` 入库，复用去重和字段清洗；元数据补全与
各类状态更新使用 `db.py` 的相应接口。

## 数据流

```text
邮件 / 学术 API
       ↓
item + item_group        共享题录及按组成员关系
       ↓
item_enrichment          摘要、引用数等元数据
       ↓
score + group_state      按组评分、规则排除
       ↓
summary + summary_group  共享中性摘要、按组方向性说明
       ↓
Web 阅读与反馈           全局阅读状态及按组忽略状态
```

### 邮件采集

`pipeline.ingest_mail()` 首先检查 `sources.xmol_enabled`。关闭时跳过所有邮件接入
和历史原文重放。开启后同步配置中的组，提交组信息，再进行邮件网络读取。

`mail.iter_messages()` 从 folder、Maildir 或 IMAP 获取邮件，
`xmol_email.parse_bytes()` 解析题录。无法识别为 X-MOL 条目的邮件可留存原文，
但不会生成文献条目。

每封邮件在一个 savepoint 中保存原文、全部条目、组成员关系和 `processed_at`。
事务提交成功后才调用 `acknowledge()`，执行归档或标记已读。写入失败时回滚本封并
保留重试条件。IMAP 网络等待不应持有未提交的数据库写事务。

`raw_email.processed_at` 是处理完成的依据。历史上已保存原文但尚未标记完成的邮件，
会从数据库重放，即使外部邮件已归档或标为已读。

IMAP 使用共享的 TLS 连接入口，校验证书与主机名。只读模式使用 `readonly=True`
与 `BODY.PEEK[]`；退出使用 `LOGOUT`，不执行可能清除已删除邮件的 `CLOSE`。

### 检索与被引追踪

`pipeline.ingest_keyword_search()` 逐组执行检索。各组按配置顺序调用 Crossref、
Semantic Scholar 和 OpenAlex，再处理引用滚雪球，结果归一化去重后入库并记录组成员。
当前请求按顺序执行，没有跨来源或跨组的并行请求。

Semantic Scholar 检索使用 `/paper/search/bulk`。日期先转换为年份范围传给服务端，
再根据返回记录的具体日期进行本地过滤。检索与被引追踪在未配置 `S2_API_KEY` 时跳过。

滚雪球沿“哪些论文引用了种子文献”的方向检索。`seed_cite` 保存本组的引用关系，
`seed_query` 保存本组种子的刷新时间；同一 DOI 被多个组使用时，各组独立维护进度。
每轮优先刷新较久未查询的种子，并在本组全部种子及历史轮次上累计共同引用数量。
默认门槛为 1，数量主要用于展示质量线索。

### 去重与元数据补全

`item.dedup_key` 有唯一约束。`pipeline._prepare()` 的键生成规则为：

- 有 DOI：`doi:<归一化后的 DOI>`。
- 无 DOI：`title:<归一化标题的前 120 个字符>`。

DOI 大小写在建键前统一处理，入库时清洗刊名并过滤非 HTTP(S) 链接。
`db.upsert_item()` 命中已有条目时补充空字段，也允许具体日期替换仅有年份的占位日期。
无 DOI 的标题键不是模糊匹配，不能保证与日后带 DOI 的同一文献自动合并。

`enrich.run()` 优先处理尚未补全的条目，再处理仍缺摘要且未达到重试上限的条目：

1. Semantic Scholar 批量获取摘要、引用数与开放获取信息。
2. Crossref 批量补充元数据；规范的期刊、ISSN、作者等非空字段可覆盖旧值。
3. easyScholar 按刊名查询期刊等级，并缓存到 `journal_rank`。

摘要重试通过 `item_enrichment.abstract_attempts` 限制。期刊等级使用别名归一化和
每轮 `max_lookups` 上限；仅在接口明确无结果时写入 `hit=0`，网络、限流、认证或
协议错误保留重试机会。未配置 easyScholar 密钥时不发起查询。

### 排序

`rank.run()` 只读取各组在运行窗口内的 `item_group` 成员：

| 阶段 | 行为 |
|---|---|
| 规则过滤 | 按标题前缀、标题排除词及通用排除词筛选；关键词、期刊、作者等作为加分项 |
| BM25 粗排 | 计算关键词相关度并决定顺序；默认不截断，`rerank_top_k > 0` 时限制候选 |
| LLM 精排 | 分批评分并给出推荐理由，参考全局收藏和本组已有分数的忽略条目 |

分数和自动排除状态按组保存。收藏条目不会因规则变化被自动隐藏，但仍可能缺少新分数。
运行时会清理本组窗口内未进入最终候选集的旧分数。

有 LLM 评分时，最终分按 `w_llm × llm + w_coarse × coarse + w_rule × rule` 合成。
同轮仅部分条目缺少 LLM 分时，这些条目保留粗排与规则的加权分，不重新归一化；若
整轮没有任何 LLM 分，则将关键词与规则分按其权重和归一化。网页另行区分关闭精排、
缺少密钥和已启用但缺少评分的状态，避免只凭最终分误判结果。

### 摘要与缓存

`summarize.run()` 将窗口内的摘要任务分为深度和简要两档。每次运行先汇总参与组的
深度摘要需求，再生成共享内容，最后补齐各组方向性说明。

- `summary` 保存中文标题、简要结论及深度摘要中的中性内容。
- `summary_group` 保存“对研究的用处”，读取时必须同时限定组和条目。
- 本轮已成功生成的共享摘要按条目去重，`--force` 也遵循这一规则；各组需要的深度先
  合并，避免因组的处理顺序重复生成简要和深度版本。
- 原文摘要指纹变化会使旧摘要失效，也会清理失去原文依据的方向性说明。仅强制重算
  不会清除未参与本轮的其他组说明。
- 缺少本组说明时保持为空，不能回退读取全局旧字段或其他组说明。

`db.sync_groups()` 比较方向文本去除首尾空白后的前 300 个字符，与提示词使用范围
一致。方向改变只清除该组的方向性说明，名称改变不触发清理。
`interest_group.direction_initialized` 区分已建立的方向基线与旧库首次同步，防止
升级时误清除已迁移的 `default` 说明。

`verify_numbers()` 标记关键结果中无法在原文摘要匹配的数字。提示词要求标注推断，
但这些措施不构成对生成内容正确性的完整验证。

## 数据模型与迁移

| 表 | 当前职责 |
|---|---|
| `item` | 共享题录；`dedup_key` 唯一，当前采集生成 `paper` 条目 |
| `item_enrichment` | 一篇一行的元数据补全结果与摘要重试次数 |
| `item_state` | 全局阅读状态与收藏状态 |
| `interest_group` | slug、显示名、方向、启用开关及方向同步基线 |
| `item_group` | `(group_id, item_id)` 成员关系，决定组内可读取的条目 |
| `score` | `(group_id, item_id)` 评分与推荐理由 |
| `group_state` | `(group_id, item_id)` 用户忽略及规则排除状态 |
| `summary` | 按条目共享的中性摘要及原文指纹 |
| `summary_group` | `(group_id, item_id)` 方向性说明 |
| `feedback` | 追加记录条目、动作与时间；当前日志不含组标识，实际分组状态以 `group_state` 为准 |
| `journal_rank` | 按归一化刊名保存查询结果及明确无结果的缓存 |
| `raw_email` | 邮件原文与完成标记，支持失败重试和历史重放 |
| `seed_cite` / `seed_query` | 按组保存种子引用关系与刷新记录 |
| `run_log` | 阶段、状态、统计、错误与可选的组标识 |
| `scheduled_run` | 以本地日期为唯一键记录每日认领、开始、结束和结果 |
| `scheduler_worker` | 单一调度进程的心跳与设置错误提示 |
| `item_fts` | 由触发器同步的 FTS5 索引，覆盖标题、原文摘要、期刊和作者 |

`item_state.ignored/excluded` 与 `summary.relevance` 是兼容旧库保留的字段，不再作为
当前分组状态的读写来源。界面读取 `group_state` 和 `summary_group`；迁移会将可确定
归属的历史单方向说明转入 `default`。

当前 schema 版本为 8，使用 `PRAGMA user_version` 记录迁移进度。新库直接按最新
`SCHEMA` 建表，旧库顺序执行尚未完成的迁移。修改表结构时必须同时更新 `SCHEMA`
和迁移列表，并验证从旧结构升级后的数据保留情况。

## Web 层与组上下文

| 路由 | 用途 |
|---|---|
| `GET /` | 当前组收件箱 |
| `GET /week` | 当前组本期精选 |
| `GET /search` | 当前组成员的 FTS5 检索 |
| `GET /item/{item_id}` | 当前组中的文献详情 |
| `POST /item/{item_id}/action` | 收藏、已读、忽略及撤销 |
| `GET/POST /interests` | 编辑整份画像，校验后原子替换并轮转备份 |
| `/settings/groups` / `/settings/group?slug=…` | 方向卡片、单组表单及版本化保存 |
| `/settings/services` / `/settings/reading` | 服务、邮箱、阅读及运行设置 |
| `/settings/schedule` / `/settings/maintenance` | 每日计划、状态、访问与恢复 |
| `/setup` / `/login` / `/logout` | 一次性初始化与访问会话 |
| `GET /stats` | 当前组统计与相关运行记录 |
| `POST /admin/run/{stage}` | 手动运行所选阶段 |
| `GET /healthz` | 健康检查 |

页面显式携带 `?g=<slug>`，反馈、撤销与流水线请求沿用页面的组。兼容旧写请求时可从
同源 Referer 获取组；Cookie 仅作没有显式组时的默认值。组标识在 URL 和 Cookie 中
编码，解析失败时按既定回退规则处理。显式指定已不存在的组进行写操作会被拒绝，
未同步或没有成员的组显示空列表。

`/admin/run/*` 检查访问会话或接口口令、同源条件与非回环保护。受限阶段接受登录会话
或额外管理员密码；取得流水线锁后调用统一次数校验再执行。CLI 和调度也在同一把锁内
检查次数，并使用相同的任务凭据快照。具体范围见 [部署与维护](deployment.md)。

## 设置、凭据与调度

配置读取与普通写入共用配置旁的短期锁。网页版本由配置和画像内容的摘要组成，保存
只合并当前页字段；CLI 或其他页面变更后拒绝旧版本。旧单方向首次转换保留 `default`，
网页改名固化旧标识，停用不删除文献。原子写入使用唯一临时文件和 `os.replace`，
普通设置保留五份备份，凭据单独写入且不自动备份。

凭据优先级为进程环境、配置旁的私有文件、配置旁的 `.env`，不再将 `.env` 混入
`os.environ`。私有空标记屏蔽旧文件值；进程环境中的显式空值同样属于部署管理。
每个任务开始时固定配置与凭据，更换密钥只影响新任务。来源模块通过 `read_secret`
读取当前任务的 ContextVar，配置属性通过附加快照读取同一版本。

首次设置链接仅由本机签发，有效期 30 分钟，原子消费后保存密码哈希。登录失败次数
持久保存以跨 worker 限流。会话为密码哈希签名的 12 小时 HttpOnly Cookie，换密码后
原会话失效。所有设置写入、测试和预览要求访问权限、同源以及管理员授权。

`serve` 启动独立调度子进程，Web worker 不注册计划；独立部署可运行 `scheduler`。
调度终身持有专用锁，执行时取得流水线锁并持久认领当天日期。忙时不认领，失败或
中断后当天不自动重复；重启只补当天任务。选择多个方向时一次合并摘要需求，保持
共享深度摘要的复用。另有心跳线程，避免长模型调用使进程显示离线。

监听、证书、数据库路径和外部定时器归部署管理。启用应用计划前必须停用外部定时
入口并通过本机命令确认接管，网页不会用一次保存代替宿主机变更。

## 开发约定

1. 新来源返回规范记录，通过 `_store()` 入库；按组采集时必须同时记录 `item_group`。
2. 关联 `score`、`group_state`、`summary_group` 时必须包含组条件，防止重复行或读到其他方向的结果。
3. 邮件确认必须在事务提交之后，网络等待前应结束不必要的写事务。
4. 区分自动规则排除与用户忽略；收藏、已读维持全局语义。
5. 密钥通过 `config.read_secret()` 读取，不写入配置样例、源码或日志。
6. 跨组运行保留已成功的工作，失败记录需与 CLI 退出码一致；不能将告警一概理解为阶段失败。
7. 测试使用临时数据库和模拟外部服务。分组改动至少验证重叠成员、方向差异、旧库迁移与跨标签页操作等实际边界。

默认配置与相对数据路径基于 `config.ROOT` 解析，根目录由模块位置确定；凭据文件
随所选配置目录。参数字段见
[config.example.yaml](../config.example.yaml)，研究画像字段见
[interests.example.yaml](../interests.example.yaml)。
