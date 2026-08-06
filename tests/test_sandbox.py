"""Running code we did not write.

Most of these are attempts to escape. The sandbox is defence in depth rather
than a jail, so the point is that the realistic failures — a loop, a runaway
allocation, a casual network call — are all stopped without hurting anything.
"""

from __future__ import annotations

import json

import pytest

from savesmith.agent.sandbox import (
    Limits,
    describe_restrictions,
    memory_limit_enforced,
    run,
)
from savesmith.core.platform_ import Platform, current_platform

POSIX_ONLY = pytest.mark.skipif(
    current_platform() is Platform.WINDOWS,
    reason="file-size limits are POSIX-only; Windows relies on the timeout",
)
MEMORY_LIMITED = pytest.mark.skipif(
    not memory_limit_enforced(),
    reason="this system has no address-space limit; the timeout covers it",
)


class TestRunningCode:
    def test_bytes_in_bytes_out(self) -> None:
        result = run("def decode(data):\n    return data[::-1]\n", payload=b"abc")
        assert result.ok
        assert result.value == b"cba"

    def test_a_structure_comes_back_as_data(self) -> None:
        source = "import json\ndef decode(data):\n    return json.loads(data)\n"
        result = run(source, payload=json.dumps({"gold": 1}).encode())
        assert result.ok
        assert result.value == {"gold": 1}

    def test_a_different_function_can_be_called(self) -> None:
        result = run("def encode(data):\n    return data + b'!'\n", call="encode", payload=b"hi")
        assert result.value == b"hi!"

    def test_a_missing_function_is_reported(self) -> None:
        result = run("x = 1\n", payload=b"")
        assert not result.ok
        assert "no function called decode" in result.error

    def test_an_error_inside_the_codec_is_reported_not_raised(self) -> None:
        result = run("def decode(data):\n    raise ValueError('nope')\n", payload=b"")
        assert not result.ok
        assert "ValueError: nope" in result.error

    def test_syntax_errors_are_reported(self) -> None:
        result = run("def decode(data)\n    return data\n", payload=b"")
        assert not result.ok
        assert "SyntaxError" in result.error

    def test_printed_output_is_captured(self) -> None:
        result = run("def decode(data):\n    print('working')\n    return data\n", payload=b"x")
        assert result.ok
        assert "working" in result.stdout

    def test_a_value_that_cannot_be_carried_back(self) -> None:
        result = run("def decode(data):\n    return object()\n", payload=b"")
        assert not result.ok


class TestLimits:
    def test_an_endless_loop_is_stopped(self) -> None:
        result = run("def decode(data):\n    while True:\n        pass\n", payload=b"",
                     limits=Limits(timeout_seconds=1.0))
        assert not result.ok
        assert "timeout" in result.limits_hit or "processor" in result.error

    @MEMORY_LIMITED
    def test_a_runaway_allocation_is_stopped(self) -> None:
        source = "def decode(data):\n    x = bytearray(10**10)\n    return bytes(x[:1])\n"
        result = run(source, payload=b"", limits=Limits(timeout_seconds=10, memory_mb=128))
        assert not result.ok

    @POSIX_ONLY
    def test_a_huge_file_write_is_stopped(self) -> None:
        source = (
            "def decode(data):\n"
            "    with open('big.bin', 'wb') as handle:\n"
            "        for _ in range(200):\n"
            "            handle.write(b'x' * (1024 * 1024))\n"
            "    return b''\n"
        )
        result = run(source, payload=b"", limits=Limits(timeout_seconds=20, file_size_mb=8))
        assert not result.ok


class TestWhatIsBlocked:
    @pytest.mark.parametrize("module", ["subprocess", "socket", "urllib.request", "ctypes"])
    def test_dangerous_imports_are_refused(self, module: str) -> None:
        source = f"def decode(data):\n    import {module}\n    return data\n"
        result = run(source, payload=b"")
        assert not result.ok
        assert "does not allow importing" in result.error

    def test_os_system_is_gone(self) -> None:
        source = "import os\ndef decode(data):\n    os.system('echo hello')\n    return data\n"
        result = run(source, payload=b"")
        assert not result.ok

    def test_nothing_from_our_environment_reaches_the_codec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An API key or a proxy in the parent's environment must not leak."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value-that-must-not-leak")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")

        source = (
            "import os\n"
            "def decode(data):\n"
            "    return sorted(os.environ.keys())\n"
        )
        result = run(source, payload=b"")
        assert result.ok
        leaked = set(result.value or []) - {"PATH", "PYTHONIOENCODING", "LC_CTYPE"}
        # Whatever the interpreter adds for locale is fine; nothing of ours is.
        assert "ANTHROPIC_API_KEY" not in leaked
        assert "HTTPS_PROXY" not in leaked
        assert all(name.startswith("__") or name.startswith("LC_") for name in leaked), leaked

    def test_it_cannot_see_the_save_folder(self, tmp_path: str) -> None:
        """The codec gets bytes, not a path into someone's game."""
        source = (
            "import os\n"
            "def decode(data):\n"
            "    return sorted(os.listdir('.'))\n"
        )
        result = run(source, payload=b"x")
        assert result.ok
        assert set(result.value or []) <= {"codec.py", "payload.bin", "_runner.py", "result.json"}

    def test_harmless_modules_still_work(self) -> None:
        """Blocking everything would make the sandbox useless."""
        source = (
            "import json, zlib, struct, hashlib, base64\n"
            "def decode(data):\n"
            "    return hashlib.md5(data).hexdigest()\n"
        )
        result = run(source, payload=b"abc")
        assert result.ok
        assert result.value == "900150983cd24fb0d6963f7d28e17f72"


class TestHonesty:
    def test_the_restrictions_are_listed_for_auditing(self) -> None:
        text = " ".join(describe_restrictions())
        assert "separate process" in text
        assert "subprocess" in text

    def test_an_unenforceable_memory_limit_is_not_claimed(self) -> None:
        """Claiming a ceiling that the OS refused would be worse than none."""
        text = " ".join(describe_restrictions())
        if memory_limit_enforced():
            assert "MB of memory" in text
        else:
            assert "no memory ceiling on this system" in text
