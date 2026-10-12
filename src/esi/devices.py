"""Local audio routing preferences, independent of an experiment's numerical settings."""

import json
from pathlib import Path


def audio_devices():
    import sounddevice as sd

    hosts = sd.query_hostapis()
    return [
        {
            "index": index,
            "name": device["name"],
            "hostapi": hosts[device["hostapi"]]["name"],
            "inputs": device["max_input_channels"],
            "outputs": device["max_output_channels"],
        }
        for index, device in enumerate(sd.query_devices())
    ]


def identity(device):
    return {key: device[key] for key in ("name", "hostapi")} if device else None


def resolve_device(selected, devices, direction):
    """Resolve stable identity at startup; never reuse a stale numeric index silently."""
    if selected is None:
        return None
    matches = [d for d in devices if d[direction] > 0 and identity(d) == identity(selected)]
    if len(matches) > 1:
        explicit = [d for d in matches if d["index"] == selected.get("index")]
        if len(explicit) == 1:
            return explicit[0]["index"]
    if len(matches) != 1:
        raise ValueError(
            "選んだ音声機器が見つからないか、同じ名前が複数あります。機器一覧を更新して選び直してください。"
        )
    return matches[0]["index"]


def stream_format(device, direction, sample_rate, channels=1):
    """Use a validated native format when a device cannot accept 16 kHz mono."""
    import sounddevice as sd

    details = sd.query_devices(device, direction)
    maximum = details[f"max_{direction}_channels"]
    if maximum < 1:
        raise ValueError("選んだ機器はこの方向の音声に対応していません。")
    native = int(details["default_samplerate"])
    candidates = [(sample_rate, channels), (native, channels), (native, min(2, maximum))]
    check = sd.check_input_settings if direction == "input" else sd.check_output_settings
    last = None
    for rate, count in dict.fromkeys(candidates):
        try:
            check(device=device, samplerate=rate, channels=count, dtype="float32")
            return rate, count, details["name"]
        except (sd.PortAudioError, ValueError) as exc:
            last = exc
    raise RuntimeError(
        f"選んだ音声機器を開けません。別の機器を選ぶかWindowsの音声設定を確認してください：{last}"
    )


def load_preferences(path):
    path = Path(path)
    if not path.is_file():
        return {"input": None, "output": None, "microphone_on_start": True}
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("input", "output"):
        if data.get(key) is not None and (
            not isinstance(data[key], dict)
            or not all(isinstance(data[key].get(k), str) for k in ("name", "hostapi"))
        ):
            raise ValueError("音声機器の保存設定を読めません。機器を選び直してください。")
    return {
        "input": data.get("input"),
        "output": data.get("output"),
        "microphone_on_start": bool(data.get("microphone_on_start", True)),
    }


def save_preferences(path, selected_input, selected_output, microphone_on_start):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "input": identity(selected_input),
                "output": identity(selected_output),
                "microphone_on_start": bool(microphone_on_start),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
