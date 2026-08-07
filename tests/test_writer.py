"""The codec-writing loop, without a key, a network or a bill.

The point of these tests is the loop rather than the model: that a codec is
only accepted when it rebuilds the file byte for byte, that a failure is fed
back rather than swallowed, and that the budget is a real ceiling.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from savesmith.agent.discovery import (
    Attempt,
    CodecRequest,
    describe_shape,
    hexdump,
    trial_for,
)
from savesmith.agent.writer import (
    ModelCodecWriter,
    cost_usd,
    extract_source,
    make_client,
    price,
)
from savesmith.core.detect import inspect

GOOD_CODEC = '''
def decode(data):
    return {"text": data.decode("utf-8")}


def encode(value):
    return value["text"].encode("utf-8")
'''

LOSSY_CODEC = '''
def decode(data):
    return {"text": data.decode("utf-8").strip()}


def encode(value):
    return value["text"].encode("utf-8")
'''

NO_ENCODE = '''
def decode(data):
    return list(data)
'''


# ---------------------------------------------------------------------------
# A stand-in for the SDK
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, input_tokens: int = 1000, output_tokens: int = 500) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Message:
    def __init__(self, text: str, *, stop_reason: str = "end_turn") -> None:
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get_final_message(self) -> _Message:
        return self._message


@dataclass
class FakeClient:
    """Hands back canned replies and records what it was asked."""

    replies: list[Any]
    """Each entry is a reply as text, a prepared ``_Message``, or an exception."""

    prompt_tokens: int = 1000
    requests: list[dict[str, Any]] = field(default_factory=list)
    counted: int = 0

    def __post_init__(self) -> None:
        client = self

        class _Messages:
            def stream(self, **arguments: Any) -> _Stream:
                return client._stream(arguments)

            def count_tokens(self, **_arguments: Any) -> Any:
                client.counted += 1
                return types.SimpleNamespace(input_tokens=client.prompt_tokens)

        self.messages = _Messages()
        # No beta namespace: exercises the graceful path for an older SDK.

    def _stream(self, arguments: dict[str, Any]) -> _Stream:
        self.requests.append(arguments)
        if not self.replies:
            raise AssertionError("the writer asked for more replies than the test supplied")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, _Message):
            return _Stream(reply)
        return _Stream(_Message(reply))


def _request(payload: bytes = b"hello world\n", **kwargs: Any) -> CodecRequest:
    defaults: dict[str, Any] = {
        "look": inspect(payload),
        "head_hex": hexdump(payload),
        "tail_hex": hexdump(payload),
        "tried": ("gzip", "zlib"),
        "max_budget_usd": 10.0,
        "trial": trial_for(payload),
    }
    defaults.update(kwargs)
    return CodecRequest(**defaults)


def _fenced(source: str) -> str:
    return f"This looks like plain UTF-8 text.\n\n```python\n{source}\n```\n"


# ---------------------------------------------------------------------------
# The round-trip gate
# ---------------------------------------------------------------------------


def test_accepts_a_codec_that_rebuilds_the_file() -> None:
    client = FakeClient(replies=[_fenced(GOOD_CODEC)])
    writer = ModelCodecWriter(client=client)

    proposal = writer.propose(_request())

    assert proposal is not None
    assert proposal.verified
    assert proposal.attempts == 1
    assert "def encode" in proposal.source
    assert proposal.explanation.startswith("This looks like plain UTF-8 text.")
    assert proposal.cost_usd > 0


def test_a_codec_that_loses_bytes_is_not_accepted() -> None:
    """The lossy codec decodes fine and re-encodes to something shorter.

    This is the failure that matters: it looks like it worked.
    """
    client = FakeClient(replies=[_fenced(LOSSY_CODEC)] * 4)
    writer = ModelCodecWriter(client=client, max_attempts=4)

    proposal = writer.propose(_request())

    assert proposal is not None
    assert not proposal.verified
    assert "4 attempts" in writer.reason


def test_the_failure_is_sent_back_with_the_offset() -> None:
    client = FakeClient(replies=[_fenced(LOSSY_CODEC), _fenced(GOOD_CODEC)])
    writer = ModelCodecWriter(client=client)

    proposal = writer.propose(_request())

    assert proposal is not None and proposal.verified
    assert proposal.attempts == 2

    second = client.requests[1]["messages"]
    assert second[1]["role"] == "assistant"
    feedback = second[2]["content"]
    assert "did not reproduce it" in feedback
    assert "11 bytes long; the original is 12" in feedback


def test_a_reply_with_no_module_is_asked_again() -> None:
    client = FakeClient(replies=["I would need to see more of the file.", _fenced(GOOD_CODEC)])
    writer = ModelCodecWriter(client=client)

    proposal = writer.propose(_request())

    assert proposal is not None and proposal.verified
    assert "no Python module" in client.requests[1]["messages"][2]["content"]


def test_a_codec_without_encode_fails_the_gate() -> None:
    client = FakeClient(replies=[_fenced(NO_ENCODE)] * 2)
    writer = ModelCodecWriter(client=client, max_attempts=2)

    proposal = writer.propose(_request())

    assert proposal is not None and not proposal.verified
    assert "encode" in client.requests[1]["messages"][2]["content"]


def test_without_a_trial_the_proposal_is_returned_unverified() -> None:
    client = FakeClient(replies=[_fenced(GOOD_CODEC)])
    writer = ModelCodecWriter(client=client)

    proposal = writer.propose(_request(trial=None))

    assert proposal is not None
    assert not proposal.verified


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def test_the_budget_stops_the_loop_before_spending_over_it() -> None:
    """The cap is checked against what the *next* attempt could cost.

    An attempt is priced at its worst case — the whole output allowance at
    Opus rates, about $0.41 here — so a ceiling just above that buys one
    attempt and refuses the second, whatever the first one actually cost.
    """
    client = FakeClient(replies=[_fenced(LOSSY_CODEC)] * 4)
    writer = ModelCodecWriter(client=client, max_attempts=4)

    proposal = writer.propose(_request(max_budget_usd=0.41))

    assert proposal is not None and not proposal.verified
    assert writer.ledger.calls == 1
    assert writer.ledger.spent_usd <= 0.41
    assert "budget does not cover another attempt" in writer.reason


def test_a_budget_too_small_for_one_attempt_spends_nothing() -> None:
    client = FakeClient(replies=[_fenced(GOOD_CODEC)])
    writer = ModelCodecWriter(client=client)

    proposal = writer.propose(_request(max_budget_usd=0.001))

    assert proposal is None
    assert writer.ledger.spent_usd == 0.0
    assert client.requests == []


def test_a_cheaper_model_buys_more_attempts() -> None:
    client = FakeClient(replies=[_fenced(LOSSY_CODEC)] * 4)
    writer = ModelCodecWriter(client=client, model="claude-haiku-4-5", max_attempts=4)

    writer.propose(_request(max_budget_usd=0.60))

    assert writer.ledger.calls == 4


def test_cost_is_taken_from_the_reported_usage() -> None:
    usage = _Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-opus-5", usage) == pytest.approx(30.0)
    assert cost_usd("claude-opus-5", None) == 0.0


def test_an_unknown_model_is_priced_high_rather_than_free() -> None:
    """A model we have never heard of must not be treated as costing nothing."""
    assert price("something-new")[0] >= max(
        value[0] for value in [price("claude-opus-5"), price("claude-sonnet-5")]
    )


def test_cached_tokens_are_cheaper_than_fresh_ones() -> None:
    fresh = _Usage(input_tokens=100_000, output_tokens=0)
    cached = _Usage(input_tokens=0, output_tokens=0)
    cached.cache_read_input_tokens = 100_000
    assert cost_usd("claude-opus-5", cached) < cost_usd("claude-opus-5", fresh)


# ---------------------------------------------------------------------------
# Failures that are not the model's fault
# ---------------------------------------------------------------------------


def test_no_client_means_a_reason_not_a_crash() -> None:
    writer = ModelCodecWriter(client=None)
    writer.propose(_request())  # make_client() returns None without credentials

    # Either there genuinely is no client here, or the environment has one and
    # we must not call it in a test.
    assert writer.reason or writer.client is None


def test_a_network_failure_is_reported_in_a_sentence() -> None:
    client = FakeClient(replies=[ConnectionError("name resolution failed")])
    writer = ModelCodecWriter(client=client)

    assert writer.propose(_request()) is None
    assert "could not be reached" in writer.reason
    assert "name resolution failed" in writer.reason


def test_a_refusal_stops_the_loop() -> None:
    client = FakeClient(replies=[_Message("", stop_reason="refusal")])
    writer = ModelCodecWriter(client=client)

    assert writer.propose(_request()) is None
    assert "declined" in writer.reason


def test_make_client_without_the_package_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    def _missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", _missing)
    assert make_client() is None


# ---------------------------------------------------------------------------
# Parsing what came back
# ---------------------------------------------------------------------------


def test_extract_prefers_the_block_with_both_halves() -> None:
    text = (
        "First idea:\n```python\ndef decode(data):\n    return data\n```\n"
        "Better:\n```python\ndef decode(data):\n    return data\n\n\ndef encode(v):\n"
        "    return v\n```\n"
    )
    source = extract_source(text)
    assert source is not None and "def encode" in source


def test_extract_takes_the_last_revision() -> None:
    text = (
        "```python\ndef decode(data):\n    return 1\n\n\ndef encode(v):\n    return b''\n```\n"
        "On reflection:\n"
        "```python\ndef decode(data):\n    return 2\n\n\ndef encode(v):\n    return b''\n```\n"
    )
    source = extract_source(text)
    assert source is not None and "return 2" in source


def test_extract_accepts_bare_code() -> None:
    assert extract_source("def decode(data):\n    return data\n") is not None


def test_extract_returns_none_when_there_is_no_code() -> None:
    assert extract_source("I cannot work this format out from what you sent.") is None


# ---------------------------------------------------------------------------
# The trial closure
# ---------------------------------------------------------------------------


def test_the_trial_reports_the_shape_without_the_contents() -> None:
    payload = b'{"coins": 500}'
    source = (
        "import json\n\n\n"
        "def decode(data):\n    return json.loads(data)\n\n\n"
        "def encode(value):\n"
        '    return json.dumps(value, separators=(", ", ": ")).encode()\n'
    )
    attempt = trial_for(payload)(source)

    assert attempt.ok
    assert attempt.shape == "decode() returned a dict with keys: coins"
    assert "500" not in attempt.shape


def test_the_trial_catches_a_codec_that_raises() -> None:
    attempt = trial_for(b"abc")("def decode(data):\n    raise ValueError('nope')\n")
    assert not attempt.ok
    assert "nope" in attempt.error


def test_describe_shape_handles_anything() -> None:
    assert describe_shape(None) == ""
    assert describe_shape({"type": "list", "count": 3}) == "decode() returned a list of 3"
    assert describe_shape({"type": "int"}) == "decode() returned a int"


def test_attempt_defaults_are_empty() -> None:
    assert Attempt(ok=True) == Attempt(ok=True, error="", shape="")
