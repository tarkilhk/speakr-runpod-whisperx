class TemporaryRunPodError(Exception):
    pass


class RunPodTimeoutError(Exception):
    pass


class BadUpstreamResponseError(Exception):
    pass


class ConfigurationError(Exception):
    pass


class RunPodNotFoundError(Exception):
    pass
