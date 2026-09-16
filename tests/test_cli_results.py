"""分组失败可继续其它工作,但 CLI 退出状态必须反映已报告的错误。"""
from types import SimpleNamespace

import pytest

from litradar import cli, pipeline, rank, summarize
from litradar.config import Config


@pytest.fixture
def stages(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "cli.db")
    cfg.interests_data = {"groups": [{"slug": "org"}, {"slug": "mat"}]}
    cfg.sources.xmol_enabled = False
    calls, failures = [], set()

    def runner(stage):
        def run(conn, cfg, prof, *a, **kw):
            calls.append((stage, prof.slug))
            if (stage, prof.slug) in failures:
                raise RuntimeError(f"injected {stage} {prof.slug}")
            return {"errors": 0}
        return run

    monkeypatch.setattr(cli, "load_config", lambda *_: cfg)
    monkeypatch.setattr(rank, "_rank_group", runner("rank"))
    monkeypatch.setattr(summarize, "_summarize_group", runner("summarize"))
    monkeypatch.setattr(pipeline, "_ingest_group", runner("search"))
    monkeypatch.setattr(summarize, "LLMClient", lambda *_: SimpleNamespace(available=True))
    monkeypatch.setattr(pipeline.enrich, "run", lambda *a, **kw: {"enriched": 0})
    return calls, failures


@pytest.mark.parametrize("stage,argv", [
    ("rank", ["rank"]), ("rank", ["rank", "--force"]), ("summarize", ["summarize"]),
    ("search", ["ingest", "search"]), ("search", ["ingest", "all"]),
])
@pytest.mark.parametrize("failed_groups", [[], ["org"], ["org", "mat"]])
def test_分组CLI成功部分失败全部失败的退出状态(stages, capsys, stage, argv, failed_groups):
    calls, failures = stages
    failures.update((stage, slug) for slug in failed_groups)
    assert cli.main(argv) == int(bool(failed_groups))
    assert calls == [(stage, "org"), (stage, "mat")]
    output = capsys.readouterr().out
    for slug in failed_groups:
        assert f"injected {stage} {slug}" in output


@pytest.mark.parametrize("failed_stage", [None, "search", "rank", "summarize"])
def test_run嵌套阶段错误影响退出码且不阻止其余阶段(stages, failed_stage):
    calls, failures = stages
    if failed_stage:
        failures.add((failed_stage, "org"))
    assert cli.main(["run"]) == int(failed_stage is not None)
    assert calls == [(stage, slug) for stage in ("search", "rank", "summarize")
                     for slug in ("org", "mat")]


def test_未配置可选来源或LLM的跳过仍算成功(stages, monkeypatch):
    monkeypatch.setattr(summarize, "LLMClient", lambda *_: SimpleNamespace(available=False))
    assert cli.main(["summarize"]) == 0
    assert cli.main(["ingest", "mail"]) == 0


def test_名为errors的检索词和组不会被当作阶段错误(stages, monkeypatch):
    monkeypatch.setattr(pipeline, "ingest_keyword_search", lambda *a, **kw: {
        "errors": 0, "groups": {"errors": {"per_query": {"errors": 2}, "errors": 0}},
    })
    assert cli.main(["ingest", "search"]) == 0
    assert cli.main(["run"]) == 0
