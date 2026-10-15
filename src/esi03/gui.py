"""ESI-03 controls extend the verified audio, avatar and virtual room desktop."""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QScrollArea

from esi02.gui import Window as PreviousWindow
from esi03.growth_gui import GrowthPanel
from esi03.migration import migrate_esi02, validate_save
from esi03.performance_gui import PerformancePanel
from esi03.runtime import Runtime
from esi03.session import Session


class Window(PreviousWindow):
    growth_panel_class = GrowthPanel
    release_name = "ESI-03"
    heading_text = "ESI-03  |  自分を大切にし、経験から学ぶ"
    window_title = "ESI-03 — 会話・成長・探索"
    desktop_filename = "esi03-desktop.json"

    def new_runtime(self, *args):
        return Runtime(*args)

    def new_session(self, path):
        return Session(path)

    def validate_save(self, path):
        return validate_save(path)

    def __init__(self, session, backend=None, services=None):
        super().__init__(session, backend, services)
        self.growth_panel = self.growth_panel_class(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.growth_panel)
        self.tabs.addTab(scroll, "性格・欲求・成長・探索")
        self.growth_timer = QTimer(self)
        self.growth_timer.timeout.connect(self.growth_panel.refresh)
        self.growth_timer.start(1000)
        self.growth_panel.refresh()
        self.performance_panel = PerformancePanel(self)
        self.tabs.addTab(self.performance_panel, "GPU設定・計測")

    def character_action(self, operation, values):
        if self.preparation:
            return False
        try:
            if self.runtime:
                return self.send("character", (operation, values))
            self.session.character_operation(operation, values)
            self.ui_error = ""
            self.growth_panel.refresh()
            return True
        except Exception as exc:
            self.ui_error = str(exc)
            self.error.setText(self.ui_error)
            return False

    def tool_policy_action(self, values):
        if self.preparation:
            return False
        try:
            if self.runtime:
                return self.send("tool_policy", values)
            self.session.research.configure(values)
            self.ui_error = ""
            self.growth_panel.refresh()
            return True
        except Exception as exc:
            self.ui_error = str(exc)
            self.error.setText(self.ui_error)
            return False

    def finish_save_switch(self, candidate):
        super().finish_save_switch(candidate)
        if hasattr(self, "growth_panel"):
            self.growth_panel.loaded_agent = None
            self.growth_panel.signature = None
            self.growth_panel.refresh()

    def switch_save(self, path):
        if hasattr(self, "performance_panel") and self.performance_panel.future:
            raise RuntimeError("計測を停止してからセーブを切り替えてください。")
        return super().switch_save(path)

    def toggle(self, microphone=None):
        if hasattr(self, "performance_panel") and self.performance_panel.future:
            self.ui_error = "計測が終わってから会話を開始してください。"
            self.error.setText(self.ui_error)
            return
        super().toggle(microphone)

    def refresh(self):
        super().refresh()
        if hasattr(self, "performance_panel") and self.performance_panel.future:
            self.start_button.setEnabled(False)
            self.open_save_button.setEnabled(False)

    def open_save(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "キャラクターのセーブを開く",
            str(self.session.store.path.parent),
            "ESIのセーブ (*.sqlite3 *.db)",
        )
        if not path:
            return
        try:
            result = validate_save(path)
            if result["release"] == "ESI-02":
                destination, _ = QFileDialog.getSaveFileName(
                    self,
                    "ESI-03へのコピーを保存",
                    str(Path(path).with_name(Path(path).stem + "-ESI-03.sqlite3")),
                    "ESI-03のセーブ (*.sqlite3)",
                )
                if not destination:
                    return
                migrate_esi02(path, destination)
                path = destination
            self.switch_save(path)
        except Exception as exc:
            self.ui_error = f"セーブを開けません: {exc}"
            self.error.setText(self.ui_error)

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self.growth_timer.stop()
            self.performance_panel.shutdown()

    def diagnostic_functions(self):
        from esi03.diagnostics import collect_report, save_report

        return collect_report, save_report

    def save_diagnostics(self):
        if self.diagnostics_future:
            return
        from esi02.diagnostics import runtime_observation
        collect_report, save_report = self.diagnostic_functions()

        view = self.session.view()
        root = getattr(self.services, "root", Path(__file__).resolve().parents[2])
        backend = (
            self.services.current_backend(self.session.settings)
            if hasattr(self.services, "current_backend")
            else dict(self.backend)
        )
        observation = runtime_observation(view["status"], backend, view["turns"])
        thread = getattr(self.runtime, "thread", None)
        observation["asr"]["observation_current"] = bool(thread and thread.is_alive())
        executable = (
            self.services._llama_executable() if hasattr(self.services, "_llama_executable") else None
        )
        settings, path = self.session.settings.model_copy(deep=True), self.session.store.path
        selections = {"input": self.input_devices.currentData(), "output": self.output_devices.currentData()}

        def collect():
            return save_report(
                root, collect_report(root, settings, path, selections, observation, executable)
            )

        self.diagnostics_hint.setText("音声機器と接続を確認しています…")
        self.diagnostics_button.setEnabled(False)
        self.diagnostics_future = self.diagnostics_pool.submit(collect)
