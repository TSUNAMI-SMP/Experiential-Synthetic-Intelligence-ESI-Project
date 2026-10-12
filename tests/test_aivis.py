import httpx
import pytest

from esi.adapters import TTS
from esi.models import Settings


@pytest.mark.parametrize("fault", ["missing_model", "wrong_version", "other_speaker", "missing_style"])
def test_requested_aivis_voice_cannot_silently_fall_back(fault):
    settings = Settings()

    def response(request):
        if request.url.path.startswith("/aivm_models/"):
            if fault == "missing_model":
                return httpx.Response(404, json={"detail": "not installed"})
            return httpx.Response(
                200,
                json={
                    "manifest": {
                        "uuid": settings.tts_model_uuid,
                        "name": "凛音エル",
                        "version": "9.0.0" if fault == "wrong_version" else "1.0.0",
                        "speakers": [{"uuid": settings.tts_speaker_uuid}],
                    }
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "name": "凛音エル",
                    "speaker_uuid": "unrelated-speaker"
                    if fault == "other_speaker"
                    else settings.tts_speaker_uuid,
                    "styles": [
                        {
                            "id": 1388823424,
                            "name": "unrequested style" if fault == "missing_style" else "ノーマル",
                        }
                    ],
                }
            ],
        )

    with httpx.Client(base_url="http://aivis-test", transport=httpx.MockTransport(response)) as client:
        with pytest.raises((ValueError, RuntimeError)):
            TTS(settings).resolve_voice(client)
