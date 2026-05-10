import unittest
from unittest.mock import AsyncMock, patch

import httpx

from adapter.wrapper_health import wrapper_healthy_detail


class FakeAsyncClient:
    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None, **_kwargs) -> None:
        self._response = response
        self._exc = exc
        self.get = AsyncMock(side_effect=exc, return_value=response)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_args) -> None:
        return None


class WrapperHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_200_is_healthy(self) -> None:
        with patch("adapter.wrapper_health.httpx.AsyncClient", return_value=FakeAsyncClient(httpx.Response(200))):
            self.assertEqual(await wrapper_healthy_detail("http://pod"), (True, "status=200"))

    async def test_non_200_is_unhealthy_with_status_detail(self) -> None:
        with patch("adapter.wrapper_health.httpx.AsyncClient", return_value=FakeAsyncClient(httpx.Response(503))):
            self.assertEqual(await wrapper_healthy_detail("http://pod"), (False, "http_status=503"))

    async def test_http_error_is_unhealthy_with_error_detail(self) -> None:
        error = httpx.ConnectError("no route")
        with patch("adapter.wrapper_health.httpx.AsyncClient", return_value=FakeAsyncClient(exc=error)):
            healthy, detail = await wrapper_healthy_detail("http://pod")

        self.assertFalse(healthy)
        self.assertIn("ConnectError", detail)


if __name__ == "__main__":
    unittest.main()
