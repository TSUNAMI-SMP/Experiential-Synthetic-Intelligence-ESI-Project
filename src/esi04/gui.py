"""ESI-04 integrates the room, real screen/PC access and autonomous avatar with one owner."""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QLabel, QScrollArea

from esi02.avatar_assets import BUNDLED_MODEL as PREVIOUS_MODEL
from esi03.gui import Window as PreviousWindow
from esi04 import __version__
from esi04.computer_gui import ComputerPanel, ComputerSupervisor
from esi04.desktop import BUNDLED_MODEL, DesktopViews
from esi04.growth_gui import GrowthPanel
from esi04.history_gui import HistoryPanel, choose_recovery
from esi04.migration import migrate_esi03, validate_save
from esi04.plugin_gui import PluginPanel
from esi04.runtime import Runtime
from esi04.session import Session
from esi04.world_gui import WorldPanel


class Window(PreviousWindow):
    release_name = "ESI-04"
    heading_text = f"ESI-04 {__version__}  |  見て、試して、自分で活動を選ぶ"
    window_title = "ESI-04 — 会話・空間・画面・PC操作"
    desktop_filename = "esi04-desktop.json"
    growth_panel_class = GrowthPanel

    def new_runtime(self, *args):
        supervisor = getattr(self, "computer_supervisor", None)
        return Runtime(*args, computer=supervisor.hub if supervisor else None, services=self.services)

    def new_session(self, path):
        return Session(path)

    def validate_save(self, path):
        return validate_save(path)

    def new_desktop(self, path):
        return DesktopViews(self, path)

    def new_world_panel(self):
        return WorldPanel(self)

    def __init__(self, *args, **kwargs):
        self.history_panel = None
        self.plugin_panel = None
        self.computer_supervisor, self.computer_panel, self.computer_scroll = None, None, None
        super().__init__(*args, **kwargs)
        self.setup_computer()
        self.plugin_panel = PluginPanel(self)
        self.plugin_scroll = QScrollArea()
        self.plugin_scroll.setWidgetResizable(True)
        self.plugin_scroll.setWidget(self.plugin_panel)
        self.tabs.addTab(self.plugin_scroll, "プラグイン・Minecraft")
        self.history_panel = HistoryPanel(self)
        self.tabs.addTab(self.history_panel, "会話履歴・セーブ")
        self.tabs.currentChanged.connect(lambda index: self.history_panel.reload()
                                        if self.tabs.widget(index) is self.history_panel else None)

    def setup_computer(self):
        if isinstance(self.computer_panel, ComputerPanel):
            self.computer_panel.close_wide()
        if self.computer_supervisor:
            self.computer_supervisor.shutdown()
        if self.computer_panel:
            index = self.tabs.indexOf(self.computer_scroll)
            self.tabs.removeTab(index)
            self.computer_scroll.deleteLater()
        try:
            screen_root = self.session.store.path.with_name(self.session.store.path.stem+".screen")
            self.computer_supervisor = ComputerSupervisor(screen_root,
                                                          self.session.settings.screen_history_mb, self)
            self.computer_panel = ComputerPanel(self, self.computer_supervisor)
        except OSError as exc:
            self.computer_supervisor = None
            self.computer_panel = QLabel("この表示環境では画面・PC操作を開始できません："+str(exc))
            self.computer_panel.setWordWrap(True)
        self.computer_scroll = QScrollArea()
        self.computer_scroll.setWidgetResizable(True)
        self.computer_scroll.setWidget(self.computer_panel)
        self.tabs.addTab(self.computer_scroll, "画面・PC操作・実演")

    def scene_action(self, operation, values):
        try:
            if self.runtime:
                return self.send("scene", (operation, values))
            if operation == "approach":
                item = self.session.world.get(values["id"])
                self.session.world_operation("move", self.session.world.approach(item).model_dump())
            else:
                self.session.scene_operation(operation, values)
            self.ui_error = ""
            self.refresh()
            return True
        except Exception as exc:
            self.ui_error = str(exc)
            self.error.setText(self.ui_error)
            return False

    def toggle(self, *args, **kwargs):
        if self.runtime and self.computer_supervisor:
            self.computer_supervisor.revoke()
        return super().toggle(*args, **kwargs)

    def switch_save(self, path):
        if self.computer_supervisor:
            self.computer_supervisor.revoke()
        return super().switch_save(path)

    def finish_save_switch(self, candidate):
        super().finish_save_switch(candidate)
        if hasattr(self, "computer_panel"):
            self.setup_computer()
        if self.history_panel:
            self.history_panel.reload()

    def set_model(self, path):
        path = BUNDLED_MODEL if path in {BUNDLED_MODEL, PREVIOUS_MODEL} else path
        try:
            if not self.desktop.writable:
                raise ValueError(self.desktop.problem)
            self.desktop.avatar.set_model(path)
            self.desktop.preferences.model_path = BUNDLED_MODEL if path == BUNDLED_MODEL else (
                str(self.desktop.avatar.model_path) if self.desktop.avatar.model_path else None)
            self.desktop.flush()
            self.ui_error = ""
        except Exception as exc:
            self.ui_error = f"モデルを読み込めません：{exc}"
        self.refresh()

    def open_save(self):
        path, _ = QFileDialog.getOpenFileName(self, "キャラクターのセーブを開く",
                                             str(self.session.store.path.parent), "ESIのセーブ (*.sqlite3 *.db)")
        if not path:
            return
        self.open_selected_save(path)

    def recover_history(self):
        path = choose_recovery(self, getattr(self.services, "root", Path.cwd()), self.session.store.path)
        if path:
            self.open_selected_save(path)

    def open_selected_save(self, path):
        try:
            if validate_save(path)["release"] != "ESI-04":
                destination, _ = QFileDialog.getSaveFileName(self, "ESI-04へのコピーを保存",
                    str(Path(path).with_name(Path(path).stem+"-ESI-04.sqlite3")), "ESI-04のセーブ (*.sqlite3)")
                if not destination:
                    return
                migrate_esi03(path, destination)
                path = destination
            self.switch_save(path)
        except Exception as exc:
            self.ui_error = f"セーブを開けません：{exc}"
            self.error.setText(self.ui_error)

    def refresh(self):
        super().refresh()
        if self.plugin_panel:
            self.plugin_panel.refresh()
            active = bool(self.session.plugins.active_id)
            for index in range(self.tabs.count()):
                if self.tabs.tabText(index) in {"仮想の部屋・身体", "欲求・行動の学習"}:
                    self.tabs.setTabEnabled(index, not active)
            if active:
                self.body_action.setText("生活環境：Minecraft / 仮想の部屋・水分・温度・音の不快は停止")
        if self.computer_panel and isinstance(self.computer_panel, ComputerPanel):
            self.computer_panel.refresh()
            self.computer_panel.runtime_status(self.session.status)

    def closeEvent(self, event):
        if isinstance(self.computer_panel, ComputerPanel):
            self.computer_panel.close_wide()
        if self.computer_supervisor:
            self.computer_supervisor.revoke()
        super().closeEvent(event)
        if event.isAccepted() and self.computer_supervisor:
            self.computer_supervisor.shutdown()

    def diagnostic_functions(self):
        from esi04.diagnostics import collect_report, save_report

        backend = dict(getattr(self.services, "vision_backend", {}))
        def collect(*args, **kwargs):
            return collect_report(*args, **kwargs, vision_backend=backend)
        return collect, save_report
