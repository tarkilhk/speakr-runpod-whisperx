"""Wire contract for bounded ASR result retrieval across RunPod TCP forwarding."""

PROTOCOL_HEADER = "X-ASR-Result-Protocol"
PROTOCOL_VERSION = "chunk-v1"
CHUNK_SIZE = 8 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024
