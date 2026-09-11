# Semantic Scholar API Key 申请表 —— 草稿

> 三个必填项的英文草稿。数字按当前实际实现估算,如果你觉得偏差大可以自行调整。
> 项目地址/仓库可填你的本地路径或写 "personal, not published"。

---

## 1. How do you plan to use Semantic Scholar API in your project? (50 words or more)

I am building a personal, single-user literature-alerting tool for my own chemistry
research (an organic synthetic-methodology topic; the specific field is not relevant
to this request, only that it relies on journal metadata rather than full text). The pipeline collects new papers from Crossref keyword queries and
my own X-MOL subscription e-mails, de-duplicates them by DOI, then uses Semantic
Scholar to fill in the **abstract** and citation count. This matters because Crossref
frequently omits abstracts for ACS journals (JACS, Org. Lett., J. Org. Chem.), so
Semantic Scholar is the primary source for that field. Records are then ranked and
summarised locally.

I use `POST /graph/v1/paper/batch` as the primary endpoint, passing DOIs in chunks of
100, with `GET /graph/v1/paper/DOI:{doi}` only as a single-record fallback. Requested
fields: `title, abstract, tldr, citationCount, venue, year, publicationDate,
externalIds, openAccessPdf`.

There is exactly one user — me. Volume is small: roughly 50–150 new papers per day, so
**1–2 batch requests per day in steady state**. To stay efficient I batch aggressively,
cache every result locally in SQLite so a DOI is never requested twice, issue requests
strictly sequentially with **no concurrency**, and back off on HTTP 429. I never fetch
paper text or PDFs, only metadata and abstracts.

---

## 2. Which endpoints do you plan to use?

```
POST /graph/v1/paper/batch          (primary — up to 100 DOIs per request)
GET  /graph/v1/paper/DOI:{doi}      (single-record fallback only)
```

I do **not** plan to use `/paper/search` or any bulk-dump endpoint — discovery is
handled by Crossref, and Semantic Scholar is used purely for per-DOI enrichment.

---

## 3. How many requests per day do you anticipate using?

```
Steady state : 1–2 requests/day   (one batch of 100 DOIs covers a whole day's intake)
Typical busy : 3–5 requests/day   (re-enriching records that arrived without abstracts)
Peak         : ~30–50 requests    (one-off initial backfill of an existing library)
```

Requests are issued **sequentially with a delay between them**, so the sustained rate
stays below **1 request/second** and usually far below it. Because results are cached
locally and only uncached DOIs are ever requested, the request count scales with *new*
literature, not with total library size.

---

## 备注

- 如果你的实际用量预计更大(比如打算导入上千篇历史文献),把 Peak 那行改成
  `~100–200 requests (one-off backfill)`,并说明是一次性的。
- 表单里若问 "institutional or personal",选 **personal / academic research**。
- 如果问是否商用:否,纯个人科研自用,不对外提供服务、不二次分发数据。
