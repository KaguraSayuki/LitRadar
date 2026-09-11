# LitRadar 📡

个人化学文献雷达 —— 把 **X-MOL 关键词订阅邮件** + **Crossref 关键词检索** 汇到一起,
用 **DeepSeek** 做个性化精排和中文结构化摘要,在**局域网网页**上看。

单用户自用,不对外分发。

---

## 数据从哪来

| 层 | 源 | 负责 | 成本 |
|---|---|---|---|
| 精选 | **X-MOL 订阅邮件** | 它替你按订阅词筛过的高精度结果(通常每次 2 条) | 免费 |
| 召回 A | **Crossref 关键词检索** | 模糊匹配,召回高、噪声大;覆盖白名单里的 24 本期刊 | 免费、无预算限制 |
| 召回 B | **Semantic Scholar `/paper/search/bulk`** | 精确 AND 查询,召回低但准确率高 | 免费(需 key) |
| 召回 C | **引用滚雪球**(`/paper/{id}/citations`) | 顺着基础文献的引用关系往下滚,捞关键词找不到的 | 免费(每种子 1 次请求) |
| 摘要 | **Semantic Scholar `/paper/batch`** | 按 DOI 补摘要 —— 实测 ACS 系期刊 4/4 都能补到 | 免费 |
| 精排 | **DeepSeek `deepseek-chat`** | 打分 + 给"为什么推给你"的理由 | 极低 |
| 期刊等级 | **easyScholar 开放接口** | 影响因子、中科院分区、北核、CSCD 等 | 按次计额,结果本地缓存 |

### 两条召回腿为什么互补

```
Crossref  模糊相关度匹配       单查询约 60-100 篇   噪声大
S2 bulk   精确 AND 查询        单查询约 3-100 篇    准确率高
```

两者结果用 **DOI 合并**,所以同一条文献不会重复,而两条腿各自的盲区能被对方补上。

> ⚠️ **S2 bulk 必须用查询语法**,否则**静默返回 0 条**:
> ```
> "N-H insertion" + diazo + aniline      -> 命中 3      ✅
> diazo carbene N-H insertion aniline    -> 命中 0      ❌ 裸词被当短语
> ```
> 语法:`+` = AND,`|` = OR,双引号 = 短语,`-` = 排除。
> 所以画像里 Crossref 用 `search_queries`(自然语言),S2 用 `s2_queries`(查询语法),
> 两者分开配置 —— **不能共用一份检索词**。

> ⚠️ **关于 OpenAlex**:2026 年起已改为 API Key + 额度制,未配 key 会直接返回
> `Insufficient budget`。因此默认关闭;若你有 key,在 `.env` 里配 `OPENALEX_API_KEY`
> 并把 `sources.openalex_enabled` 设为 `true` 即可启用。
>
> 摘要之所以不依赖 OpenAlex,是因为 **Semantic Scholar 免费且 ACS 覆盖完整**——
> 这是实测结论,不是假设。

### 为什么不抓 X-MOL 网站

X-MOL 的 `robots.txt` 明确禁止爬取检索页:

```
Disallow: /paper/search
Disallow: /paper/journal
```

且全站部署了阿里云人机验证。所以本项目**只解析你自己收到的订阅邮件**,
不碰它的网站。X-MOL 自带的「私人定制」订阅请照常使用,它负责广度。

### 专利

当前数据源**不覆盖专利**(X-MOL 和 Crossref/S2 都不含专利)。`/patents` 页面
保留了框架。要监控专利可接 EPO OPS / PatentsView / Lens.org(均免费)。
注意专利去重必须按**专利族**而非公开号,且有 18 个月公开延迟。

---

## 快速开始

```bash
cd /srv/Work/LitRadar

# 1. 依赖(已装好,重装用这个)
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 配置
cp config.example.yaml config.yaml
cp .env.example .env && chmod 600 .env
#   -> 填 DEEPSEEK_API_KEY / IMAP_PASSWORD(可选 LITRADAR_TOKEN)

# 3. 初始化
.venv/bin/python -m litradar.cli init-db

# 4. 把 X-MOL 订阅邮件导出为 .eml 放进 data/inbox/

# 5. 跑一次完整流水线
.venv/bin/python -m litradar.cli run

# 6. 起服务
.venv/bin/uvicorn litradar.web.app:app   # 默认只绑 127.0.0.1
```

