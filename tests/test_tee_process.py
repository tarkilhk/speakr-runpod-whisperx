"""Tests for runpod-image/tee_process.py"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "runpod-image"))

from tee_process import _run, main  # noqa: E402


def _console_mock() -> MagicMock:
    mock = MagicMock()
    mock.buffer = io.BytesIO()
    return mock


class TeeRunTests(unittest.IsolatedAsyncioTestCase):
    async def _run_child(
        self, code: str, tmp: str
    ) -> tuple[int, bytes, bytes, bytes, bytes]:
        """Run python -c <code>; return (rc, console_out, console_err, file_out, file_err).

        File contents are read while the temp dir is still alive so callers can
        assert after the TemporaryDirectory context exits.
        """
        stdout_log = Path(tmp) / "stdout.log"
        stderr_log = Path(tmp) / "stderr.log"
        out, err = _console_mock(), _console_mock()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = await _run([sys.executable, "-c", code], stdout_log, stderr_log)
        file_out = stdout_log.read_bytes() if stdout_log.exists() else b""
        file_err = stderr_log.read_bytes() if stderr_log.exists() else b""
        return rc, out.buffer.getvalue(), err.buffer.getvalue(), file_out, file_err

    async def test_stdout_teed_to_file_and_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, console_out, _, file_out, _ = await self._run_child(
                "import sys; sys.stdout.write('hello\\n'); sys.stdout.flush()", tmp
            )
        self.assertEqual(rc, 0)
        self.assertIn(b"hello\n", file_out)
        self.assertIn(b"hello\n", console_out)

    async def test_stderr_teed_to_file_and_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, console_err, _, file_err = await self._run_child(
                "import sys; sys.stderr.write('oops\\n'); sys.stderr.flush()", tmp
            )
        self.assertEqual(rc, 0)
        self.assertIn(b"oops\n", file_err)
        self.assertIn(b"oops\n", console_err)

    async def test_exit_code_propagated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, *_ = await self._run_child("import sys; sys.exit(42)", tmp)
        self.assertEqual(rc, 42)

    async def test_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout_log = Path(tmp) / "deep" / "nested" / "stdout.log"
            stderr_log = Path(tmp) / "deep" / "nested" / "stderr.log"
            out, err = _console_mock(), _console_mock()
            with patch("sys.stdout", out), patch("sys.stderr", err):
                await _run([sys.executable, "-c", "pass"], stdout_log, stderr_log)
            # Assert while tmp is still alive.
            self.assertTrue(stdout_log.parent.is_dir())

    async def test_empty_output_creates_empty_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, _, file_out, file_err = await self._run_child("pass", tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(file_out, b"")
        self.assertEqual(file_err, b"")

    async def test_stdout_and_stderr_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, _, file_out, file_err = await self._run_child(
                "import sys; sys.stdout.write('out\\n'); sys.stderr.write('err\\n');"
                " sys.stdout.flush(); sys.stderr.flush()",
                tmp,
            )
        self.assertEqual(rc, 0)
        self.assertIn(b"out\n", file_out)
        self.assertIn(b"err\n", file_err)
        self.assertNotIn(b"err\n", file_out)
        self.assertNotIn(b"out\n", file_err)


class TeeMainTests(unittest.TestCase):
    def test_exit_code_propagated_through_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout_log = Path(tmp) / "stdout.log"
            stderr_log = Path(tmp) / "stderr.log"
            out, err = _console_mock(), _console_mock()
            with patch("sys.stdout", out), patch("sys.stderr", err):
                with patch("sys.argv", [
                    "tee_process.py",
                    "--stdout-log", str(stdout_log),
                    "--stderr-log", str(stderr_log),
                    "--", sys.executable, "-c", "import sys; sys.exit(3)",
                ]):
                    with self.assertRaises(SystemExit) as cm:
                        main()
        self.assertEqual(cm.exception.code, 3)

    def test_double_dash_separator_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout_log = Path(tmp) / "stdout.log"
            stderr_log = Path(tmp) / "stderr.log"
            out, err = _console_mock(), _console_mock()
            with patch("sys.stdout", out), patch("sys.stderr", err):
                with patch("sys.argv", [
                    "tee_process.py",
                    "--stdout-log", str(stdout_log),
                    "--stderr-log", str(stderr_log),
                    "--", sys.executable, "-c", "print('via double dash')",
                ]):
                    with self.assertRaises(SystemExit) as cm:
                        main()
            # Assert while tmp is still alive.
            self.assertEqual(cm.exception.code, 0)
            self.assertIn(b"via double dash", stdout_log.read_bytes())

    def test_missing_command_exits_nonzero(self) -> None:
        with patch("sys.argv", [
            "tee_process.py",
            "--stdout-log", "/tmp/s.log",
            "--stderr-log", "/tmp/e.log",
        ]):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    main()
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
