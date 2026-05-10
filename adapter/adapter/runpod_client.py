import logging
from typing import Any

import httpx

from adapter.config import AdapterConfig
from adapter.errors import BadUpstreamResponseError, ConfigurationError, RunPodNotFoundError, TemporaryRunPodError
from adapter.pod_mapping import extract_created_pod_id

logger = logging.getLogger("whisperx-adapter.runpod_client")

# Pod poll query: minimal GraphQL selection for TCP/public-port discovery, stuck-init (machineId
# vs runtime + fingerprint), and startup_progress_fingerprint(). RunPod does not expose Docker
# pull progress; coarse field twitching resets stuck-init — see docs/runpod-graphql-api.md.
POD_FIELDS = """
id
desiredStatus
lastStatusChange
lastStartedAt
version
uptimeSeconds
machineId
machine {
  podHostId
}
latestTelemetry {
  state
}
runtime {
  ports {
    ip
    isIpPublic
    privatePort
    publicPort
    type
  }
}
"""

GET_POD_QUERY = f"""
query Pod($input: PodFilter) {{
  pod(input: $input) {{
    {POD_FIELDS}
  }}
}}
"""

DEPLOY_POD_MUTATION = """
mutation DeployPod($input: PodFindAndDeployOnDemandInput) {
  podFindAndDeployOnDemand(input: $input) {
    id
    imageName
    machineId
  }
}
"""

RESUME_POD_MUTATION = """
mutation ResumePod($input: PodResumeInput!) {
  podResume(input: $input) {
    id
    desiredStatus
  }
}
"""

STOP_POD_MUTATION = """
mutation StopPod($input: PodStopInput!) {
  podStop(input: $input) {
    id
    desiredStatus
  }
}
"""

TERMINATE_POD_MUTATION = """
mutation TerminatePod($input: PodTerminateInput!) {
  podTerminate(input: $input)
}
"""


class RunPodClient:
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    async def get_pod(self, pod_id: str) -> dict[str, Any]:
        payload = await self._graphql(GET_POD_QUERY, {"input": {"podId": pod_id}}, "pod")
        if not isinstance(payload, dict):
            raise RunPodNotFoundError(f"RunPod pod {pod_id} was not found")
        return payload

    async def start_pod(self, pod_id: str) -> None:
        await self._graphql(RESUME_POD_MUTATION, {"input": {"podId": pod_id, "gpuCount": self.config.runpod_gpu_count}}, "podResume")

    async def stop_pod(self, pod_id: str) -> None:
        await self._graphql(STOP_POD_MUTATION, {"input": {"podId": pod_id}}, "podStop")

    async def terminate_pod(self, pod_id: str) -> None:
        await self._graphql(TERMINATE_POD_MUTATION, {"input": {"podId": pod_id}}, "podTerminate")

    async def deploy_from_template(self) -> str:
        if not self.config.runpod_template_id:
            raise ConfigurationError("RUNPOD_TEMPLATE_ID is not configured")
        if not self.config.runpod_gpu_type_ids:
            raise ConfigurationError("RUNPOD_GPU_TYPE_IDS is not configured")

        body: dict[str, Any] = {
            "name": self.config.runpod_pod_name,
            "templateId": self.config.runpod_template_id,
            "gpuTypeIdList": self.config.runpod_gpu_type_ids,
            "gpuCount": self.config.runpod_gpu_count,
            "cloudType": self.config.runpod_cloud_type,
            "supportPublicIp": self.config.runpod_support_public_ip,
        }
        if self.config.runpod_container_disk_gb:
            body["containerDiskInGb"] = self.config.runpod_container_disk_gb
        if self.config.runpod_network_volume_id:
            body["networkVolumeId"] = self.config.runpod_network_volume_id

        logger.info(
            "Deploying RunPod template_id=%s cloud_type=%s gpu_count=%s disk_gb=%s name=%s",
            self.config.runpod_template_id,
            self.config.runpod_cloud_type,
            self.config.runpod_gpu_count,
            self.config.runpod_container_disk_gb or "(omit)",
            self.config.runpod_pod_name,
        )
        payload = await self._graphql(DEPLOY_POD_MUTATION, {"input": body}, "podFindAndDeployOnDemand")
        pod_id = extract_created_pod_id(payload)
        logger.info("Created RunPod pod %s", pod_id)
        return pod_id

    async def _graphql(self, query: str, variables: dict[str, Any], data_key: str) -> Any:
        self._require_api_key()
        timeout = httpx.Timeout(60, connect=30)
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Bearer keeps the key out of the URL (httpx logs URLs at INFO if enabled).
            response = await client.post(
                self.config.runpod_graphql_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.runpod_api_key}",
                },
                json={"query": query, "variables": variables},
            )

        error_text = response.text[:500]
        if response.status_code >= 500 or response.status_code in {408, 409, 423, 429} or _is_capacity_error(error_text):
            raise TemporaryRunPodError(f"RunPod GraphQL returned {response.status_code}: {error_text}")
        if response.status_code >= 400:
            raise TemporaryRunPodError(f"RunPod GraphQL returned {response.status_code}: {error_text}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise BadUpstreamResponseError("RunPod GraphQL returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise BadUpstreamResponseError("RunPod GraphQL returned a non-object response")

        errors = payload.get("errors")
        if errors:
            error_text = str(errors)[:500]
            if _is_not_found_error(error_text):
                raise RunPodNotFoundError(error_text)
            raise TemporaryRunPodError(f"RunPod GraphQL errors: {error_text}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise BadUpstreamResponseError("RunPod GraphQL response did not include data")
        return data.get(data_key)

    def _require_api_key(self) -> None:
        if not self.config.runpod_api_key:
            raise ConfigurationError("RUNPOD_API_KEY is not configured")


def _is_capacity_error(error_text: str) -> bool:
    lower_error = error_text.lower()
    return any(
        phrase in lower_error
        for phrase in (
            "not enough free gpu",
            "no gpu",
            "no available",
            "capacity",
        )
    )


def _is_not_found_error(error_text: str) -> bool:
    lower_error = error_text.lower()
    return "not found" in lower_error or "does not exist" in lower_error
