"""GPU execution settings and private fixed-sample benchmarks."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QWidget,
)

from esi.models import uid
from esi02.desktop_preferences import write_json
from esi02.dialogue import Cancellation
from esi03.performance import benchmark_asr, benchmark_language


class PerformancePanel(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.loaded_agent = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esi03-benchmark")
        self.future, self.cancellation = None, None
        layout = QFormLayout(self)
        self.language_device = self.combo([("自動", "auto"), ("CPU", "cpu"), ("GPU必須", "cuda")])
        self.asr_device = self.combo([("自動", "auto"), ("CPU", "cpu"), ("GPU必須", "cuda")])
        self.context = QSpinBox()
        self.context.setRange(2048, 8192)
        self.context.setSingleStep(512)
        self.flash = self.combo([("自動", "auto"), ("ON", "on"), ("OFF", "off")])
        self.cache = self.combo([("Q8（標準）", "q8_0"), ("F16", "f16"), ("Q4（容量を抑える）", "q4_0")])
        self.asr_precision = self.combo(
            [
                ("自動", "auto"),
                ("GPU F16を優先", "float16"),
                ("F32を優先", "float32"),
                ("GPU INT8/F16を優先", "int8_float16"),
                ("CPU INT8", "int8"),
            ]
        )
        self.reserve = QSpinBox()
        self.reserve.setRange(512, 8192)
        self.reserve.setSingleStep(256)
        self.reserve.setSuffix(" MB")
        for label, widget in [
            ("会話の実行先", self.language_device),
            ("聞き取りの実行先", self.asr_device),
            ("会話の文脈トークン数", self.context),
            ("FlashAttention", self.flash),
            ("KVキャッシュ", self.cache),
            ("聞き取りの計算形式", self.asr_precision),
            ("GPUメモリーの余裕", self.reserve),
        ]:
            layout.addRow(label, widget)
        self.apply = QPushButton("停止中の実行設定を保存・接続を準備")
        self.apply.clicked.connect(self.save_settings)
        layout.addRow(self.apply)
        explanation = QLabel(
            "Qwenの重みはQ4_K_Mです。NVFP4への変換は行いません。聞き取りは実際の計算で対応を確認し、使用した形式を表示します。"
        )
        explanation.setWordWrap(True)
        layout.addRow(explanation)
        self.language_test = QPushButton("会話モデルを3回計測する")
        self.language_test.clicked.connect(lambda: self.start("language"))
        self.asr_test = QPushButton("30秒以内の音声で聞き取りを計測する")
        self.asr_test.clicked.connect(self.choose_audio)
        self.cancel = QPushButton("計測を中止")
        self.cancel.clicked.connect(self.stop_benchmark)
        layout.addRow(self.language_test)
        layout.addRow(self.asr_test)
        layout.addRow(self.cancel)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        layout.addRow(self.result)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.load_controls()

    @staticmethod
    def combo(values):
        widget = QComboBox()
        for label, value in values:
            widget.addItem(label, value)
        return widget

    def load_controls(self):
        session = self.controller.session
        settings = session.settings
        for widget, value in [
            (self.language_device, settings.llm_device),
            (self.asr_device, settings.asr_device),
            (self.flash, settings.flash_attention),
            (self.cache, settings.cache_type),
            (self.asr_precision, settings.asr_compute_type),
        ]:
            widget.setCurrentIndex(widget.findData(value))
        self.context.setValue(settings.context_tokens)
        self.reserve.setValue(settings.gpu_reserve_mb)
        self.loaded_agent = session.agent_id

    def save_settings(self):
        if self.controller.runtime or self.future or self.controller.preparation:
            self.result.setPlainText("会話・計測・接続の準備を止めてから設定を変更してください。")
            return
        try:
            data = self.controller.session.settings.model_dump()
            data.update(
                llm_device=self.language_device.currentData(),
                asr_device=self.asr_device.currentData(),
                context_tokens=self.context.value(),
                flash_attention=self.flash.currentData(),
                cache_type=self.cache.currentData(),
                asr_compute_type=self.asr_precision.currentData(),
                gpu_reserve_mb=self.reserve.value(),
            )
            self.controller.session.change_runtime_settings(data)
            self.result.setPlainText("設定を保存しました。接続を確認し直します。")
            if self.controller.services:
                self.controller.connection_ready = False
                self.controller.retry_connection()
        except Exception as exc:
            self.result.setPlainText(str(exc))

    def choose_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, "計測する音声を選択", "", "音声 (*.wav *.flac *.ogg)")
        if path:
            self.start("asr", path)

    def start(self, kind, path=None):
        if self.controller.runtime or self.future or self.controller.preparation:
            self.result.setPlainText("会話を停止し、接続の準備が終わってから計測してください。")
            return
        settings = self.controller.session.settings.model_copy(deep=True)
        self.cancellation = Cancellation()
        cancellation = self.cancellation
        if kind == "language":
            backend = (
                self.controller.services.current_backend(settings)
                if hasattr(self.controller.services, "current_backend")
                else dict(self.controller.backend)
            )
            self.future = self.pool.submit(benchmark_language, settings, cancellation, backend)
        else:
            self.future = self.pool.submit(
                benchmark_asr,
                settings,
                path,
                self.controller.session.store.path.parent / "benchmark-audio",
                cancellation,
            )
        self.result.setPlainText("計測中です。この出力は会話やキャラクターの学習に使いません。")

    def stop_benchmark(self):
        if self.cancellation:
            self.cancellation.set()

    def poll(self):
        if self.controller.session.agent_id != self.loaded_agent:
            self.load_controls()
        idle = not self.controller.runtime and not self.future and not self.controller.preparation
        for widget in (self.apply, self.language_test, self.asr_test):
            widget.setEnabled(idle)
        self.cancel.setEnabled(self.future is not None)
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                report = future.result()
                root = getattr(self.controller.services, "root", self.controller.session.store.path.parent)
                path = Path(root) / "data/esi03/performance" / ("performance-" + uid() + ".json")
                write_json(path, report)
                if report["kind"] == "language":
                    values = report["settings"]
                    output = f"最初の応答: 中央値 {report['median_first_token_seconds']:.2f}秒\n全体: 中央値 {report['median_total_seconds']:.2f}秒\n実行先: {report['backend'].get('device', 'unknown')}\n重み Q4_K_M / KV {values['cache_type']} / FlashAttention {values['flash_attention']}\n"
                    if report["hardware_after"]:
                        gpu = report["hardware_after"][0]
                        output += f"{gpu['name']} — 計測後のGPU使用メモリー {gpu['total_mb'] - gpu['free_mb']} MB（ピーク値ではありません）\n"
                else:
                    device = report["device"]
                    output = f"計算時間 {report['seconds']:.2f}秒 / 音声 {report['sample_seconds']:.2f}秒\n実行先 {device['device']} / 形式 {device['compute_type']}\n"
                self.result.setPlainText(output + "記録: " + str(path))
            except Exception as exc:
                self.result.setPlainText("計測を完了できませんでした: " + str(exc))

    def shutdown(self):
        self.timer.stop()
        self.stop_benchmark()
        self.pool.shutdown(wait=False, cancel_futures=True)
