"""The one step that costs money: asking a model to write a codec.

Everything before this in :mod:`savesmith.agent.discovery` is free and
deterministic. This runs only for files that survived all of it, and it is
bounded on three sides:

* **A budget in dollars.** Every request is priced before it is sent and
  charged after it returns. When the next attempt would not fit inside what the
  user allowed, the loop stops and says so.
* **A number of attempts.** Four by default.
* **A round-trip gate.** The model's module is run against the real file in the
  sandbox, and is only accepted when ``encode(decode(data))`` reproduces the
  file byte for byte. A codec that decodes something plausible but cannot
  rebuild the original is not a codec; it is a way to corrupt a save.

The failure from each attempt goes back to the model — the exception it raised,
the offset where its output first differed — so the next attempt is a
correction rather than another guess.

**What crosses the wire.** The prompt built in ``discovery`` carries a hex dump
of the head and tail, the entropy figures, and the byte ranges that moved
between two saves. The feedback added here carries an exception message, a byte
offset, and the key names the module produced. The file itself never leaves the
machine; the model writes code, and the code runs here.

``anthropic`` is deliberately not a dependency of SaveSmith: every known
format, every edit and every backup works offline and free without it. Install
it (``pip install anthropic``) and set ``ANTHROPIC_API_KEY`` only to use
``savesmith discover --model``. Without the package, without credentials, or
without the budget for another attempt, :meth:`ModelCodecWriter.propose`
declines and says which of those it was, and discovery carries on to report
everything the free steps found.

Authorisation is the user's own API key. There is no way to point this at a
claude.ai subscription, and it does not try.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from savesmith.agent.discovery import CodecProposal, CodecRequest

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_ATTEMPTS = 4

# A codec plus the model's reasoning fits comfortably; the cap also bounds what
# one attempt can cost, which is what makes the budget arithmetic honest.
MAX_OUTPUT_TOKENS = 16_000

# Re-runs a refused request on another model rather than returning the refusal.
# Reverse engineering a file format is ordinary work, but a classifier that
# reads "decrypt this" can disagree, and one declined request should not end a
# discovery run.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# US dollars per million tokens, (input, output). Only used for the budget cap,
# so being slightly out of date makes the cap conservative, not wrong.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}
_UNKNOWN_MODEL_PRICE = (10.0, 50.0)

SYSTEM_PROMPT = """You reverse-engineer save-file formats for a save editor.

Write one Python module with two functions at module level:

    decode(data: bytes) -> object   # dicts, lists, numbers, strings, bytes
    encode(value) -> bytes          # rebuilds the original bytes

encode(decode(data)) has to equal data byte for byte — padding, field order, \
alignment, and any region you could not identify. Carry the parts you did not \
understand through the structure verbatim rather than dropping them or \
regenerating them; a save the game refuses to load is worse for the player \
than no editor at all.

The module runs in a sandbox: the standard library only, no network, no \
subprocesses, no reading files. Third-party packages are not installed.

