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


def loaded_paths():
    """Observe DLLs already loaded by this process; do not load or replace them."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    result = {}
    for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudart64_12.dll"):
        handle = kernel.GetModuleHandleW(name)
        if handle:
            buffer = ctypes.create_unicode_buffer(32768)
            if kernel.GetModuleFileNameW(handle, buffer, len(buffer)):
                result[name] = buffer.value
    return result
