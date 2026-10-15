"""Stage-specific speech failures; diagnostics omit synthesis text and URL queries."""

from urllib.parse import urlsplit

import httpx


def issue(exc, stage):
    detail = str(exc)
    status, endpoint = None, None
    probe = detail.lower()
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        endpoint = urlsplit(str(exc.request.url)).path
        probe = exc.response.text[:16000].lower()
        detail = f"HTTP {status} / {endpoint}\n"+exc.response.text[:700]
    memory = any(term in probe for term in ("out of memory", "failed to allocate memory", "cuda_error_memory_allocation",
                                           "cudaerrormemoryallocation", "cublas_status_alloc_failed"))
    gpu_memory = memory and any(term in probe for term in ("cuda", "cublas", "cudnn", "gpu"))
    kind = "memory" if memory else "connection" if isinstance(exc, httpx.TransportError) else "http" if status else "device" if stage == "playback" else "synthesis"
    if memory:
        message = "音声合成に必要なGPUメモリが不足しています。" if gpu_memory else "音声合成に必要なメモリが不足しています。"
    elif stage == "playback":
        message = "音声をスピーカーへ出力できません。停止後に出力機器を選び直して再試行できます。"
    elif isinstance(exc, httpx.TransportError):
        message = "音声合成ソフトと通信できません。AivisSpeechが動いているか確認してください。"
    elif status:
        message = f"音声合成ソフトがHTTP {status}を返しました。"
    else:
        message = "音声の生成に失敗しました。"
    return {"stage": stage, "kind": kind, "gpu_memory": gpu_memory, "message": message, "error_type": type(exc).__name__,
            "technical_error": detail[:700], "http_status": status, "endpoint": endpoint}


class SpeechFailure(RuntimeError):
    def __init__(self, exc, stage, audio=None):
        self.problem = issue(exc, stage)
        self.audio = audio
        super().__init__(self.problem["message"]+" "+self.problem["technical_error"])
