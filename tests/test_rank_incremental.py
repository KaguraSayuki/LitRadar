"""Incremental scoring uses synthetic records and a local model double only."""
import re
import sqlite3

import pytest

from litradar import db, rank, ranking_state
from litradar.config import Config
from litradar.llm import LLMError


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "synthetic.db")
    cfg.interests_data = {"groups": [{"slug": "a", "name": "Synthetic A",
                                     "direction": "Synthetic preference A"}]}
    cfg.llm.rerank_batch_size = 2

    class Model:
        available = True
        calls = []
        missing = set()
        fail = False
        interrupt_at = None
        inspect = None

        def json(self, system, prompt, **kwargs):
            numbers = [int(n) for n in re.findall(r"^\[\d+\] Synthetic paper (\d+)", prompt, re.M)]
            self.calls.append(numbers)
            if self.inspect:
                self.inspect()
            if len(self.calls) == self.interrupt_at:
                raise KeyboardInterrupt
            if self.fail:
                raise LLMError("Synthetic failure")
            return {"scores": [{"id": i + 1, "score": 80, "reason": "Synthetic assessment"}
                               for i, n in enumerate(numbers) if n not in self.missing]}

    model = Model()
    monkeypatch.setattr(rank, "LLMClient", lambda *a, **kw: model)
    return cfg, model


def seed(cfg, numbers, *, group="a", old_date=False):
    conn = db.Database(cfg.db_file).connect()
    gids = db.sync_groups(conn, rank.load_groups(cfg))
    ids = {}
    for n in numbers:
        title = f"Synthetic paper {n}"
        iid, _ = db.upsert_item(conn, {"kind": "paper", "dedup_key": f"synthetic:{n}",
            "title": title, "title_norm": title.lower(), "source": "test",
            "published_at": "2001-01-02" if old_date else "2026-01-02"})
        db.add_to_group(conn, gids[group], iid)
        ids[n] = iid
    conn.commit()
    conn.close()
    return ids


def scores(cfg, group="a"):
    conn = db.Database(cfg.db_file).connect()
    try:
        return {r["item_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM score WHERE group_id=?", (db.group_id(conn, group),))}
    finally:
        conn.close()


def run(cfg, **kwargs):
    result = rank.run(cfg, verbose=False, **kwargs)
    assert result["errors"] == 0
    return result


def called(model):
    return {n for batch in model.calls for n in batch}


def set_old_scores(cfg, ids, values):
    conn = db.Database(cfg.db_file).connect()
    for n, value in values.items():
        conn.execute("UPDATE score SET final_score=? WHERE group_id=? AND item_id=?",
                     (value, db.group_id(conn, "a"), ids[n]))
    conn.commit()
    conn.close()


def test_only_new_members_are_scored_without_date_or_legacy_top_k_limits(run_env):
    cfg, model = run_env
    cfg.llm.rerank_top_k = 1
    seed(cfg, range(1, 6), old_date=True)
    assert run(cfg, days=1)["llm_new"] == 5
    assert called(model) == {1, 2, 3, 4, 5}
    before = scores(cfg)
    model.calls.clear()
    assert run(cfg)["llm_reused"] == 5
    assert model.calls == []
    assert {i: r["llm_scored_at"] for i, r in scores(cfg).items()} == {
        i: r["llm_scored_at"] for i, r in before.items()}
    seed(cfg, [6, 7], old_date=True)
    assert run(cfg)["llm_new"] == 2
    assert called(model) == {6, 7}


def test_two_hundred_cached_papers_plus_twenty_new_need_one_batch(run_env):
    cfg, model = run_env
    cfg.llm.rerank_batch_size = 20
    seed(cfg, range(200))
    assert run(cfg)["llm_new"] == 200
    assert len(model.calls) == 10
    seed(cfg, range(200, 220))
    model.calls.clear()
    result = run(cfg)
    assert result["llm_new"] == 20 and result["llm_reused"] == 200
    assert len(model.calls) == 1 and called(model) == set(range(200, 220))


