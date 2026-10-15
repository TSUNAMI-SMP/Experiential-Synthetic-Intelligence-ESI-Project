"""A capped visual JSON response must be retried, never applied as a partial observation."""

import hashlib
import time
from io import BytesIO

import httpx
import pytest
from PIL import Image

from esi.models import uid, utc
from esi02.dialogue import Cancellation
from esi04.computer_models import Foreground, Frame, Rect
from esi04.config import Settings
from esi04.runtime import Runtime
from esi04.session import Session
from esi04.vision import Observation, VisionClient, VisualDescription


def frame(tmp_path):
    path = tmp_path / "frame.png"
    raw = BytesIO()
    Image.new("RGB", (80, 60), "navy").save(raw, format="PNG")
    path.write_bytes(raw.getvalue())
    return Frame(id=uid(), sequence=1, generation=1, captured_at=time.monotonic(), observed_at=utc(),
                 path=str(path), sha256=hashlib.sha256(raw.getvalue()).hexdigest(), screen="test",
                 image_width=80, image_height=60, physical=Rect(x=0, y=0, width=80, height=60),
                 logical=Rect(x=0, y=0, width=80, height=60), foreground=Foreground())


@pytest.mark.parametrize("second_good", [True, False])
def test_capped_output_retries_once_then_returns_valid_json_or_a_useful_error(tmp_path, second_good):
    calls = []
    def response(request):
        import json
        calls.append(json.loads(request.content))
        good = second_good and len(calls) == 2
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop" if good else "length",
            "message": {"content": '{"description":"青い画面","visible_text":[],"targets":[],"uncertain":true}'
                        if good else '{"description":"途中'}}]})
    def client(**kwargs):
        return httpx.Client(**kwargs, transport=httpx.MockTransport(response))
    vision = VisionClient(Settings(), client_factory=client)
    if second_good:
        assert vision.observe(frame(tmp_path), "何が見える？", Cancellation()).result.description == "青い画面"
    else:
        with pytest.raises(RuntimeError, match="最後まで"):
            vision.observe(frame(tmp_path), "何が見える？", Cancellation())
    assert len(calls) == 2 and calls[1]["max_tokens"] >= 1024


def test_phase_failure_keeps_the_original_exception_and_computer_errors_are_separate(tmp_path):
    session = Session(tmp_path / "save.db", Settings(idle_deliberation=False))
    runtime = Runtime(session)
    try:
        runtime._status("error", "reply：具体的な原因")
        runtime._status("phase", "failed")
        assert session.status["error"] == "reply：具体的な原因"
        runtime._status("phase", "waiting")
        runtime._status("error", "")
        runtime._handle("computer", ("observe", {}))
        assert "接続" in session.status["computer_error"]
        assert session.status["phase"] == "waiting" and not session.status["error"]
    finally:
        runtime.stop()
        session.close()


def test_short_inference_schema_does_not_break_long_observations_in_existing_saves():
    old = Observation(id=uid(), frame_id=uid(), frame_sha256="a"*64, observed_at=utc(), question="以前の画面",
                      result=VisualDescription(description="過去の長い読み取り。"*100,
                                               visible_text=[str(i) for i in range(35)]))
    assert Observation.model_validate_json(old.model_dump_json()) == old
