from typing import Any

from adapter.errors import BadUpstreamResponseError


def extract_tcp_mapping(pod: dict[str, Any], wrapper_port: int) -> tuple[str, int] | None:
    for item in walk_dicts(pod):
        mapping = item.get("portMappings")
        if isinstance(mapping, dict):
            found = _mapping_from_port_mappings(pod, mapping, wrapper_port)
            if found:
                return found

        ports = item.get("ports")
        if isinstance(ports, list):
            found = _mapping_from_ports(pod, ports, wrapper_port)
            if found:
                return found

    return None


def startup_progress_fingerprint(pod: dict[str, Any]) -> tuple[Any, ...]:
    """Signals RunPod may update while a pod is still warming up (before runtime/ports exist).

    RunPod GraphQL does **not** expose Docker layer/pull progress. We use coarse lifecycle
    fields from the schema (`dockerId`, `lastStartedAt`, `machine.podHostId`, telemetry,
    etc.). Any change resets the stuck-init countdown.

    See docs/runpod-graphql-api.md ("Warmup vs docker pull").
    """
    telemetry = pod.get("latestTelemetry")
    tel_state = None
    if isinstance(telemetry, dict):
        tel_state = telemetry.get("state")
    machine = pod.get("machine")
    pod_host_id = machine.get("podHostId") if isinstance(machine, dict) else None
    # Tuple order is arbitrary but must stay stable (compared across polls).
    return (
        pod.get("desiredStatus"),
        pod.get("dockerId"),
        pod.get("lastStatusChange"),
        pod.get("lastStartedAt"),
        pod.get("uptimeSeconds"),
        pod.get("version"),
        tel_state,
        pod_host_id,
        pod.get("runtime") is not None,
    )


def warmup_fingerprint_kv(fp: tuple[Any, ...]) -> str:
    """Compact key=value line for logs when fingerprint tuple appears frozen or changed."""
    if len(fp) < 9:
        return f"fingerprint_partial={fp!r}"
    docker_raw = fp[1]
    docker = "set" if docker_raw else "none"
    return (
        f"desiredStatus={fp[0]!r} dockerId={docker} lastStatusChange={fp[2]!r} "
        f"lastStartedAt={fp[3]!r} uptimeSeconds={fp[4]!r} version={fp[5]!r} "
        f"telemetry.state={fp[6]!r} podHostId={fp[7]!r} has_runtime={fp[8]!r}"
    )


def warmup_status_kv(pod: dict[str, Any], *, wrapper_port: int | None = None) -> str:
    """Single-line lifecycle snapshot for INFO logs (no secrets)."""
    telemetry = pod.get("latestTelemetry")
    tel_state = telemetry.get("state") if isinstance(telemetry, dict) else None
    tel_time = telemetry.get("time") if isinstance(telemetry, dict) else None
    machine = pod.get("machine")
    pod_host_id = machine.get("podHostId") if isinstance(machine, dict) else None
    docker = "set" if pod.get("dockerId") else "none"
    parts = [
        f"desiredStatus={pod.get('desiredStatus')!r}",
        f"machineId={pod.get('machineId')!r}",
        f"dockerId={docker}",
        f"lastStatusChange={pod.get('lastStatusChange')!r}",
        f"lastStartedAt={pod.get('lastStartedAt')!r}",
        f"uptimeSeconds={pod.get('uptimeSeconds')!r}",
        f"version={pod.get('version')!r}",
        f"telemetry.state={tel_state!r}",
        f"telemetry.time={tel_time!r}",
        f"podHostId={pod_host_id!r}",
        f"has_runtime={pod.get('runtime') is not None}",
        f"imageName={pod.get('imageName')!r}",
    ]
    if wrapper_port is not None:
        parts.append(f"wrapper_port={wrapper_port}")
    return " ".join(parts)


def pod_is_expected_running(pod: dict[str, Any]) -> bool:
    for item in walk_dicts(pod):
        for key in ("desiredStatus", "desired_status", "status"):
            value = item.get(key)
            if isinstance(value, str) and value.upper() in {"RUNNING", "STARTING", "PROVISIONING"}:
                return True
    return False


def extract_created_pod_id(payload: dict[str, Any]) -> str:
    for item in walk_dicts(payload):
        pod_id = item.get("id") or item.get("podId")
        if isinstance(pod_id, str) and pod_id:
            return pod_id
    raise BadUpstreamResponseError("RunPod create pod response did not include a pod ID")


def walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(walk_dicts(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_dicts(item))
    return found


def _mapping_from_port_mappings(
    pod: dict[str, Any],
    mapping: dict[str, Any],
    wrapper_port: int,
) -> tuple[str, int] | None:
    for key, value in mapping.items():
        if str(key).split("/")[0] != str(wrapper_port):
            continue
        if isinstance(value, int):
            host = _extract_public_ip(pod)
            return (host, value) if host else None
        if isinstance(value, str) and value.isdigit():
            host = _extract_public_ip(pod)
            return (host, int(value)) if host else None
        if isinstance(value, dict):
            port = _extract_public_port(value)
            host = _extract_public_ip(value) or _extract_public_ip(pod)
            return (host, port) if host and port else None
    return None


def _mapping_from_ports(pod: dict[str, Any], ports: list[Any], wrapper_port: int) -> tuple[str, int] | None:
    for port_item in ports:
        if not isinstance(port_item, dict):
            continue
        private_port = (
            port_item.get("privatePort")
            or port_item.get("containerPort")
            or port_item.get("internalPort")
            or port_item.get("port")
        )
        if str(private_port) != str(wrapper_port):
            continue
        if port_item.get("isIpPublic") is False:
            continue
        public_port = _extract_public_port(port_item)
        host = _extract_public_ip(port_item) or _extract_public_ip(pod)
        if host and public_port:
            return host, public_port
    return None


def _extract_public_ip(value: dict[str, Any]) -> str | None:
    for key in ("publicIp", "publicIP", "ip", "host"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    runtime = value.get("runtime")
    if isinstance(runtime, dict):
        return _extract_public_ip(runtime)
    return None


def _extract_public_port(value: dict[str, Any]) -> int | None:
    for key in ("publicPort", "hostPort", "externalPort"):
        candidate = value.get(key)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None
