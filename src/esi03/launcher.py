"""Shared engines, independent characters and consistent ESI-02 copy migration."""

import os
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPushButton,
)

from esi02.launcher import Launcher as PreviousLauncher
from esi02.launcher import read_preferences
from esi03.characters import Characters
from esi03.config import Settings
from esi03.gui import Window
from esi03.migration import migrate_esi02, validate_save
from esi03.services import Services
from esi03.session import Session, inspect_database


class Launcher(PreviousLauncher):
    data_directory = "esi03"
    config_filename = "esi03-windows.toml"
    window_title = "ESI-03を起動"
    heading_text = "ESI-03  |  会話・成長・探索の準備"

    def read_config(self, path):
        return Settings.read(path)

    def new_session(self, path, settings):
        return Session(path, settings)

    def __init__(self, root, services_factory=Services, window_factory=Window):
        super().__init__(root, services_factory, window_factory)
        self.offline_requested = False
        self.characters = Characters(self.data / "characters")
        row = QHBoxLayout()
        self.character_list = QComboBox()
        self.character_list.setMinimumWidth(200)
        self.character_list.activated.connect(self.select_character)
        self.create_character = QPushButton("別のキャラクターを作る")
        self.create_character.clicked.connect(self.new_character)
        row.addWidget(self.character_list, 1)
        row.addWidget(self.create_character)
        self.layout().insertLayout(1, row)
        self.refresh_characters()

    def enabled(self, idle):
        super().enabled(idle)
        if hasattr(self, "create_character"):
            self.create_character.setEnabled(idle)
            self.character_list.setEnabled(idle)

    def settings(self):
        if self.preferences_error:
            raise ValueError(self.preferences_error)
        if self.preferences.save_path and not self.save_path.is_file():
            raise FileNotFoundError("前回のキャラクターのセーブがありません。セーブを選び直してください。")
        return inspect_database(self.save_path) if self.save_path.exists() else self.fallback

    def migrate_if_needed(self, settings):
        if not self.save_path.exists() and not self.preferences.save_path:
            old_preferences = read_preferences(self.root / "data/esi02/launcher.json")
            source = Path(old_preferences.save_path or self.root / "data/esi02/session.sqlite3")
            if source.is_file():
                self.notify("ESI-02のセーブを、元データを残してESI-03へコピー移行しています…")
                migrate_esi02(source, self.save_path)
                return inspect_database(self.save_path)
        return settings

    def prepare_job(self, settings, prepare):
        settings = self.migrate_if_needed(settings)
        return settings if self.offline_requested else super().prepare_job(settings, prepare)

    def start(self, prepare=False):
        self.offline_requested = False
        super().start(prepare)

    def open_offline(self):
        if self.future or self.closing or self.window:
            return
        try:
            self.offline_requested = True
            self.enabled(False)
            self.status.setText("セーブ・記憶の画面を準備しています…")
            self.future = self.pool.submit(self.prepare_job, self.settings(), False)
        except Exception as exc:
            self.record_error(exc)

    def open_main(self, settings, offline=False):
        return super().open_main(settings, offline or self.offline_requested)

    def refresh_characters(self):
        self.character_list.clear()
        self.character_list.addItem("現在のセーブ", str(self.save_path))
        for entry in self.characters.entries():
            if Path(entry["path"]) != self.save_path:
                self.character_list.addItem(entry["name"], entry["path"])

    def select_character(self, index):
        path = Path(self.character_list.itemData(index))
        try:
            inspect_database(path)
            self.save_path = path
            self.preferences.save_path = str(path)
            self.save_preferences()
            self.update_save_label()
        except Exception as exc:
            self.record_error(exc)

    def new_character(self):
        name, ok = QInputDialog.getText(self, "キャラクターを追加", "名前")
        if not ok or not name.strip():
            return
        labels = {
            "好奇心と自己尊重を大切にする": "curious",
            "考えをしっかり持つ": "steadfast",
            "柔軟に考えを更新する": "flexible",
        }
        selected, ok = QInputDialog.getItem(
            self, "性格の初期傾向", "初期傾向（あとで調整できます）", list(labels), editable=False
        )
        if not ok:
            return
        try:
            settings = self.settings()
            self.save_path = self.characters.create(name.strip(), labels[selected], settings)
            self.preferences.save_path = str(self.save_path)
            self.save_preferences()
            self.refresh_characters()
            self.update_save_label()
        except Exception as exc:
            self.record_error(exc)

    def choose_save(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "キャラクターのセーブを選ぶ", str(self.data), "セーブ (*.sqlite3 *.db)"
        )
        if not path:
            return
        try:
            if validate_save(path)["release"] == "ESI-02":
                destination, _ = QFileDialog.getSaveFileName(
                    self,
                    "ESI-03へのコピーを保存",
                    str(Path(path).with_name(Path(path).stem + "-ESI-03.sqlite3")),
                    "セーブ (*.sqlite3)",
                )
                if not destination:
                    return
                migrate_esi02(path, destination)
                path = destination
            inspect_database(path)
            self.save_path = Path(path)
            self.preferences.save_path = str(self.save_path)
            self.save_preferences()
            self.refresh_characters()
            self.update_save_label()
            self.start()
        except Exception as exc:
            self.record_error(exc)

    def save_diagnostics(self):
        if self.diagnostics_future or self.closing:
            return
        from esi03.diagnostics import collect_report, save_report

        try:
            settings = self.settings()
        except Exception as exc:
            self.record_error(exc)
            return
        root, path = self.root, self.save_path
        self.diagnostics_hint.setText("音声機器と接続を確認しています…")
        self.diagnostics_button.setEnabled(False)
        self.diagnostics_future = self.diagnostics_pool.submit(
            lambda: save_report(root, collect_report(root, settings, path))
        )


def main(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    os.chdir(root)
    data = root / "data/esi03"
    data.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    lock = QLockFile(str(data / "launcher.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "ESI-03", "ESI-03は起動済みです。タスクバーの画面を開いてください。")
        return 1
    launcher = None
    try:
        launcher = Launcher(root)
        launcher.show()
        QTimer.singleShot(0, launcher.start)
        return app.exec()
    finally:
        if launcher:
            launcher.shutdown()
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
