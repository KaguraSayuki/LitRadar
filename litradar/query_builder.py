"""Translate ordinary term groups into source queries without losing legacy rules."""
from __future__ import annotations

import re

from .settings import SettingsError, lines

SOURCES = {"s2_queries": "Semantic Scholar", "s2_venue_queries": "Semantic Scholar 指定期刊",
           "search_queries": "Crossref / OpenAlex"}


def quote_term(term: str) -> str:
    return '"' + term.replace('\\', '\\\\').replace('"', '\\"') + '"'


def compile_rule(rule: dict) -> list[str]:
    required, any_terms, excluded = (rule.get(k, []) for k in ("all", "any", "exclude"))
    if not required and not any_terms:
        raise SettingsError("每条检索至少填写一个必须包含或任意包含的术语。", "queries")
    if rule["source"] == "search_queries":
        if excluded:
            raise SettingsError("Crossref / OpenAlex 的通用检索不保证排除语义。请将排除内容填入上方文献筛选，或选择 Semantic Scholar。", "queries")
        return [" ".join([*required, *alternative]) for alternative in
                ([ [term] for term in any_terms ] if any_terms else [[]])]
    parts = [quote_term(t) for t in required]
    if any_terms:
        parts.append("(" + " | ".join(quote_term(t) for t in any_terms) + ")")
    parts.extend("-" + quote_term(t) for t in excluded)
    return [" + ".join(parts)]


def parse_simple(query: str) -> dict | None:
    """Conservative migration: only accept a complete, understood expression.

    Unrecognised nesting, proximity/wildcard syntax and implicit operators stay
    opaque. Editing unrelated preferences never rewrites them.
    """
    token_re = re.compile(r'\s*("(?:\\.|[^"\\])*"|[()+|\-]|[^\s()+|"\-]+)')
    tokens = []
    position = 0
    while position < len(query.rstrip()):
        match = token_re.match(query, position)
        if not match:
            return None
        tokens.append(match.group(1))
        position = match.end()
    index = 0
    def term():
        nonlocal index
        if index >= len(tokens):
            raise ValueError
        value = tokens[index]
        index += 1
        if value in ('+', '|', '-', '(', ')'):
            raise ValueError
        if value.startswith('"'):
            return re.sub(r'\\(.)', r'\1', value[1:-1])
        if not re.fullmatch(r"[\w.]+", value):
            raise ValueError
        return value
    rule = {"all": [], "any": [], "exclude": []}
    try:
        while index < len(tokens):
            if tokens[index] == '(':
                if rule['any']:
                    return None
                index += 1
                rule['any'].append(term())
                while index < len(tokens) and tokens[index] == '|':
                    index += 1
                    rule['any'].append(term())
                if tokens[index] != ')':
                    return None
                index += 1
            elif tokens[index] == '-':
                index += 1
                rule['exclude'].append(term())
            else:
                rule['all'].append(term())
            if index < len(tokens):
                if tokens[index] != '+':
                    return None
                index += 1
                if index == len(tokens):
                    return None
        return rule if rule['all'] or rule['any'] else None
    except (ValueError, IndexError):
        return None


def rows_for(entry: dict) -> tuple[list[dict], list[dict]]:
    rows, opaque = [], []
    metadata = entry.get('query_editor') or []
    if not isinstance(metadata, list):
        metadata = []
    for source in SOURCES:
        queries = entry.get(source) or []
        if source == "search_queries" and not queries and entry.get("search_query"):
            queries = [entry['search_query']]
        for index, query in enumerate(queries):
            identifier = f"{source}:{index}"
            rule = None
            for candidate in metadata:
                if not isinstance(candidate, dict) or candidate.get('source') != source or candidate.get('query') != query:
                    continue
                if not all(isinstance(candidate.get(k),list) and all(isinstance(v,str) for v in candidate[k])
                           for k in ('all','any','exclude')):
                    continue
                try:
                    if compile_rule(candidate) == [query]:
                        rule = {k:candidate[k] for k in ('all','any','exclude')}
                        break
                except SettingsError:
                    pass
            if rule is None and source != "search_queries":
                rule = parse_simple(query)
            if rule:
                rows.append({**rule, "source": source, "original": identifier})
            else:
                opaque.append({"id": identifier, "source": source, "number": index + 1,
                               "label": SOURCES[source]})
    return rows, opaque


def parse_form(form, entry: dict) -> dict:
    if "queries_present" not in form:
        return {}
    original_rows, opaque = rows_for(entry)
    all_originals = {r['original']: r for r in original_rows}
    preserved = {name: [] for name in SOURCES}
    removed = set(form.getlist("remove_query"))
    valid_ids = {r['id'] for r in opaque}
    if removed - valid_ids:
        raise SettingsError("旧检索条件已变化，请重新载入此方向。", "queries", 409)
    for old in opaque:
        if old['id'] in removed:
            continue
        source, index = old['source'], old['number'] - 1
        queries = entry.get(source) or ([entry['search_query']] if source == 'search_queries' and entry.get('search_query') else [])
        preserved[source].append(queries[index])
    sources = form.getlist("query_source")
    metadata = []
    if len(sources) > 30:
        raise SettingsError("最多设置 30 条检索条件。", "queries")
    for i, source in enumerate(sources):
        if source not in SOURCES:
            raise SettingsError("请选择检索来源。", "queries")
        row = {'source': source}
        for key in ('all', 'any', 'exclude'):
            values = form.getlist('query_' + key)
            row[key] = lines(str(values[i])) if i < len(values) else []
            if len(row[key]) > 20 or any(len(t) > 200 for t in row[key]):
                raise SettingsError("每组最多 20 个术语，每个术语不超过 200 个字。", "queries")
        if not any(row[k] for k in ('all', 'any', 'exclude')):
            continue
        ids = form.getlist('query_original')
        old = all_originals.get(ids[i] if i < len(ids) else '')
        if old and all(old[k] == row[k] for k in ('source', 'all', 'any', 'exclude')):
            compiled = [entry[source][int(old['original'].split(':')[1])]]
        else:
            compiled = compile_rule(row)
        preserved[source].extend(compiled)
        for query in compiled:
            editor = dict(row)
            if source == 'search_queries' and row['any']:
                editor = {**row, 'all':[query], 'any':[]}
            metadata.append({**editor, 'query':query})
    # Clear the old fallback only after moving its exact value into the list.
    return {**preserved, 'search_query': '', 'query_editor':metadata}


def rows_from_form(form) -> list[dict]:
    rows = []
    for index, source in enumerate(form.getlist("query_source")):
        row = {"source": source}
        for key in ("all", "any", "exclude", "original"):
            values = form.getlist("query_" + key)
            value = str(values[index]) if index < len(values) else ""
            row[key] = value if key == "original" else lines(value)
        rows.append(row)
    return rows
