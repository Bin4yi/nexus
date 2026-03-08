"""
tests/test_05_map_reduce.py
Gate 5: 8 tests for the Map-Reduce reasoning pipeline.
All LLM calls are mocked — no API key required.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from reasoning.map_step import MapStep, MapResult
from reasoning.reduce_step import ReduceStep, NO_COMMUNITIES_MESSAGE, DATA_BUDGET
from community.prompt_builder import count_tokens


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_map_result(community_id: int, score: int, summary: str = "some summary") -> MapResult:
    return MapResult(
        community_id=community_id,
        score=score,
        reason=f"Reason for community {community_id}",
        summary_text=summary,
    )


def _mock_llm_response(content: str):
    """Build a minimal OpenAI response mock."""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = content
    return mock_resp


# ── Test 1: Zero-score filter ─────────────────────────────────────────────────

def test_01_zero_score_filtered():
    """Test 1: Communities scored 0 are removed before the reduce step."""
    mock_chroma = MagicMock()
    # The collection returned by get_or_create_collection() needs count() to be int
    mock_collection = MagicMock()
    mock_collection.count.return_value = 3
    mock_collection.query.return_value = {
        "ids": [["c1", "c2", "c3"]],
        "documents": [["summary A", "summary B", "summary C"]],
        "metadatas": [[
            {"community_id": 1},
            {"community_id": 2},
            {"community_id": 3},
        ]],
    }
    mock_chroma.get_or_create_collection.return_value = mock_collection

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.side_effect = [
        _mock_llm_response('{"score": 0, "reason": "no relation"}'),    # community 1 → filtered
        _mock_llm_response('{"score": 75, "reason": "auth impact"}'),   # community 2 → kept
        _mock_llm_response('{"score": 0, "reason": "unrelated"}'),      # community 3 → filtered
    ]

    step = MapStep(mock_chroma, mock_llm, n_candidates=3)
    results = step.run("Changed UserService.getUser()")

    # Only community 2 (score=75) should remain
    assert len(results) == 1
    assert results[0].community_id == 2
    assert results[0].score == 75


# ── Test 2: Score parsing ─────────────────────────────────────────────────────

def test_02_score_parsed_from_json():
    """Test 2: LLM JSON response is parsed into integer score."""
    mock_chroma = MagicMock()
    mock_collection = MagicMock()
    mock_collection.count.return_value = 1
    mock_collection.query.return_value = {
        "ids": [["c1"]],
        "documents": [["some summary"]],
        "metadatas": [[{"community_id": 10}]],
    }
    mock_chroma.get_or_create_collection.return_value = mock_collection

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value = _mock_llm_response(
        '{"score": 88, "reason": "Direct dependency affected"}'
    )

    step = MapStep(mock_chroma, mock_llm, n_candidates=1)
    results = step.run("PR changes UserRepository.findById")

    assert len(results) == 1
    assert isinstance(results[0].score, int)
    assert results[0].score == 88


# ── Test 3: Invalid JSON → score of 0 ────────────────────────────────────────

def test_03_invalid_json_defaults_to_zero():
    """Test 3: Malformed LLM response defaults to score=0 (and is filtered out)."""
    mock_chroma = MagicMock()
    mock_collection = MagicMock()
    mock_collection.count.return_value = 1
    mock_collection.query.return_value = {
        "ids": [["c1"]],
        "documents": [["summary"]],
        "metadatas": [[{"community_id": 5}]],
    }
    mock_chroma.get_or_create_collection.return_value = mock_collection

    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value = _mock_llm_response(
        "I think the impact is quite high."   # not JSON
    )

    step = MapStep(mock_chroma, mock_llm, n_candidates=1)
    results = step.run("some PR")
    assert results == []   # score=0 → filtered out


# ── Test 4: Reduce stays under 8k tokens ─────────────────────────────────────

def test_04_reduce_prompt_under_8k():
    """Test 4: The reduce prompt is always under 8,000 tokens."""
    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value = _mock_llm_response("review text")

    # Create 100 communities with long summaries
    map_results = [
        _make_map_result(i, 100 - i, summary="X" * 500)  # 500-char summaries
        for i in range(100)
    ]
    reduce = ReduceStep(mock_llm)
    token_count = reduce.count_prompt_tokens(map_results)
    assert token_count < 8000


# ── Test 5: Truncation works ──────────────────────────────────────────────────

def test_05_truncation_reduces_community_count():
    """Test 5: When budget is exceeded, communities are truncated (lower scores dropped)."""
    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value = _mock_llm_response("review text")

    # 500 communities × 500-char summaries → definitely exceeds 8k
    map_results = [
        _make_map_result(i, 100 - i, summary="Y" * 500)
        for i in range(500)
    ]
    reduce = ReduceStep(mock_llm)
    summaries_text, n_included = reduce._build_summaries_text(map_results)
    assert n_included < 500         # some communities were truncated
    assert count_tokens(summaries_text) <= DATA_BUDGET


# ── Test 6: Empty map result ──────────────────────────────────────────────────

def test_06_empty_map_returns_no_communities():
    """Test 6: Empty map results returns a safe message, no crash."""
    mock_llm = MagicMock()
    reduce = ReduceStep(mock_llm)
    result = reduce.run([])
    assert result == NO_COMMUNITIES_MESSAGE
    mock_llm.chat.completions.create.assert_not_called()


# ── Test 7: Final review is non-empty ────────────────────────────────────────

def test_07_final_review_non_empty():
    """Test 7: Reduce returns a non-empty review string with risk level."""
    mock_llm = MagicMock()
    mock_llm.chat.completions.create.return_value = _mock_llm_response(
        "**Overall Risk Level**: HIGH\n"
        "**Blast Radius**: Auth and user service are affected.\n"
        "**Top Concerns**: JWT token expiry logic is impacted.\n"
        "**Recommended Actions**: Review token validation unit tests."
    )

    map_results = [_make_map_result(1, 85), _make_map_result(2, 60)]
    reduce = ReduceStep(mock_llm)
    review = reduce.run(map_results)

    assert len(review) > 0
    assert "HIGH" in review or "Risk" in review


# ── Test 8: Property-based — many community counts always under 8k ────────────

@pytest.mark.parametrize("n_communities", [1, 5, 10, 25, 50, 100, 200])
def test_08_token_budget_property(n_communities):
    """Test 8: For any number of communities, reduce prompt always stays under 8k."""
    mock_llm = MagicMock()
    map_results = [
        _make_map_result(i, 50, summary="A" * 300)
        for i in range(n_communities)
    ]
    reduce = ReduceStep(mock_llm)
    token_count = reduce.count_prompt_tokens(map_results)
    assert token_count < 8000, (
        f"Token budget exceeded with {n_communities} communities: {token_count} tokens"
    )
