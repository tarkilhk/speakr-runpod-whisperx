"""Fetch application logs from the RunPod wrapper over HTTPS and emit them on a dedicated logger."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from adapter.config import AdapterConfig
from adapter.pod_mapping import extract_tcp_mapping
from adapter.runpod_client import RunPodClient

logger = logging.getLogger("whisperx-adapter.pod_logs")
# Collect container logs for this logger (e.g. Grafana Alloy) and route as you like.
capture_logger = logging.getLogger("whisperx-adapter.runpod_logs")

WRAPPER_POD_LOGS_PATH = "/internal/pod-logs"
# Cap UTF-8 bytes per line when emitting (avoids oversized single records in log pipelines).
_DEFAULT_EMIT_MAX_LINE_BYTES = 65_536


def _lines_from_bundle(bundle: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (log_file, raw_line) pairs for every line in every file in the bundle."""
    files = bundle.get("files")
    if not isinstance(files, list):
        return []
    entries: list[tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        content = item.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            continue
        for raw_line in content.splitlines():
            entries.append((name, raw_line))
    return entries


_TRUNCATION_SUFFIX = "…[truncated]"
_TRUNCATION_SUFFIX_BYTES = len(_TRUNCATION_SUFFIX.encode("utf-8"))


def _emit_capture_lines(
    pod_id: str,
    entries: list[tuple[str, str]],
    *,
    max_line_bytes: int | None = None,
) -> None:
    limit = max(256, max_line_bytes if max_line_bytes is not None else _DEFAULT_EMIT_MAX_LINE_BYTES)
    for log_file, raw_line in entries:
        encoded = raw_line.encode("utf-8", errors="replace")
        if len(encoded) > limit:
            cut = max(1, limit - _TRUNCATION_SUFFIX_BYTES)
            raw_line = encoded[:cut].decode("utf-8", errors="replace") + _TRUNCATION_SUFFIX
        capture_logger.info("pod_id=%s log_file=%s %s", pod_id, log_file, raw_line)


async def _fetch_bundle(base_url: str, token: str, timeout_seconds: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{WRAPPER_POD_LOGS_PATH}"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        msg = "wrapper pod-logs response is not a JSON object"
        raise ValueError(msg)
    return data


async def drain_cloud_pod_logs(config: AdapterConfig, pod_id: str, client: RunPodClient) -> bool:
    """Resolve the public wrapper URL, pull the log bundle, emit each line on ``capture_logger``.

    Returns True when the bundle was successfully fetched and emitted, False on any operational
    failure (no token, pod lookup error, no TCP mapping, HTTP error, bad response). Failures are
    always logged as warnings so the idle-release path can stay best-effort by ignoring the return
    value; the standalone CLI uses it to set the process exit code.
    """
    if not config.adapter_whisperx_token:
        logger.warning("Pod log drain skipped: ADAPTER_WHISPERX_TOKEN is not set")
        return False

    try:
        pod = await client.get_pod(pod_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pod log drain skipped: failed to load pod %s: %s", pod_id, exc)
        return False

    mapping = extract_tcp_mapping(pod, config.runpod_wrapper_port)
    if not mapping:
        logger.warning(
            "Pod log drain skipped: no public TCP mapping for pod_id=%s port=%s",
            pod_id,
            config.runpod_wrapper_port,
        )
        return False

    base_url = f"http://{mapping[0]}:{mapping[1]}"
    timeout = float(config.adapter_pod_log_fetch_timeout_seconds)
    try:
        bundle = await _fetch_bundle(base_url, config.adapter_whisperx_token, timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pod log drain failed for pod_id=%s url=%s: %s", pod_id, base_url, exc)
        return False

    entries = _lines_from_bundle(bundle)
    logger.info(
        "Pod log drain fetched pod_id=%s lines=%s files=%s",
        pod_id,
        len(entries),
        len(bundle.get("files", [])) if isinstance(bundle.get("files"), list) else 0,
    )

    _emit_capture_lines(pod_id, entries)
    return True
