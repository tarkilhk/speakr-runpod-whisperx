def make_config(**overrides):
    from adapter.config import AdapterConfig

    values = {
        "runpod_graphql_url": "https://example.test/graphql",
        "runpod_api_key": "test-api-key",
        "runpod_pod_id": "",
        "runpod_template_id": "template-1",
        "runpod_gpu_type_ids": ["gpu"],
        "runpod_gpu_count": 1,
        "runpod_container_disk_gb": 0,
        "runpod_pod_name": "test-pod",
        "runpod_network_volume_id": "",
        "runpod_cloud_type": "SECURE",
        "runpod_support_public_ip": True,
        "runpod_active_pod_id_path": "",
        "runpod_idle_action": "",
        "adapter_whisperx_token": "test-token",
        "runpod_wrapper_port": 9000,
        "runpod_readiness_timeout_seconds": 30,
        "runpod_stuck_init_timeout_seconds": 300,
        "runpod_poll_interval_seconds": 0,
        "runpod_request_timeout_seconds": 1800,
        "runpod_idle_stop_seconds": 30,
        "runpod_retry_after_seconds": 300,
        "max_file_size_mb": 0,
        "log_level": "INFO",
        "adapter_drain_pod_logs_on_idle": True,
        "adapter_pod_log_fetch_timeout_seconds": 120.0,
    }
    values.update(overrides)
    return AdapterConfig(**values)
