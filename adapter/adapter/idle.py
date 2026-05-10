import asyncio
import logging
from typing import Protocol

from adapter.config import AdapterConfig

logger = logging.getLogger("whisperx-adapter.idle")


class ReleasableRunPod(Protocol):
    def load_active_pod_id(self) -> str:
        pass

    async def release_idle_pod(self) -> None:
        pass


class IdleReleaseController:
    """Tracks ASR activity and releases the pod after the configured quiet period."""

    def __init__(self, config: AdapterConfig, runpod: ReleasableRunPod) -> None:
        self._config = config
        self._runpod = runpod
        self._idle_stop_task: asyncio.Task | None = None
        self._active_requests = 0

    def request_started(self) -> None:
        self._active_requests += 1
        # Work in progress: do not let a sleeping idle timer fire mid-request.
        # Idle timer reset/start stays at INFO below; per-request cancel is DEBUG to avoid doubling noise.
        if self._idle_stop_task and not self._idle_stop_task.done():
            self._idle_stop_task.cancel()
            logger.debug(
                "Idle timer cancelled (ASR started) in_flight=%s pod=%s",
                self._active_requests,
                self._active_pod_id_label(),
            )

    def request_finished(self) -> None:
        self._active_requests -= 1
        self._schedule_idle_release()

    def _schedule_idle_release(self) -> None:
        # Each completed /asr schedules a fresh timer; cancel the previous so idle is measured
        # from the last request end, not the first.
        replaced = bool(self._idle_stop_task and not self._idle_stop_task.done())
        if replaced:
            self._idle_stop_task.cancel()
        delay = self._config.runpod_idle_stop_seconds
        logger.info(
            "Idle timer %s delay=%ss action=%s in_flight=%s pod=%s",
            "reset" if replaced else "started",
            delay,
            self._config.idle_action,
            self._active_requests,
            self._active_pod_id_label(),
        )
        self._idle_stop_task = asyncio.create_task(self._release_after_idle_delay())

    async def _release_after_idle_delay(self) -> None:
        try:
            delay = self._config.runpod_idle_stop_seconds
            if delay > 0:
                await asyncio.sleep(delay)
            # Another /asr may have started during the sleep; only release when truly quiet.
            if self._active_requests != 0:
                logger.info(
                    "Idle skipped: in_flight=%s pod=%s",
                    self._active_requests,
                    self._active_pod_id_label(),
                )
                return
            logger.info(
                "Idle release pod=%s action=%s delay=%ss template_mode=%s",
                self._active_pod_id_label(),
                self._config.idle_action,
                delay,
                self._config.template_mode_enabled,
            )
            await self._runpod.release_idle_pod()
        except asyncio.CancelledError:
            # Expected when a new /asr finishes and replaces this task, or on process shutdown.
            logger.debug("Idle release timer cancelled (superseded by newer timer or shutdown)")
            return

    def _active_pod_id_label(self) -> str:
        return self._runpod.load_active_pod_id() or "(none)"
