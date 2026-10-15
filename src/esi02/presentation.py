"""Short, distinct activity labels used by both main and floating views."""

PHASES = {
    "stopped": ("stop", "停止中"),
    "waiting": ("wait", "話しかけてください"),
    "queued": ("wait", "前の処理を止めて返答を準備中"),
    "generating": ("think", "返答の文章を考えています"),
    "synthesizing": ("speak", "凛音エルの声を作っています"),
    "playing": ("speak", "音声を再生しています"),
    "failed": ("error", "処理に問題が起きています"),
    "speech_failed": ("error", "返答文はできましたが、音声に問題があります"),
}
BODY_SYMBOLS = {
    "rest": "rest",
    "drink": "water",
    "explore": "explore",
    "ask_cool": "temperature",
    "ask_warm": "temperature",
    "ask_quiet": "ear",
    "ask_talk": "speak",
}


def activity(status):
    phase = status.get("phase", "stopped")
    if phase == "waiting":
        if status.get("request_phase") in {"synthesizing", "playing"}:
            return "speak", "ESIからのお願いを声にしています"
        if status.get("audio_input", {}).get("asr_busy"):
            return "ear", "聞いた声を言葉に変換しています"
        if status.get("memory_phase") == "organizing":
            return "reprocess", "会話から記憶を整理中（話しかけられます）"
    return PHASES.get(phase, ("wait", "状態を確認中"))
