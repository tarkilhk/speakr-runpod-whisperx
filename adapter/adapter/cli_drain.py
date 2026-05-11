"""Standalone pod-log drain: resolve active pod, pull cloud logs, emit to stdout, then exit.

Run as:  python -m adapter.cli_drain
Or via docker:  docker run --rm --env-file .env <image> python -m adapter.cli_drain

The drain always runs regardless of ADAPTER_DRAIN_POD_LOGS_ON_IDLE (that flag only gates the
automatic idle-release hook in the adapter process). Lines are always emitted on
``whisperx-adapter.runpod_pod_capture``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from adapter.config import AdapterConfig
from adapter.pod_logs import drain_cloud_pod_logs
from adapter.pod_state import ActivePodStore
from adapter.runpod_client import RunPodClient

_log = logging.getLogger("whisperx-adapter.cli_drain")


async def _run(config: AdapterConfig) -> int:
    if not config.runpod_api_key:
        _log.error("RUNPOD_API_KEY is not set")
        return 1
    if not config.adapter_whisperx_token:
        _log.error("ADAPTER_WHISPERX_TOKEN is not set")
        return 1

    store = ActivePodStore(config.runpod_pod_id, config.runpod_active_pod_id_path)
    pod_id = store.load()
    if not pod_id:
        _log.error(
            "No active pod ID found. Set RUNPOD_POD_ID or ensure %s exists.",
            config.runpod_active_pod_id_path or "RUNPOD_ACTIVE_POD_ID_PATH",
        )
        return 1

    _log.info("Draining cloud pod logs pod_id=%s", pod_id)
    client = RunPodClient(config)
    ok = await drain_cloud_pod_logs(config, pod_id, client)
    if not ok:
        _log.error("Pod log drain failed for pod_id=%s — see preceding warnings", pod_id)
        return 1
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    config = AdapterConfig.from_env()
    logging.getLogger().setLevel(config.log_level)
    sys.exit(asyncio.run(_run(config)))


if __name__ == "__main__":
    main()