Reply with the module in a single ```python block. A short paragraph before it \
describing what you concluded about the format is useful; put nothing after it.
"""

_FENCE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)
_FEEDBACK_LIMIT = 4000


@dataclass
class Ledger:
    """What this discovery has spent, and what it may still spend."""

    limit_usd: float
    spent_usd: float = 0.0
    calls: int = 0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    def can_afford(self, estimate_usd: float) -> bool:
        return self.spent_usd + estimate_usd <= self.limit_usd

    def charge(self, amount_usd: float) -> None:
        self.spent_usd += amount_usd
        self.calls += 1


@dataclass
class ModelCodecWriter:
    """A :class:`~savesmith.agent.discovery.CodecWriter` backed by the Claude API.

    ``client`` is injectable so the loop — the part with the budget, the
    retries and the round-trip gate in it — can be tested without a key, a
    network or a bill.
    """

    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_attempts: int = DEFAULT_ATTEMPTS
    client: Any | None = None
    log: Callable[[str], None] | None = None
    reason: str = ""
    """Why nothing came back, in a sentence, for the discovery report."""

    ledger: Ledger = field(default_factory=lambda: Ledger(limit_usd=0.0))

    def propose(self, request: CodecRequest) -> CodecProposal | None:
        client = self.client if self.client is not None else make_client()
        if client is None:
            self.reason = (
                "no model is configured; set ANTHROPIC_API_KEY or run 'ant auth login'"
            )
            return None

        self.ledger = Ledger(limit_usd=request.max_budget_usd)
        messages: list[dict[str, Any]] = [{"role": "user", "content": request.as_prompt()}]
        last_source: str | None = None

        for attempt_number in range(1, self.max_attempts + 1):
            estimate = self._estimate_usd(client, messages)
            if not self.ledger.can_afford(estimate):
                self.reason = (
                    f"the ${request.max_budget_usd:.2f} budget does not cover another "
                    f"attempt (about ${estimate:.2f}); ${self.ledger.spent_usd:.2f} spent "
                    f"over {self.ledger.calls} call(s)"
                )
                break

            self._say(f"asking {self.model} for a codec (attempt {attempt_number})")
            try:
                message = self._ask(client, messages)
            except Exception as exc:  # the SDK's exception types are loaded dynamically
                self.reason = f"the model could not be reached: {_short(exc)}"
                break

            self.ledger.charge(cost_usd(self.model, getattr(message, "usage", None)))

            if getattr(message, "stop_reason", None) == "refusal":
                self.reason = "the model declined this request"
                break

            text = _text_of(message)
            source = extract_source(text)
            if source is None:
                messages.append({"role": "assistant", "content": message.content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That reply had no Python module in it. Send the module in a "
                            "single ```python block, with decode(data) and encode(value) "
                            "at module level."
                        ),
                    }
                )
                continue

            last_source = source
            if request.trial is None:
                # Nothing to test against; hand it back unverified and let the
                # caller decide what to do with it.
                return CodecProposal(
                    source=source,
                    explanation=_prose_of(text),
                    cost_usd=self.ledger.spent_usd,
                    verified=False,
                    attempts=attempt_number,
                )

            attempt = request.trial(source)
            if attempt.ok:
                self._say(f"the codec rebuilt the file exactly (${self.ledger.spent_usd:.2f})")
                return CodecProposal(
                    source=source,
                    explanation=_prose_of(text),
                    cost_usd=self.ledger.spent_usd,
                    verified=True,
                    attempts=attempt_number,
                )

            self._say(f"attempt {attempt_number} did not round-trip: {attempt.error}")
            messages.append({"role": "assistant", "content": message.content})
            messages.append(
                {
                    "role": "user",
                    "content": _feedback(attempt.error, attempt.shape, attempt_number),
                }
            )
        else:
            self.reason = (
                f"{self.max_attempts} attempts did not produce a codec that rebuilds "
                f"the file exactly (${self.ledger.spent_usd:.2f} spent)"
            )

        if last_source is not None:
            # An unverified module is still worth returning: someone can read
            # it, and the report says plainly that it did not round-trip.
            return CodecProposal(
                source=last_source,
                explanation=self.reason,
                cost_usd=self.ledger.spent_usd,
                verified=False,
                attempts=self.ledger.calls,
            )
        return None

    # -- the API call ----------------------------------------------------

    def _ask(self, client: Any, messages: list[dict[str, Any]]) -> Any:
        """One request. Streamed, because a long answer on a slow connection
        would otherwise sit past the HTTP timeout and fail for no reason."""
        arguments: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        try:
            with client.beta.messages.stream(
                betas=[FALLBACK_BETA], fallbacks="default", **arguments
            ) as stream:
                return stream.get_final_message()
        except (TypeError, AttributeError):
            # An SDK older than the fallback parameter. Not worth failing over.
            pass
        except Exception as exc:
            if not _is_bad_request(exc):
                raise
        with client.messages.stream(**arguments) as stream:
            return stream.get_final_message()

    def _estimate_usd(self, client: Any, messages: list[dict[str, Any]]) -> float:
        """The worst this attempt could cost, so the cap means something.

        Counting tokens is free and exact; when it is unavailable the fallback
        deliberately over-estimates rather than risking an overspend.
        """
        input_price, output_price = price(self.model)
        try:
            counted = client.messages.count_tokens(
                model=self.model, system=SYSTEM_PROMPT, messages=messages
            )
            prompt_tokens = int(counted.input_tokens)
        except Exception:
            prompt_tokens = _rough_tokens(messages) + _rough_tokens(
                [{"content": SYSTEM_PROMPT}]
            )
        return (prompt_tokens * input_price + MAX_OUTPUT_TOKENS * output_price) / 1_000_000

    def _say(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


def make_client() -> Any | None:
    """The Anthropic client, or ``None`` if it cannot be had.

    Imported through :mod:`importlib` so that ``anthropic`` stays genuinely
    optional: a SaveSmith that only edits known formats has no use for it, and
    the package should not fail to import without it.
    """
    try:
        anthropic = importlib.import_module("anthropic")
    except ImportError:
        return None
    try:
        # Resolves an API key from the environment or a stored profile; raises
        # when there is neither.
        return anthropic.Anthropic()
    except Exception:
        return None


def price(model: str) -> tuple[float, float]:
    return PRICES_USD_PER_MTOK.get(model, _UNKNOWN_MODEL_PRICE)


def cost_usd(model: str, usage: Any) -> float:
    """What one response actually cost, from the usage the API reported."""
    if usage is None:
        return 0.0
    input_price, output_price = price(model)
    fresh = float(getattr(usage, "input_tokens", 0) or 0)
    cached = float(getattr(usage, "cache_read_input_tokens", 0) or 0)
    written = float(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    output = float(getattr(usage, "output_tokens", 0) or 0)
    total = (
        fresh * input_price
        + cached * input_price * 0.1
        + written * input_price * 1.25
        + output * output_price
    )
    return total / 1_000_000


def extract_source(text: str) -> str | None:
    """Pull the module out of the reply.

    A module that defines both halves wins over one that defines only
    ``decode``; the last such block wins over an earlier one, because a model
    that revises itself puts the final version last.
    """
    blocks = [str(block).strip() for block in _FENCE.findall(text)]
    complete = [block for block in blocks if "def decode" in block and "def encode" in block]
    if complete:
        return complete[-1]
    partial = [block for block in blocks if "def decode" in block]
    if partial:
        return partial[-1]
    if not blocks and "def decode" in text:
        return text.strip()
    return None


def _prose_of(text: str) -> str:
    """Whatever the model said before the code, as the explanation."""
    head = _FENCE.split(text)[0] if "```" in text else text
    cleaned = head.strip()
    return cleaned if len(cleaned) <= 2000 else cleaned[:2000] + "…"


def _text_of(message: Any) -> str:
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(block.text))
    return "\n".join(parts)


def _feedback(error: str, shape: str, attempt_number: int) -> str:
    lines = [
        f"Attempt {attempt_number} was run against the real save file and did not "
        "reproduce it:",
        "",
        error[:_FEEDBACK_LIMIT] or "it failed without saying why",
    ]
    if shape:
        lines += ["", shape]
    lines += [
        "",
        "Send a corrected module. Remember that every byte has to come back, "
        "including any region you decided to ignore.",
    ]
    return "\n".join(lines)


def _rough_tokens(messages: list[dict[str, Any]]) -> int:
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return characters // 3 + 1


def _is_bad_request(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 400:
        return True
    return "BadRequest" in type(exc).__name__


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0]}" if text else type(exc).__name__
