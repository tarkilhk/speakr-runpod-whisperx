import logging


class QuietUvicornAccessFilter(logging.Filter):
    """Swagger UI polls /docs and /openapi.json; omit those from access logs."""

    _SKIP = (' "GET /docs ', ' "GET /openapi.json ', ' "GET /redoc ')

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(fragment in msg for fragment in self._SKIP)
