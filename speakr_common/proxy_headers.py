from collections.abc import Iterable, Mapping

REQUEST_HEADERS_EXCLUDED = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "transfer-encoding",
}

RESPONSE_HEADERS_EXCLUDED = {
    "connection",
    "content-encoding",
    "content-length",
    "content-type",
    "transfer-encoding",
}


def forwarded_request_headers(
    headers: Mapping[str, str],
    *,
    authorization_token: str | None = None,
    extra_excluded: Iterable[str] = (),
) -> dict[str, str]:
    excluded = REQUEST_HEADERS_EXCLUDED | {header.lower() for header in extra_excluded}
    forwarded = {key: value for key, value in headers.items() if key.lower() not in excluded}
    if authorization_token is not None:
        forwarded["Authorization"] = f"Bearer {authorization_token}"
    return forwarded


def forwarded_response_headers(headers: Mapping[str, str], extra_excluded: Iterable[str] = ()) -> dict[str, str]:
    excluded = RESPONSE_HEADERS_EXCLUDED | {header.lower() for header in extra_excluded}
    return {key: value for key, value in headers.items() if key.lower() not in excluded}
