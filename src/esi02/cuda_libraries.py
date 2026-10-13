"""Load installed CUDA libraries through supported DLL search paths on Windows."""

import importlib.util
import os
from pathlib import Path

_handles = []
_initialized = False


def initialize():
    global _initialized
    if os.name != "nt" or _initialized:
        return
    for module in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc"):
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            continue
        if spec is None:
            continue
        locations = list(spec.submodule_search_locations or [])
        if spec.origin:
            locations.append(str(Path(spec.origin).parent))
        for location in set(locations):
            for directory in (Path(location) / "bin", Path(location) / "lib"):
                if directory.is_dir():
                    _handles.append(os.add_dll_directory(str(directory)))
    _initialized = True
