from pathlib import Path

_BUNDLE_ROOT = Path(__file__).resolve().parent
_DEFAULT_PACKAGE_DIR = _BUNDLE_ROOT / "package"
import ctypes
import os
import sys

if str(_BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_ROOT))

_SUPPORT_DIR = _BUNDLE_ROOT / ".opt32_support"


def _preload_support_libraries():
    if not _SUPPORT_DIR.exists():
        return
    mode = getattr(ctypes, "RTLD_GLOBAL", getattr(os, "RTLD_GLOBAL", 0))
    for candidate in sorted(_SUPPORT_DIR.glob("*.so*")):
        ctypes.CDLL(str(candidate), mode=mode)


_preload_support_libraries()

from opt32_client_runtime import ACTConfig
from opt32_client_runtime import ACTPolicy as _RuntimePolicyBase


class ACTPolicy(_RuntimePolicyBase):
    def __init__(self, package_dir=_DEFAULT_PACKAGE_DIR, **kwargs):
        super().__init__(package_dir=package_dir, **kwargs)


def load_policy(**kwargs):
    return ACTPolicy(**kwargs)
