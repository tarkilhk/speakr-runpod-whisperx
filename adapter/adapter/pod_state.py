import asyncio
import fcntl
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TextIO

logger = logging.getLogger("whisperx-adapter.pod_state")


class ActivePodStore:
    def __init__(self, configured_pod_id: str, path: str) -> None:
        self._configured_pod_id = configured_pod_id
        self._active_pod_id = configured_pod_id
        self._path = Path(path) if path else None

    @property
    def path_label(self) -> str:
        return str(self._path) if self._path else "(no path)"

    def load(self) -> str:
        if self._active_pod_id:
            return self._active_pod_id
        if not self._path:
            return ""
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def store(self, pod_id: str) -> None:
        if self._configured_pod_id:
            logger.warning("Ignoring active RunPod pod ID store because RUNPOD_POD_ID is configured")
            return
        self._active_pod_id = pod_id
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(pod_id, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to write active RunPod pod ID to %s: %s", self._path, exc)

    def clear(self, pod_id: str | None = None) -> None:
        if self._configured_pod_id:
            return
        if pod_id and self._active_pod_id and pod_id != self._active_pod_id:
            return
        self._active_pod_id = ""
        if not self._path:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove active RunPod pod ID file: %s", exc)


class DeployLock:
    def __init__(self, active_pod_id_path: str) -> None:
        self._path = Path(
            f"{active_pod_id_path}.deploy.lock"
            if active_pod_id_path
            else "/tmp/speakr-runpod-deploy.lock"
        )

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        lock_file = await asyncio.to_thread(self._acquire)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release, lock_file)

    def _acquire(self) -> TextIO:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._path.open("a+", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return lock_file

    def _release(self, lock_file: TextIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
