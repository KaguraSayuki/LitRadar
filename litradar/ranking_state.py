"""Per-group preference versions and durable, bounded reranking plans."""
from __future__ import annotations

import hashlib
import json


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def preference_hash(profile) -> str:
    """Ignore display changes, feedback and ordering within preference lists."""
    def text(value):
        return " ".join((value or "").split()).casefold()

    fields = ("core", "bonus", "negative", "negative_titles", "current_challenges",
              "boost_topics", "journals_core", "journals_ok", "authors_watch",
              "exclude_title_prefixes")
    value = {key: sorted({text(v) for v in getattr(profile, key, []) if text(v)})
             for key in fields}
    value["direction"] = text(profile.direction)
    return fingerprint(value)


def initialize(conn, group_id: int, profile) -> None:
    # A baseline detects future edits; it does not certify legacy scores.
    conn.execute("INSERT OR IGNORE INTO rank_state (group_id, preference_hash) VALUES (?,?)",
                 (group_id, preference_hash(profile)))


def prepare(conn, group_id: int, profile, eligible: set[int], scores: dict,
            *, force: bool = False) -> tuple[str, set[int]]:
    """Freeze historical targets before local scores change; resume only pending IDs."""
    initialize(conn, group_id, profile)
    current = preference_hash(profile)
    previous = conn.execute("SELECT preference_hash FROM rank_state WHERE group_id=?",
                            (group_id,)).fetchone()[0]
    if force or previous != current:
        if force:
            selected = eligible
        else:
            historical = [s for iid, s in scores.items()
                          if iid in eligible and s["llm_score"] is not None]
            historical.sort(key=lambda s: (-(s["final_score"] or 0), s["item_id"]))
            if profile.rerank_policy == "top_n":
                historical = historical[:profile.rerank_top_n]
            else:
                historical = [s for s in historical
                              if (s["final_score"] or 0) >= profile.rerank_min_score]
            selected = {s["item_id"] for s in historical}
        conn.execute("DELETE FROM rank_pending WHERE group_id=?", (group_id,))
        conn.executemany("INSERT INTO rank_pending (group_id,item_id,preference_hash) VALUES (?,?,?)",
                         [(group_id, iid, current) for iid in sorted(selected)])
        conn.execute("UPDATE rank_state SET preference_hash=? WHERE group_id=?",
                     (current, group_id))
    pending = {r[0] for r in conn.execute(
        "SELECT item_id FROM rank_pending WHERE group_id=? AND preference_hash=?",
        (group_id, current))} & eligible
    conn.commit()
    return current, pending