浏览器打开 `http://<局域网IP>:8080`。

---

## 邮件怎么接进来

### 现状:Outlook 个人账号不能用密码登 IMAP

实测 `outlook.office365.com:993` 的能力声明是:

```
* CAPABILITY IMAP4 IMAP4rev1 AUTH=XOAUTH2 LOGINDISABLED ...
```

`LOGINDISABLED` 表示密码登录已被明确禁用,只能用 OAuth2。

### 推荐做法:Outlook 转发到 QQ 邮箱

> ⚠️ **不要用 Gmail。** 实测这台机器上 `imap.gmail.com` 的 DNS 被透明代理拦到
> `198.18.x.x`,SSL 握手直接 EOF,连不上。
> 实测**可用**的:`imap.qq.com` / `imap.163.com` / `imap.126.com` /
> `imap.feishu.cn` / `imap.exmail.qq.com`,都支持密码/授权码登录。

**① 拿 QQ 邮箱授权码**

QQ 邮箱网页版 → 设置 → 账户 → 「POP3/IMAP/SMTP服务」→ 开启 IMAP/SMTP
→ 按提示发短信 → **生成授权码**(16 位,只显示一次)

**② 在 Outlook 建转发规则**

设置 → 邮件 → 规则 → 新建:条件「发件人地址包含 `newsletter.x-mol.com`」
→ 操作「转发到 `你的QQ号@qq.com`」

**③ 填进配置**

```yaml
# config.yaml
mail:
  mode: "imap"
  imap_host: "imap.qq.com"
  imap_user: "你的QQ号@qq.com"
  imap_password_env: "IMAP_PASSWORD"
```

```bash
# .env(填授权码,不是 QQ 密码)
IMAP_PASSWORD=你生成的16位授权码
```

**④ 验证(只读,不标记已读、不改动邮件)**

```bash
.venv/bin/python -m litradar.cli mail-test
```

它会连上去、报告找到几封 X-MOL 邮件,并**实际解析一封**给你看提取出的标题与
DOI。连不上或解析不出会分别给出对应排查方向。

### 备选:本地文件夹模式(零配置)

`mail.mode: "folder"`(默认)。把邮件导出成 `.eml` 丢进 `data/inbox/`,
跑一次流水线,处理完会自动移到 `data/inbox/processed/`。

---

## 中文输出

界面是中文优先的:

- **中文标题** —— 由 LLM 翻译,专业术语保留英文(`aza-Claisen`、`NHC`、`P(V)` 等不硬译)。
  卡片上中文当主标题,**英文原名以小字副标题保留**,方便你去搜原文
- **中文摘要** —— 前 `deep_summary_top_n`(默认 8)篇是深度摘要
  (问题/方法/关键结果/局限/对你的用处),其余是「中文标题 + 一句话结论」
- **防幻觉** —— `key_results` 里出现的每个数字都会与英文原文逐字比对,
  对不上会标注 `⚠️(数字 X 未在原文中找到,请核对)`
- **允许标注推断** —— 摘要通常不直说"解决了什么问题",这类字段允许合理推断
  但会标 `(推断)`,与原文事实区分开

重新生成:

```bash
.venv/bin/python -m litradar.cli summarize --force     # 重做全部
.venv/bin/python -m litradar.cli summarize             # 只补缺失的(默认)
```

---

## 常用命令

```bash
.venv/bin/python -m litradar.cli init-db        # 建库
.venv/bin/python -m litradar.cli parse          # 只解析邮件,校准解析器用
.venv/bin/python -m litradar.cli ingest all     # 采集(邮件 + 关键词检索)
.venv/bin/python -m litradar.cli enrich         # 富化:补摘要/引用数
.venv/bin/python -m litradar.cli rank           # 排序
.venv/bin/python -m litradar.cli summarize      # 生成摘要
.venv/bin/python -m litradar.cli run            # 上面全部
.venv/bin/python -m litradar.cli stats          # 统计
.venv/bin/python -m pytest tests/ -q            # 跑测试
```

网页的「统计」页也能一键手动触发各阶段。

