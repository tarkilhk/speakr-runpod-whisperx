import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_ROOT = _REPO_ROOT / "adapter"

for _path in (str(_REPO_ROOT), str(_ADAPTER_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
