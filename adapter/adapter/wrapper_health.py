import httpx


async def wrapper_healthy_detail(base_url: str) -> tuple[bool, str]:
    """Return (healthy, detail) where detail is safe for logs."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{base_url}/health")
        if response.status_code == 200:
            return True, "status=200"
        return False, f"http_status={response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
