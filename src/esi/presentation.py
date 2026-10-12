"""Plain-language display of measured stages, without inventing motives or observations."""

ACTION_LABELS = {
    "wait": "待つ",
    "reprocess": "経験を見直す",
    "think": "検討する",
    "inquire": "確認のために話す",
    "speak": "話す",
    "rest": "休む",
}
SOURCE_LABELS = {"microphone": "マイク", "recording": "録音", "simulated": "文字・模擬入力"}


def describe(view, running):
    status = view["status"]
    microphone = status.get("microphone", "not_started")
    mic_active = microphone not in {"not_started", "stopped"}
    audio = status.get("audio_input", {})
    jobs = status.get("jobs", [])
    action = view.get("input_action")
    result = {
        "phase": "waiting",
        "title": "音声を待っています",
        "detail": "短く話した後、少し間を空けてください。",
        "error": False,
    }
    if not running:
        return {
            **result,
            "phase": "stopped",
            "title": "停止中",
            "detail": "マイクを選んで「開始」を押してください。",
        }
    labels = {
        "core": "本体",
        "microphone": "マイク",
        "vad": "声の検出",
        "asr": "聞き取り",
        "llm": "言葉の検討",
        "tts": "声の作成",
        "playback": "音声再生",
        "operation": "操作",
    }
    for component, label in labels.items():
        message = status.get(component, "")
        if isinstance(message, str) and message.startswith("failed"):
            if component == "llm" and any(t in message.lower() for t in ("connect", "refused")):
                hint = "言語ソフトにつながりません。llama.cppのウィンドウが開いているか確認してください。"
            elif component == "tts" and any(t in message.lower() for t in ("connect", "refused")):
                hint = "音声ソフトにつながりません。AivisSpeechが起動しているか確認してください。"
            elif component == "asr" and "FileNotFoundError" in message:
                hint = "音声認識用のデータが見つかりません。AIデータのダウンロードが完了しているか確認してください。"
            else:
                hint = "停止して設定を確認してください。詳しい内容は「詳しい観察」にも表示します。"
            return {
                **result,
                "phase": "error",
                "title": f"{label}で問題が起きています",
                "detail": hint + "\n" + message.removeprefix("failed: ")[:280],
                "error": True,
            }
    if audio.get("asr_busy"):
        return {
            **result,
            "phase": "recognizing",
            "title": "音声を言葉に変換しています",
            "detail": f"処理中 {audio.get('asr_seconds', 0):.0f}秒。初回は音声認識モデルの読込みも行います。",
        }
    phases = {
        "playback": ("playing", "選んだスピーカーに音声を再生しています"),
        "synthesis": ("synthesizing", "凛音エルの声を作っています"),
        "verbalize": ("generating", "選ばれた発声の文章を準備しています"),
        "interpret": ("interpreting", "聞き取った内容を検討しています"),
        "recording": ("recognizing", "録音ファイルを聞き取っています"),
    }
    for kind, (phase, title) in phases.items():
        job = next((j for j in jobs if j["kind"] == kind), None)
        if job:
            if job.get("stale") and status.get("pending_input"):
                return {
                    **result,
                    "phase": "queued",
                    "title": "前の入力の処理が終わるのを待っています",
                    "detail": f"最新の入力は記録済みです。処理待ち {job.get('seconds', 0):.0f}秒。",
                }
            return {
                **result,
                "phase": phase,
                "title": title,
                "detail": f"処理中 {job.get('seconds', 0):.0f}秒。CPUでの検討には時間がかかることがあります。",
            }
    if audio.get("speaking") and mic_active:
        return {
            **result,
            "phase": "listening",
            "title": "声らしい音を検出しています",
            "detail": "話し終わると、聞き取りに進みます。",
        }
    if (audio.get("seconds_since_packet") or 0) >= 2 and mic_active:
        return {
            **result,
            "phase": "error",
            "title": "マイクからの音声データが途絶えています",
            "detail": "マイクの接続を確認し、停止して機器を選び直してください。",
            "error": True,
        }
    if action:
        label = ACTION_LABELS[action["kind"]]
        if action["stage"] in {"failed", "unknown", "cancelled"}:
            title = {
                "failed": "処理に失敗しました",
                "unknown": "処理が時間切れになりました",
                "cancelled": "前の処理を取り消しました",
            }[action["stage"]]
            return {
                **result,
                "phase": "error",
                "title": title,
                "detail": "下の動作記録と「詳しい観察」の技術状態を確認してください。",
                "error": action["stage"] != "cancelled",
            }
        return {
            **result,
            "phase": "decided",
            "title": f"入力を確認：今回は「{label}」を選びました",
            "detail": "候補の評価値を比較した結果です。新しい音声の入力も待っています。",
        }
    if microphone in {"not_started", "stopped"}:
        return {
            **result,
            "title": "マイクはOFFです",
            "detail": "「マイクを使う」を押すか、録音・文字を入力してください。",
        }
    if not audio.get("packets_received", 0):
        return {
            **result,
            "title": "マイクからの音声データを待っています",
            "detail": "この表示が続く場合は、マイクの選択とWindowsのマイク設定を確認してください。",
        }
    if audio.get("peak", 0) < 0.0001:
        return {
            **result,
            "title": "マイクの入力は届いていますが、ほぼ無音です",
            "detail": "話しても音量が動かなければ、機器の選択・ミュート・Windowsの録音音量を確認してください。",
        }
    return result
