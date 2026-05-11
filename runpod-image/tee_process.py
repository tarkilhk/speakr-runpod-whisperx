"""Run a child process while teeing stdout/stderr to files and container streams."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path


async def _tee_stream(
    stream: asyncio.StreamReader,
    console,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log_file:
        while chunk := await stream.read(64 * 1024):
            console.buffer.write(chunk)
            console.buffer.flush()
            log_file.write(chunk)


async def _run(argv: list[str], stdout_log: Path, stderr_log: Path) -> int:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    loop = asyncio.get_running_loop()

    def _terminate_child() -> None:
        if process.returncode is None:
            process.terminate()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, _terminate_child)
        except NotImplementedError:
            signal.signal(signum, lambda *_: _terminate_child())

    assert process.stdout is not None
    assert process.stderr is not None
    await asyncio.gather(
        _tee_stream(process.stdout, sys.stdout, stdout_log),
        _tee_stream(process.stderr, sys.stderr, stderr_log),
    )
    return await process.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout-log", required=True, type=Path)
    parser.add_argument("--stderr-log", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("command is required after --")

    raise SystemExit(asyncio.run(_run(command, args.stdout_log, args.stderr_log)))


if __name__ == "__main__":
    main()