def test_keyword_change_uses_old_threshold_and_still_scores_all_new(run_env):
    cfg, model = run_env
    ids = seed(cfg, [1, 2, 3])
    run(cfg)
    set_old_scores(cfg, ids, {1: 70, 2: 69.9, 3: 95})
    cfg.interests_data["groups"][0].update(rerank_min_score=70, keywords={"core": ["synthetic"]})
    seed(cfg, [4, 5])
    model.calls.clear()
    result = run(cfg)
    assert called(model) == {1, 3, 4, 5}
    assert result["llm_new"] == 2 and result["llm_refreshed"] == 2
    current = ranking_state.preference_hash(rank.load_groups(cfg)[0])
    saved = scores(cfg)
    assert saved[ids[2]]["preference_hash"] != current
    assert saved[ids[1]]["preference_hash"] == current
    model.calls.clear()
    run(cfg)
    assert model.calls == []


def test_top_n_is_frozen_across_failures_and_does_not_expand(run_env):
    cfg, model = run_env
    ids = seed(cfg, [1, 2, 3, 4])
    run(cfg)
    old = scores(cfg)
    set_old_scores(cfg, ids, {1: 95, 2: 90, 3: 89, 4: 20})
    cfg.interests_data["groups"][0].update(direction="Synthetic preference B", rerank_policy="top_n", rerank_top_n=2)
    model.calls.clear()
    model.missing = {2}
    result = run(cfg)
    assert called(model) == {1, 2}
    assert result["llm_failed"] == 1 and result["llm_pending"] == 1
    assert scores(cfg)[ids[2]]["preference_hash"] == old[ids[2]]["preference_hash"]
    assert scores(cfg)[ids[2]]["llm_score"] == 80
    # A different paper now leads the saved ranking; it is outside the frozen plan.
    set_old_scores(cfg, ids, {3: 100})
    model.calls.clear()
    model.missing.clear()
    assert run(cfg)["llm_refreshed"] == 1
    assert called(model) == {2}
    model.calls.clear()
    run(cfg)
    assert model.calls == []


def test_every_successful_batch_is_durable_before_next_request(run_env):
    cfg, model = run_env
    seed(cfg, [1, 2, 3, 4])
    def inspect():
        if len(model.calls) == 2:
            assert sum(r["llm_score"] is not None for r in scores(cfg).values()) == 2
    model.inspect = inspect
    model.interrupt_at = 2
    with pytest.raises(KeyboardInterrupt):
        run(cfg, force=True)
    assert len(scores(cfg)) == 2
    first = set(model.calls[0])
    model.inspect = None
    model.interrupt_at = None
    model.calls.clear()
    run(cfg)
    assert called(model) == {1, 2, 3, 4} - first


def test_force_failure_retains_old_results_and_normal_run_resumes(run_env):
    cfg, model = run_env
    seed(cfg, [1, 2, 3], old_date=True)
    run(cfg)
    before = scores(cfg)
    model.fail = True
    model.calls.clear()
    assert run(cfg, force=True)["llm_failed"] == 3
    for iid, previous in before.items():
        after = scores(cfg)[iid]
        for field in ("llm_score", "llm_reason", "llm_model", "preference_hash", "llm_scored_at"):
            assert after[field] == previous[field]
    model.fail = False
    model.calls.clear()
    assert run(cfg)["llm_refreshed"] == 3
    assert called(model) == {1, 2, 3}
    model.calls.clear()
    run(cfg)
    assert model.calls == []


def test_force_obeys_exclusions_and_does_not_delete_old_scores(run_env):
    cfg, model = run_env
    ids = seed(cfg, [1, 2, 3], old_date=True)
    run(cfg)
    cfg.interests_data["groups"][0]["negative_titles"] = ["paper 2"]
    model.calls.clear()
    run(cfg, force=True)
    assert called(model) == {1, 3}
    assert scores(cfg)[ids[2]]["llm_score"] == 80


