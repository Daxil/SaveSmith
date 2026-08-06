"""Running code we did not write.

A codec produced by a model is not trusted code. It runs in a separate process
with limits on time, processor, memory and file size, with an emptied
environment, in a temporary directory of its own, and with the obvious ways out
— ``subprocess``, ``socket``, ``os.system`` — removed before it starts.

**What this is not.** It is not a jail. A determined attacker with code
execution can get past any of this; the operating system's own sandboxing is
the only real answer, and that belongs to the packaging work. What this does do
is make the realistic failures safe: a codec that loops forever, allocates
until the machine swaps, writes a hundred gigabytes, or casually phones home,
is stopped without the user noticing anything worse than an error message.

Everything the codec is allowed to touch is passed in and returned as data. It
never sees the user's save folder, only the bytes handed to it.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from savesmith.core.errors import SaveSmithError
from savesmith.core.platform_ import Platform, current_platform

DEFAULT_TIMEOUT = 10.0
DEFAULT_MEMORY_MB = 512
DEFAULT_OUTPUT_LIMIT = 1 * 1024 * 1024

# Removed from the child before its code runs. Not a complete list of dangerous
# things — a complete list does not exist — but it closes the doors that get
# opened by accident or by a model repeating a pattern it saw somewhere.
_FORBIDDEN_MODULES = (
    "subprocess",
    "socket",
    "ssl",
    "http",
    "urllib",
    "ftplib",
    "telnetlib",
    "smtplib",
    "multiprocessing",
    "ctypes",
    "webbrowser",
)

_PREAMBLE = '''
import builtins, os, sys

_FORBIDDEN = {forbidden!r}

class _Blocked(ImportError):
    pass

_real_import = builtins.__import__

def _guarded_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root in _FORBIDDEN:
        raise _Blocked(
            f"the sandbox does not allow importing {{name}}; a save codec has no "
            f"reason to reach outside the data it was given"
        )
    return _real_import(name, *args, **kwargs)

builtins.__import__ = _guarded_import

for _name in ("system", "popen", "execv", "execve", "execvp", "fork", "spawnv"):
    if hasattr(os, _name):
        setattr(os, _name, None)

sys.modules.pop("subprocess", None)
sys.modules.pop("socket", None)
'''


class SandboxError(SaveSmithError):
    """The sandboxed code did not finish successfully."""

    code = "sandbox_failed"

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"The decoding step written for this game could not be run: {reason}",
            detail=detail,
        )


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = DEFAULT_TIMEOUT
    memory_mb: int = DEFAULT_MEMORY_MB
    output_bytes: int = DEFAULT_OUTPUT_LIMIT
    file_size_mb: int = 256

    def as_dict(self) -> dict[str, float]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "memory_mb": self.memory_mb,
            "file_size_mb": self.file_size_mb,
        }


@dataclass
class Result:
    ok: bool
    value: Any = None
    """Whatever the codec returned: bytes, or anything JSON can carry."""
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    limits_hit: list[str] = field(default_factory=list)


def memory_limit_enforced() -> bool:
    """Whether an address-space limit actually holds on this system.

    macOS refuses ``RLIMIT_AS`` outright and Windows has no equivalent, so on
    both of them a runaway allocation is caught by the clock rather than by a
    memory ceiling. Saying so is better than implying a guarantee that is not
    there.
    """
    return current_platform() is Platform.LINUX


def _apply_limits(limits: Limits) -> None:  # pragma: no cover - runs in the child
    """Set what limits this system will accept, before the child runs anything.

    Best-effort on purpose: macOS rejects ``RLIMIT_AS`` with an error, and a
    limit the operating system will not take is no reason to refuse to run the
    codec at all. Whatever cannot be set is covered by the wall-clock timeout,
    which every platform honours.
    """
    memory = limits.memory_mb * 1024 * 1024
    file_size = limits.file_size_mb * 1024 * 1024
    cpu = max(1, int(limits.timeout_seconds) + 1)

    for which, value in (
        (resource.RLIMIT_CPU, (cpu, cpu)),
        (resource.RLIMIT_FSIZE, (file_size, file_size)),
        (resource.RLIMIT_CORE, (0, 0)),
        (resource.RLIMIT_AS, (memory, memory)),
    ):
        try:
            resource.setrlimit(which, value)
        except (ValueError, OSError):
            continue


def run(
    source: str,
    *,
    call: str = "decode",
    payload: bytes = b"",
    limits: Limits | None = None,
) -> Result:
    """Run ``source`` in a child process and call one function in it.

    The function receives the payload as ``bytes`` and must return bytes, a
    string, or anything JSON can carry. Nothing else crosses the boundary.
    """
    limits = limits or Limits()

    with tempfile.TemporaryDirectory(prefix="savesmith-sandbox-") as workspace:
        folder = Path(workspace)
        (folder / "codec.py").write_text(source, encoding="utf-8")
        (folder / "payload.bin").write_bytes(payload)
        runner = folder / "_runner.py"
        runner.write_text(_runner_source(call), encoding="utf-8")

        supports_rlimit = current_platform() is not Platform.WINDOWS
        try:
            # Fixed argv, no shell: nothing here is built from the codec.
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(runner)],
                cwd=folder,
                capture_output=True,
                timeout=limits.timeout_seconds,
                # An empty environment: no proxies, no tokens, no PYTHONPATH
                # pointing somewhere unexpected.
                env={"PATH": "", "PYTHONIOENCODING": "utf-8"},
                preexec_fn=(lambda: _apply_limits(limits)) if supports_rlimit else None,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return Result(
                ok=False,
                error=f"it did not finish within {limits.timeout_seconds:g} seconds",
                limits_hit=["timeout"],
            )
        except OSError as exc:
            raise SandboxError("the sandbox could not be started", detail=str(exc)) from exc

        stdout = _clip(completed.stdout, limits.output_bytes)
        stderr = _clip(completed.stderr, limits.output_bytes)

        result_file = folder / "result.json"
        if completed.returncode != 0 or not result_file.is_file():
            return Result(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                error=_diagnose(completed.returncode, stderr),
                limits_hit=_limits_hit(completed.returncode, stderr),
            )

        try:
            payload_out = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return Result(
                ok=False, stdout=stdout, stderr=stderr, error=f"unreadable result ({exc})"
            )

        if not payload_out.get("ok"):
            return Result(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                error=str(payload_out.get("error", "it raised an error")),
            )

        value = payload_out.get("value")
        if payload_out.get("encoding") == "base64":
            import base64

            value = base64.b64decode(str(value))
        return Result(ok=True, value=value, stdout=stdout, stderr=stderr)


def _runner_source(call: str) -> str:
    return f'''
{_PREAMBLE.format(forbidden=_FORBIDDEN_MODULES)}

import base64, json, traceback

result = {{"ok": False}}
try:
    with open("payload.bin", "rb") as handle:
        payload = handle.read()
    namespace = {{}}
    with open("codec.py", "r", encoding="utf-8") as handle:
        exec(compile(handle.read(), "codec.py", "exec"), namespace)
    function = namespace.get({call!r})
    if function is None:
        raise NameError("the codec has no function called {call}")
    value = function(payload)
    if isinstance(value, (bytes, bytearray)):
        result = {{"ok": True, "encoding": "base64",
                   "value": base64.b64encode(bytes(value)).decode("ascii")}}
    else:
        json.dumps(value)
        result = {{"ok": True, "value": value}}
except BaseException as exc:
    result = {{"ok": False,
               "error": f"{{type(exc).__name__}}: {{exc}}",
               "traceback": traceback.format_exc()[-4000:]}}

with open("result.json", "w", encoding="utf-8") as handle:
    json.dump(result, handle)
'''


def _clip(raw: bytes, limit: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def _limits_hit(returncode: int, stderr: str) -> list[str]:
    hit = []
    if returncode == -24 or "CPU time limit" in stderr:
        hit.append("processor time")
    if "MemoryError" in stderr or returncode == -9:
        hit.append("memory")
    if "File size limit" in stderr or returncode == -25:
        hit.append("file size")
    return hit


def _diagnose(returncode: int, stderr: str) -> str:
    for name, text in (
        ("processor time", "it used more processor time than allowed"),
        ("memory", "it tried to use more memory than allowed"),
        ("file size", "it tried to write a file larger than allowed"),
    ):
        if name in _limits_hit(returncode, stderr):
            return text
    if returncode < 0:
        return f"it was stopped by signal {-returncode}"
    last = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    return f"it failed ({last})" if last else "it failed without saying why"


def describe_restrictions() -> list[str]:
    """What the sandbox does, for the log and for anyone auditing it."""
    memory = (
        f"limited to {DEFAULT_MEMORY_MB} MB of memory"
        if memory_limit_enforced()
        else "no memory ceiling on this system; the timeout catches runaway allocation"
    )
    return [
        "runs in a separate process with an empty environment",
        f"stopped after {DEFAULT_TIMEOUT:g} seconds of wall clock time",
        memory,
        "cannot import: " + ", ".join(_FORBIDDEN_MODULES),
        "os.system, os.popen, os.exec* and os.fork are removed",
        "works in a temporary folder and never sees the save folder",
    ]
