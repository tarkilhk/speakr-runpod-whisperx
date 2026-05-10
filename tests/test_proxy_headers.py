import unittest

from speakr_common.proxy_headers import forwarded_request_headers, forwarded_response_headers


class ProxyHeadersTests(unittest.TestCase):
    def test_request_headers_drop_hop_by_hop_and_replace_authorization(self) -> None:
        headers = {
            "Host": "adapter",
            "Connection": "keep-alive",
            "Content-Length": "123",
            "Transfer-Encoding": "chunked",
            "Authorization": "Bearer old",
            "X-Request-ID": "abc",
        }

        self.assertEqual(
            forwarded_request_headers(headers, authorization_token="new-token"),
            {"X-Request-ID": "abc", "Authorization": "Bearer new-token"},
        )

    def test_response_headers_drop_transport_and_content_type_headers(self) -> None:
        headers = {
            "Content-Encoding": "gzip",
            "Content-Length": "123",
            "Content-Type": "application/json",
            "Connection": "close",
            "X-Upstream": "whisperx",
        }

        self.assertEqual(forwarded_response_headers(headers), {"X-Upstream": "whisperx"})

    def test_request_headers_extra_excluded_drops_caller_specified_headers(self) -> None:
        headers = {"X-Keep": "yes", "X-Drop": "no", "X-Also-Drop": "no"}

        result = forwarded_request_headers(headers, extra_excluded=["X-Drop", "X-Also-Drop"])

        self.assertEqual(result, {"X-Keep": "yes"})


if __name__ == "__main__":
    unittest.main()