---

## 引用滚雪球

前两条召回腿都靠**词表**。滚雪球靠**引用关系** —— 它的价值恰恰在于发现
"换了说法"的新工作,那正是关键词召回天然的盲区。实测它捞回一篇 ACS Catalysis
的工作,关键词一条查询都没命中,精排直接排到全库第 6。

种子写在 `interests.yaml` 的 `seed_dois`(已从开题报告的参考文献里挑了 15 篇)。
**种子年份很关键**:近一两年的新论文被引 0–1 次,滚不出东西;要选 2014–2023
这种每年还有十几次被引的。

做法上有两个不显然的决定:

**一、每轮只刷新 5 个种子,关系落表累积。**
S2 免费 key 名义 1 req/s,实测连打十几个种子有一半会 429。而共被引计数一旦
丢种子就会静默漏判(只被那失败种子引用的论文永远凑不够票)。所以引用关系写进
`seed_cite` 表,轮着刷新最久没查的种子,共被引在**全部种子、全部历史轮次**上统计。

**二、共被引当质量标注,不当闸门。**
一开始我把它当精度闸门(≥2 个种子引用才收),实测太狠:严格按日期过滤后,
15 个种子全查一遍总共才 ~40 条候选,而门槛 2 挡掉的 29 条里有 18 条标题明显对口。
所以默认 `snowball_min_cocitations: 1`(全收),共被引数写进 `source_ref`,
卡片上显示成 `滚雪球 ×2` —— 数字越大越可能是同一条脉络里的工作。

三个来源实测的精度对比(LLM 精排≥60 分占比):

| 来源 | 条数 | ≥60 | 中位分 | 最高 |
|---|---|---|---|---|
| Crossref 关键词 | 156 | 2 (1.3%) | 17 | 72 |
| S2 关键词 | 36 | 14 (39%) | 56 | 90 |
| 滚雪球 | 27 | 2 (7.4%) | 31 | 77 |
| 滚雪球(×2 那 4 条) | 4 | 2 (50%) | — | 77 |

---

## 排序原理

三阶段漏斗,`最终分 = 0.85×LLM + 0.10×BM25 + 0.05×规则`:

1. **规则过滤** —— 命中 `negative` 直接丢弃;命中核心关键词 / 期刊白名单 /
   X-MOL 红色高亮词 / 关注作者则加分
2. **BM25 粗排** —— **只给顺序,不截断**。粗排的信号强度远低于 LLM,
   让它有"一票否决权"是本末倒置:实测被卡在 53 / 96 / 108 名的三篇
   从此永远是"未评分"。候选池只有 ~200 条,全部送进 LLM 也才 10 次调用。
   (需要限量试跑时把 `llm.rerank_top_k` 设成正数即可恢复截断。)
   **不用向量检索**,因为 DeepSeek 不提供 embedding API,
   而引入本地 torch 模型对自用工具太重
3. **LLM 精排** —— 分批独立打分,每批 20 篇,输出分数 + 中文理由。
   批次之间用同一套评分标准,所以扩大覆盖不会互相干扰

**时间窗只有一个来源**:`app.pipeline_window_days`(默认 200)。
定时任务、命令行 `--days`、网页上的"排序/摘要"按钮都读它。
它必须 ≥ 抓取窗口(`sources.s2_search_lookback_days`),否则抓回来的文献
进了库却落在排序窗口之外,永远拿不到分数,在收件箱里长成一片"未评分"。

期刊匹配做了缩写归一:`Org. Lett.` ↔ `Organic Letters`、
`Angew. Chem. Int. Ed.` ↔ `Angewandte Chemie International Edition` 都能对上
(用首字母串比对,有测试覆盖)。

---

## 期刊等级(影响因子 / 分区)

影响因子和分区是**付费专有数据**(Clarivate JCR / 中科院文献情报中心),
Crossref 和 Semantic Scholar 都不提供 —— 这也是为什么早先的 IF 标签
只出现在 X-MOL 来的那两条上,看着像随机出现。

