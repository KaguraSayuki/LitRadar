# Semantic Scholar API Key 申请参考

以下英文草稿按 LitRadar 当前代码的用途整理，适用于个人、自托管的文献追踪场景。
提交前请核对实际用途，并替换方括号中的用量占位符。本文不代表已提交的申请，也不
预设账号获批后的额度。

项目地址可填写 [LitRadar 仓库](https://github.com/KaguraSayuki/LitRadar)。下文的端点和
请求行为对应 [Semantic Scholar 客户端](../litradar/sources/semanticscholar.py)、
[采集流程](../litradar/pipeline.py) 和 [元数据补全流程](../litradar/enrich.py)。

## 1. How do you plan to use Semantic Scholar API in your project?

I use LitRadar, a self-hosted, single-user literature monitoring tool, to support my
chemistry research. The application combines my X-MOL subscription emails with
academic metadata APIs and deduplicates papers by DOI in a local SQLite database.

Semantic Scholar serves three purposes: discovering papers through Boolean queries,
following papers that cite selected seed publications, and enriching records with
abstracts, citation counts, and open-access links. I can configure several research
directions, each with its own queries and seed papers, while sharing the underlying
paper records.

The application requests bibliographic metadata and abstracts. It does not download
PDFs or full-text articles. Results are stored in my self-hosted database and viewed
through a personal web interface. When I enable the LLM features, selected titles,
abstracts, and research preferences are sent to a configured external model API for
relevance ranking and Chinese summaries; this is inference, not model training.

To limit requests, the application batches DOI enrichment in groups of 100, reuses
stored metadata, limits search pages, and refreshes a bounded number of seed papers
per run. Requests within a pipeline run are sequential, with configurable spacing
and backoff for rate-limited responses. Records with missing abstracts may be retried,
so caching reduces repeated requests but does not eliminate them entirely.

## 2. Which endpoints do you plan to use?

| Endpoint | Use in LitRadar |
|---|---|
| `GET /graph/v1/paper/search/bulk` | Boolean queries for each research direction, with year filters and bounded pagination |
| `GET /graph/v1/paper/{paper_id}/citations` | Forward citation discovery from seed papers; the client can supply a DOI reference |
| `POST /graph/v1/paper/batch` | Shared DOI enrichment, using batches of 100 records |
| `GET /graph/v1/paper/DOI:{doi}` | Diagnostic single-paper requests; a single-paper client helper is also available |

Requested fields depend on the endpoint and include titles, abstracts, citation
counts, venues, publication dates, external identifiers, and open-access links.
Supported requests also include authors and TLDR metadata. The bulk search client
uses a separate field list and does not request TLDR. LitRadar does not use the
dataset download endpoints.

## 3. How many requests per day do you anticipate using?

```text
Expected daily usage: [replace with your estimate] requests per day
Initial backfill:    [replace with your estimate] requests in total
Scheduled runs:      [replace with your planned frequency]
```

The estimate includes discovery queries, citation pages, DOI enrichment, diagnostics,
and retries. The current default pipeline interval is five seconds between Semantic
Scholar requests. I will adjust the configuration and workload to the limits granted
to my account. Initial backfills or manual reruns may temporarily increase daily use.

## 用量估算方法

不要直接沿用早期“每日 1–2 次请求”的估算：那只覆盖少量 DOI 补全，没有计入目前的
检索、被引追踪、多组运行和重试。

可按每轮运行估算，再乘以每日运行次数：

```text
每轮请求数 ≈ 各组检索所需页数之和
           + 各组本轮种子被引记录所需页数之和
           + 向上取整（全局待补全 DOI 数 / 100）
           + 诊断与重试请求
```

例如，假设启用 2 个组，每组执行 4 条检索式、每条 1 页，刷新 5 个种子、每个 1 页，
另有 150 个待补全 DOI，则每轮约为 `2 × 4 + 2 × 5 + 2 = 20` 次请求，尚未计入
诊断与重试。这只是演示计算方法，不是实际使用记录。

估算时核对 `s2_queries`、`s2_venue_queries`、`s2_search_max_pages`、
`snowball_seeds_per_run`、`snowball_per_seed` 和定时频率。初次补全与手动重跑应单独
留出余量；请求间隔配置不等同于每日配额。

若表单询问机构、商业用途或数据使用范围，请按自己的真实情况填写。申请通过后，将
密钥写入 `.env` 的 `S2_API_KEY`，不要提交到版本库。
