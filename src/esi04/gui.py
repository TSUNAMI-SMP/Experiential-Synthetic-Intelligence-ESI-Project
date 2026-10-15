"""ESI-04 integrates the room, real screen/PC access and autonomous avatar with one owner."""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QLabel

from esi02.avatar_assets import BUNDLED_MODEL as PREVIOUS_MODEL
from esi03.gui import Window as PreviousWindow
from esi04.computer_gui import ComputerPanel, ComputerSupervisor
from esi04.desktop import BUNDLED_MODEL, DesktopViews
from esi04.growth_gui import GrowthPanel
from esi04.migration import migrate_esi03, validate_save
from esi04.runtime import Runtime
from esi04.session import Session
from esi04.world_gui import WorldPanel


class Window(PreviousWindow):
    release_name = "ESI-04"
    heading_text = "ESI-04  |  見て、試して、自分で活動を選ぶ"
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
        self.computer_supervisor, self.computer_panel = None, None
        super().__init__(*args, **kwargs)
        self.setup_computer()

    def setup_computer(self):
        if self.computer_supervisor:
            self.computer_supervisor.shutdown()
        if self.computer_panel:
            index = self.tabs.indexOf(self.computer_panel)
            self.tabs.removeTab(index)
            self.computer_panel.deleteLater()
        try:
            self.computer_supervisor = ComputerSupervisor(self.session.store.path.parent / "screen",
                                                          self.session.settings.screen_history_mb, self)
            self.computer_panel = ComputerPanel(self, self.computer_supervisor)
        except OSError as exc:
            self.computer_supervisor = None
            self.computer_panel = QLabel("この表示環境では画面・PC操作を開始できません："+str(exc))
            self.computer_panel.setWordWrap(True)
        self.tabs.addTab(self.computer_panel, "画面・PC操作・実演")

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
        try:
            if validate_save(path)["release"] == "ESI-03":
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
        if self.computer_panel and isinstance(self.computer_panel, ComputerPanel):
            status = self.session.status
            observation = status.get("screen_observation")
            if observation:
                result = observation["result"]
                self.computer_panel.observation.setPlainText(result["description"]+"\n"+"\n".join(result["visible_text"])
                    + ("\n画像の読み取りは未確認の推定を含みます。" if result["uncertain"] else ""))
            if status.get("computer_error"):
                self.computer_panel.status.setText(status["computer_error"])

    def closeEvent(self, event):
        if self.computer_supervisor:
            self.computer_supervisor.revoke()
        super().closeEvent(event)
        if event.isAccepted() and self.computer_supervisor:
            self.computer_supervisor.shutdown()
