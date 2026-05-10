import os
from dataclasses import dataclass


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def _env_csv(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


@dataclass(frozen=True)
class AdapterConfig:
    runpod_graphql_url: str
    runpod_api_key: str
    runpod_pod_id: str
    runpod_template_id: str
    runpod_gpu_type_ids: list[str]
    runpod_gpu_count: int
    runpod_container_disk_gb: int
    runpod_pod_name: str
    runpod_network_volume_id: str
    runpod_cloud_type: str
    runpod_support_public_ip: bool
    runpod_active_pod_id_path: str
    runpod_idle_action: str
    adapter_whisperx_token: str
    runpod_wrapper_port: int
    runpod_readiness_timeout_seconds: int
    runpod_stuck_init_timeout_seconds: int
    runpod_poll_interval_seconds: int
    runpod_request_timeout_seconds: int
    runpod_idle_stop_seconds: int
    runpod_retry_after_seconds: int
    max_file_size_mb: int
    log_level: str

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        return cls(
            runpod_graphql_url=os.getenv(
                "RUNPOD_GRAPHQL_URL",
                os.getenv("RUNPOD_API_BASE", "https://api.runpod.io/graphql"),
            ),
            runpod_api_key=os.getenv("RUNPOD_API_KEY", ""),
            runpod_pod_id=os.getenv("RUNPOD_POD_ID", ""),
            runpod_template_id=os.getenv("RUNPOD_TEMPLATE_ID", ""),
            runpod_gpu_type_ids=_env_csv("RUNPOD_GPU_TYPE_IDS"),
            runpod_gpu_count=int(os.getenv("RUNPOD_GPU_COUNT", "1")),
            runpod_container_disk_gb=int(os.getenv("RUNPOD_CONTAINER_DISK_GB", "0")),
            runpod_pod_name=os.getenv("RUNPOD_POD_NAME", "speakr-whisperx"),
            runpod_network_volume_id=os.getenv("RUNPOD_NETWORK_VOLUME_ID", ""),
            runpod_cloud_type=os.getenv("RUNPOD_CLOUD_TYPE", "SECURE").upper(),
            runpod_support_public_ip=_env_bool("RUNPOD_SUPPORT_PUBLIC_IP", "true"),
            runpod_active_pod_id_path=os.getenv(
                "RUNPOD_ACTIVE_POD_ID_PATH",
                "/tmp/speakr-runpod-active-pod-id",
            ),
            runpod_idle_action=os.getenv("RUNPOD_IDLE_ACTION", "").lower(),
            adapter_whisperx_token=os.getenv("ADAPTER_WHISPERX_TOKEN", ""),
            runpod_wrapper_port=int(os.getenv("RUNPOD_WRAPPER_PORT", "9000")),
            runpod_readiness_timeout_seconds=int(os.getenv("RUNPOD_READINESS_TIMEOUT_SECONDS", "600")),
            runpod_stuck_init_timeout_seconds=int(os.getenv("RUNPOD_STUCK_INIT_TIMEOUT_SECONDS", "120")),
            runpod_poll_interval_seconds=int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "5")),
            runpod_request_timeout_seconds=int(os.getenv("RUNPOD_REQUEST_TIMEOUT_SECONDS", "1800")),
            runpod_idle_stop_seconds=int(os.getenv("RUNPOD_IDLE_STOP_SECONDS", "900")),
            runpod_retry_after_seconds=int(os.getenv("RUNPOD_RETRY_AFTER_SECONDS", "300")),
            max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "0")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def template_mode_enabled(self) -> bool:
        return bool(self.runpod_template_id)

    @property
    def idle_action(self) -> str:
        if self.runpod_idle_action:
            return self.runpod_idle_action
        return "terminate" if self.template_mode_enabled else "stop"