def test_groups_are_independent_and_membership_is_new_per_group(run_env):
    cfg, model = run_env
    seed(cfg, [1, 2])
    run(cfg)
    cfg.interests_data["groups"].append({"slug": "b", "name": "Synthetic B"})
    seed(cfg, [1, 2], group="b")
    model.calls.clear()
    out = run(cfg)
    assert out["groups"]["a"]["llm_reused"] == 2
    assert out["groups"]["b"]["llm_new"] == 2
    cfg.interests_data["groups"][0].update(direction="Changed", rerank_policy="top_n", rerank_top_n=1)
    model.calls.clear()
    out = run(cfg)
    assert out["groups"]["a"]["llm_refreshed"] == 1
    assert out["groups"]["b"]["llm_refreshed"] == 0


def test_display_weights_feedback_model_and_metadata_do_not_trigger_refresh(run_env):
    cfg, model = run_env
    group = cfg.interests_data["groups"][0]
    group["keywords"] = {"core": ["alpha", "beta"]}
    ids = seed(cfg, [1, 2])
    run(cfg)
    group.update(name="Renamed", direction="  Synthetic   preference A  ")
    group["keywords"]["core"] = [" beta ", "ALPHA", "beta"]
    cfg.llm.model = "synthetic-other-model"
    cfg.ranking.w_llm = .7
    conn = db.Database(cfg.db_file).connect()
    db.set_action(conn, ids[1], "star")
    conn.execute("UPDATE item SET abstract='Synthetic new abstract' WHERE id=?", (ids[2],))
    conn.commit()
    conn.close()
    model.calls.clear()
    run(cfg)
    assert model.calls == []


@pytest.mark.parametrize("field,value", [("direction", "Changed"), ("negative", ["excluded"]),
    ("negative_titles", ["excluded"]), ("exclude_title_prefixes", ["excluded"]),
    ("authors_watch", ["Synthetic Author"]), ("journals", {"core": ["Synthetic Journal"]}),
    ("keywords", {"bonus": ["synthetic"]}), ("keywords", {"current_challenges": ["Synthetic question"]})])
def test_scoring_preferences_trigger_selected_refresh(run_env, field, value):
    cfg, model = run_env
    seed(cfg, [1])
    run(cfg)
    cfg.interests_data["groups"][0].update({field: value, "rerank_min_score": 0})
    model.calls.clear()
    run(cfg)
    assert called(model) == {1}


def test_disabled_llm_leaves_work_pending_until_enabled(run_env):
    cfg, model = run_env
    seed(cfg, [1, 2])
    cfg.interests_data["groups"][0]["llm_rank"] = False
    assert run(cfg, force=True)["llm_pending"] == 2
    assert model.calls == []
    assert all(r["llm_score"] is None and r["final_score"] is not None for r in scores(cfg).values())
    cfg.interests_data["groups"][0]["llm_rank"] = True
    assert run(cfg)["llm_new"] == 2


def test_upgrade_keeps_legacy_scores_without_claiming_current_version(run_env):
    cfg, model = run_env
    ids = seed(cfg, [1, 2])
    conn = db.Database(cfg.db_file).connect()
    gid = db.group_id(conn, "a")
    db.save_score(conn, ids[1], group_id=gid, llm_score=90, llm_reason="Synthetic legacy", final_score=85)
    for field in ("preference_hash", "evidence_hash", "feedback_hash", "llm_scored_at"):
        conn.execute(f"ALTER TABLE score DROP COLUMN {field}")
    conn.execute("DROP TABLE rank_state")
    conn.execute("DROP TABLE rank_pending")
    conn.execute("PRAGMA user_version=8")
    conn.commit()
    conn.close()
    db._READY.discard(str(cfg.db_file))
    result = run(cfg)
    assert called(model) == {2}
    assert result["llm_reused"] == 1
    legacy = scores(cfg)[ids[1]]
    assert legacy["llm_score"] == 90 and legacy["llm_reason"] == "Synthetic legacy"
    assert legacy["preference_hash"] is None and legacy["llm_scored_at"] is None
    conn = sqlite3.connect(cfg.db_file)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
