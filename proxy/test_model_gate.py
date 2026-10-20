"""Offline regressions for false-positive model promotion decisions."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

GATE_PATH = Path(__file__).parents[1] / "tooling" / "cortex_model_gate.py"
SPEC = importlib.util.spec_from_file_location("model_gate", GATE_PATH)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


def passing_results():
    return dict.fromkeys(("T1", "T2", "T3", "T4", "T5", "T7"), "OK")


def test_complete_evidence_passes():
    assert GATE.verdetto(passing_results()) == "COMPATIBILE"


@pytest.mark.parametrize("test", ["T2", "T3", "T4", "T5", "T7"])
@pytest.mark.parametrize("result", [None, "KO", "INCONCLUSIVE"])
def test_missing_or_failed_evidence_blocks_promotion(test, result):
    results = passing_results()
    results[test] = result
    results["T6"] = "OK with reasoning_effort=none"
    assert GATE.verdetto(results) == "CON RISERVA"


def test_unreachable_model_fails():
    results = passing_results()
    results["T1"] = "KO"
    assert GATE.verdetto(results) == "INCOMPATIBILE"


def test_parallel_retry_sends_mutated_document_not_boolean(monkeypatch):
    requests = []

    def post(host, token, body):
        assert isinstance(body, dict)
        requests.append(body)
        if len(requests) == 1:
            return 400, "parallel tools rejected"
        return 200, json.dumps({"choices": [{"message": {"content": "14:30, sunny"}}]})

    monkeypatch.setattr(GATE, "post", post)
    result = {"note": []}
    GATE.t5_tool_parallele("example.invalid", "synthetic", "candidate", result)
    assert len(requests[0]["messages"][1]["tool_calls"]) == 2
    assert len(requests[1]["messages"][1]["tool_calls"]) == 1
    assert "sunny, 28C" in requests[1]["messages"][2]["content"]
    assert result["T5"].startswith("OK")


@pytest.mark.parametrize("message,finish", [
    ({"content": ""}, "stop"),
    ({"content": "partial"}, "length"),
    ({"content": "", "tool_calls": [{"id": "another"}]}, "tool_calls"),
])
def test_http_success_is_not_a_completed_answer(message, finish):
    text = json.dumps({"choices": [{"message": message, "finish_reason": finish}]})
    assert not GATE.completed_text(200, text)


def test_stream_without_terminator_fails(monkeypatch):
    monkeypatch.setattr(GATE, "post", lambda *args, **kwargs: (
        200, ['data: {"choices":[{"delta":{"content":"partial"}}]}']))
    result = {"note": []}
    GATE.t3_streaming("example.invalid", "synthetic", "candidate", result)
    assert result["T3"] == "KO"


def test_reasoning_retry_keeps_explicit_none(monkeypatch):
    observed = []

    def post(host, token, body):
        observed.append(body)
        return 200, json.dumps({"choices": [{"message": {"content": "no call"}}]})

    monkeypatch.setattr(GATE, "post", post)
    result = {"note": [], "richiede_reasoning_none": True}
    GATE.t4_tool_roundtrip("example.invalid", "synthetic", "candidate", result)
    assert observed[0]["reasoning_effort"] == "none"
    assert result["T4"] == "INCONCLUSIVO"


def test_partial_text_does_not_pass(monkeypatch):
    response = json.dumps({"choices": [{"message": {"content": "partial"},
                                        "finish_reason": "length"}]})
    monkeypatch.setattr(GATE, "post", lambda *args, **kwargs: (200, response))
    result = {"note": []}
    assert not GATE.t1_t2_non_stream("example.invalid", "synthetic", "candidate", result)
    assert result["T1"] == "KO"


def test_truncated_stream_does_not_pass_with_done(monkeypatch):
    monkeypatch.setattr(GATE, "post", lambda *args, **kwargs: (
        200, ['data: {"choices":[{"delta":{"content":"partial"},'
              '"finish_reason":"length"}]}', 'data: [DONE]']))
    result = {"note": []}
    GATE.t3_streaming("example.invalid", "synthetic", "candidate", result)
    assert result["T3"] == "KO"


def test_parallel_transform_is_tested_even_when_raw_request_passes(monkeypatch):
    requests = []

    def post(host, token, body):
        requests.append(body)
        return 200, json.dumps({"choices": [{"message": {"content": "14:30, sunny"}}]})

    monkeypatch.setattr(GATE, "post", post)
    result = {"note": []}
    GATE.t5_tool_parallele("example.invalid", "synthetic", "candidate", result)
    assert len(requests) == 2
    assert len(requests[1]["messages"][1]["tool_calls"]) == 1


@pytest.mark.parametrize("tool_call", [
    {"id": "a", "function": {"name": "wrong", "arguments": '{"city":"Rome"}'}},
    {"id": "a", "function": {"name": "get_time", "arguments": 'not-json'}},
    {"id": "", "function": {"name": "get_time", "arguments": '{"city":"Rome"}'}},
])
def test_invalid_tool_calls_fail_before_followup(monkeypatch, tool_call):
    requests = []

    def post(host, token, body):
        requests.append(body)
        return 200, json.dumps({"choices": [{"message": {"tool_calls": [tool_call]}}]})

    monkeypatch.setattr(GATE, "post", post)
    result = {"note": []}
    GATE.t4_tool_roundtrip("example.invalid", "synthetic", "candidate", result)
    assert result["T4"] == "KO"
    assert len(requests) == 1