现在走 [easyScholar 开放接口](https://www.easyscholar.cc),按**刊名**查,
结果缓存进 `journal_rank` 表:全库几十本刊查一遍就够,之后不再消耗额度。

```bash
# 密钥写进 .env(不写进 config.yaml)
echo 'EASYSCHOLAR_SECRET_KEY=你的密钥' >> .env
.venv/bin/python -m litradar.cli enrich --limit 0   # 只补期刊等级,不动条目
```

卡片上是压缩过的短标签,**规则在 `config.yaml` 的 `journal_rank` 下**:

```yaml
journal_rank:
  fields: [sciwarn, sci, sciUp, sciif, pku, cssci]   # 只展示这些
  map:
    北大中文核心: 北核        # 字段名 → 标签名;留空 = 只显示值
    SCI: ""
    "/化学(\\d+)区/": "化$1"   # /正则/ 作用于值,把"化学1区"压成"化1"
  aliases:                   # 来源给的短名/罗马字名 → easyScholar 认的全名
    "Youji huaxue": "Chinese Journal of Organic Chemistry"
```

三类规则各管各的,不会互相打架:

| 写法 | 作用对象 | 例子 |
|---|---|---|
| `字段显示名: 短标签` | 标签名 | `北大中文核心` → `北核` |
| `字段显示名: ""` | 去掉标签名,**只留值** | `SCI: ""` 把 `SCI Q1` 变成 `Q1` |
| `"/正则/": "替换"` | **值**,`$1` 是捕获组 | `化学1区` → `化1` |

> ⚠️ 空标签**不等于**隐藏字段 —— 把字段整个去掉要从 `fields` 里删。
> 否则用户列出的 6 个字段里有一半在 map 里是空的,一"隐藏"就全没了。

刊名在入库时统一清洗(`&amp;` → `&`、换行 → 空格)。这不只是显示问题:
统计页按刊名分组时,带换行的 JACS 会裂成两行,按刊名查等级也直接查不到。

---

## 检索词与偏好

`interests.yaml`,网页 `/interests` 也能直接编辑(保存前校验 YAML)。
**这不是账号,就是个配置文件** —— 单用户,只有一份。

关键字段:

| 字段 | 作用 |
|---|---|
| `search_queries` | **检索关键词列表,必须英文**。多条查询取并集 |
| `exclude_title_prefixes` | 按标题前缀过滤非论文记录(同行评审、更正声明) |
| `keywords.core` / `bonus` | 规则打分 |
| `keywords.current_challenges` | 你当前的实验卡点 —— 能对症的论文会被 LLM 显著加权 |
| `keywords.boost_topics` | 希望优先命中的主题 |
| `negative` | 排除词,命中直接丢弃 |
| `journals.core` / `ok` + `issn` | 期刊白名单(ISSN 已通过 Crossref 逐本核验) |
| `authors_watch` | 重点关注作者 |

### 为什么要用「多条查询取并集」

实测(45 天窗口、按 DOI 去重):

```
单条宽查询        → 100 篇
4 条查询并集      → 186 篇   (+86%)
```

收益在**召回**,不在精确率 —— 拆开后单条的精确率反而略降。所以策略是
**宁可多拉,精确交给 LLM 精排**。

顺带实测的另一个结论:换检索字段没用,反而更差。

```
query.bibliographic 宽查询   标题含 ≥2 个核心概念: 21/60   ← 现用
query.title         宽查询   标题含 ≥2 个核心概念: 15/60   ← 更差
query.title         精确短句 标题含 ≥2 个核心概念: 4–18/60
```

所以继续用 `query.bibliographic`,通过**增加查询条数**而不是改动单条精度来扩召回。

---

## 部署(systemd)

三个单元都是**模板单元**,`%i` 是运行用户,所以仓库里不出现任何用户名。

```bash
cd /srv/Work/LitRadar

# ⚠️ 三个单元都必须是模板名(带 @),少一个 @ 就会让 %i 为空而启动失败
sudo cp deploy/litradar-web.service        /etc/systemd/system/litradar@.service
sudo cp deploy/litradar-daily@.service     /etc/systemd/system/
sudo cp deploy/litradar-daily@.timer       /etc/systemd/system/

sudo systemctl daemon-reload

# 把 <你的用户名> 换成 whoami 的结果
sudo systemctl enable --now litradar@<你的用户名>.service
sudo systemctl enable --now litradar-daily@<你的用户名>.timer

# 查看
systemctl status litradar@<你的用户名>.service
systemctl list-timers 'litradar*'
journalctl -u litradar@<你的用户名>.service -f
```

> ⚠️ `litradar-daily@.service` 用了 `User=%i`,**必须**以模板实例名安装。
> 直接拷成 `litradar-daily.service` 会让 `%i` 为空,systemd 拒绝启动。

对外访问由 nginx 反代,见 `deploy/litradar-nginx.conf`(应用只绑 `127.0.0.1:8090`)。

---

## 合规与安全

- **不要做公网端口转发。** 只绑局域网。X-MOL 邮件内容属于你自己,但公网暴露会
  把它变成对非授权用户的再分发。
- **没有账号体系。** 默认只绑 `127.0.0.1`。要绑 `0.0.0.0` 就给接口加个口令:
  在 `.env` 设 `LITRADAR_TOKEN=xxx`,访问时带 `?k=xxx`。
  不设的话同网段任何人都能调 `/admin/run/*` **花掉你的 DeepSeek 额度**。
- 本工具**不抓取 X-MOL 网站**,只解析你自己邮箱里的订阅邮件。
- `.env` 权限设为 `600`,已在 `.gitignore` 中排除。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| X-MOL 邮件只有 2 条 | 它是 teaser,"还有更多…请移步 x-mol.com"。全量靠 Crossref 补 |
| Semantic Scholar 限流 | 无 API Key 时约 1 req/s 且可能 429。免费申请 key 可显著提高额度 |
| Crossref 摘要不全 | ACS 系期刊常缺摘要 —— 这正是引入 S2 的原因 |
| 专利未覆盖 | 见上文「专利」一节 |
| 阿拉伯数字核验 | 摘要里 `key_results` 的数字会在原文中比对,对不上会标 ⚠️,但不保证覆盖全部幻觉 |

---

## 目录结构

```
litradar/
├── litradar/
│   ├── config.py         配置加载(密钥只走环境变量)
│   ├── db.py             SQLite schema 与读写
│   ├── normalize.py      DOI/标题/日期/作者归一化
│   ├── http.py           统一 UA、限流、重试
│   ├── sources/
│   │   ├── xmol_email.py       ★ X-MOL 邮件解析(有回归测试)
│   │   ├── mail.py             邮件接入:folder / maildir / imap
│   │   ├── crossref_search.py  关键词检索主力
│   │   ├── semanticscholar.py  摘要主力
│   │   └── openalex_search.py  可选(需 key)
│   ├── enrich.py         富化编排
│   ├── rank.py           三阶段排序
│   ├── summarize.py      结构化中文摘要 + 数字核验
│   ├── llm.py            DeepSeek 客户端
│   ├── pipeline.py       流水线编排
│   ├── cli.py            命令行
│   └── web/              FastAPI + Jinja2(零前端构建)
├── interests.yaml        检索词与偏好(唯一配置文件)
├── fixtures/             解析器回归基准(真实 .eml)
├── tests/                36 个测试
├── deploy/               systemd units
└── docs/litradar-design.md   完整技术方案文档
```

---

## 反馈闭环

雷达页每条都有 `⭐收藏` / `✅已读` / `✕不感兴趣`,全部落 `feedback` 表。

三个动作的去向是**分开**的,不是笼统地"隐藏":

| 动作 | 去哪 | 说明 |
|---|---|---|
| 已读 | 未读 → 已读 | "已读"里只留你还没否决的 |
| 收藏 | 收藏页签 | **免疫规则过滤** —— 之后收紧检索词也不会把它吃掉 |
| 不感兴趣 | 不感兴趣页签 | 从 未读 / 已读 / 全部 三个视图同时消失,可在页签里逐条恢复 |

被否决的条目标着红色 `已否决`,卡片整体压暗;详情页再补一段说明
"它为什么不在正常列表里"。这样任何时候都不会出现"东西怎么不见了"。

**这是整个系统里唯一会随时间变好的部分。** 攒够几十条后,可以把"已收藏"的
条目作为精排 prompt 的 few-shot 示例,或者用它们调 `negative` 词表。
建议早用早攒。
