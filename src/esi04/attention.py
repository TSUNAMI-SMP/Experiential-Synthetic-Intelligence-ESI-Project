"""Semantic screen changes are events for attention, not a periodic question timer."""

import hashlib
import json

import numpy as np
from PIL import Image


def screen_context(frame):
    with Image.open(frame.path) as image:
        thumbnail = np.asarray(image.convert("RGB").resize((12, 8)), dtype=np.uint8)
    quantized = (thumbnail//24).tobytes()
    labels = [e.name[:60] for e in frame.elements if e.name and not e.protected][:12]
    basis = json.dumps([frame.foreground.application, frame.foreground.title[:120], labels], ensure_ascii=False).encode()
    key = "screen:"+hashlib.sha256(basis+quantized).hexdigest()[:24]
    return {"id": key, "application": frame.foreground.application, "title": frame.foreground.title[:120],
            "controls": labels, "reason": "共有が許可されている画面に、まだ確かめていない内容がある"}
