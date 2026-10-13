"""Public progress follows partial failures without interpreting user labels."""
import pytest

from litradar import progress


@pytest.mark.parametrize('result, expected', [
    ({'groups': {'errors': {'errors': 0, 'per_query': {'errors': 2, 'failed': 3}}}}, 'completed'),
    ({'ingest_search': {'errors': 0, 'groups': {'failed': {'new': 2}}}}, 'completed'),
    ({'skipped': 'Not configured'}, 'skipped'),
    ({'groups': {'errors': {'brief': 1, 'brief_failed': 1}}}, 'partial'),
    ({'summarize': {'errors': 0, 'groups': {'synthetic': {'deep_failed': 1}}}}, 'partial'),
    ({'rank': {'errors': 0, 'llm_failed': 1}}, 'partial'),
    ({'enrich': {'enriched': 1, 'failed': 1}}, 'partial'),
])
def test_stage_state_distinguishes_failures_from_user_labels(result, expected):
    events = []
    with progress.observe(events.append):
        assert progress.run_stage('summarize', lambda: result) is result
    assert events[-1]['status'] == expected
