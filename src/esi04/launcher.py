"""ESI-04 startup reuses model files and copies ESI-03 without modifying the original save."""

import os
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from esi02.launcher import read_preferences
from esi02.setup_assets import prepare_binaries
from esi03.launcher import Launcher as PreviousLauncher
from esi04.characters import Characters
from esi04.config import Settings
from esi04.gui import Window
from esi04.migration import migrate_esi03, validate_save
from esi04.services import Services
from esi04.session import Session, inspect_database
from esi04.setup_assets import prepare_assets


class Launcher(PreviousLauncher):
    data_directory = "esi04"
    config_filename = "esi04-windows.toml"
    window_title = "ESI-04を起動"
    heading_text = "ESI-04  |  会話・画面・空間の準備"

    def __init__(self, root, services_factory=Services, window_factory=Window):
        super().__init__(root, services_factory, window_factory)
        self.characters = Characters(self.data / "characters")
        self.refresh_characters()

    def read_config(self, path):
        return Settings.read(path)

    def new_session(self, path, settings):
        return Session(path, settings)

    def settings(self):
        if self.preferences_error:
            raise ValueError(self.preferences_error)
        if self.preferences.save_path and not self.save_path.is_file():
            raise FileNotFoundError("前回のセーブがありません。選び直してください。")
        return inspect_database(self.save_path) if self.save_path.exists() else self.fallback

    def migrate_if_needed(self, settings):
        if not self.save_path.exists() and not self.preferences.save_path:
            preferences = read_preferences(self.root / "data/esi03/launcher.json")
            source = Path(preferences.save_path or self.root / "data/esi03/session.sqlite3")
            if source.is_file():
                self.notify("元のESI-03を保持して、ESI-04へコピー移行しています…")
                migrate_esi03(source, self.save_path)
                return inspect_database(self.save_path)
        return settings

    def prepare_job(self, settings, prepare):
        settings = self.migrate_if_needed(settings)
        if self.offline_requested:
            return settings
        self.services.stop_owned()
        if prepare:
            prepare_assets(self.root, settings, self.notify, self.services.cancel)
        else:
            prepare_binaries(self.root, settings, self.notify, self.services.cancel)
        self.services.prepare(settings)
        return settings

    def select_character(self, index):
        path = Path(self.character_list.itemData(index))
        try:
            inspect_database(path)
            self.save_path, self.preferences.save_path = path, str(path)
            self.save_preferences()
            self.update_save_label()
        except Exception as exc:
            self.record_error(exc)

    def choose_save(self):
        path, _ = QFileDialog.getOpenFileName(self, "キャラクターのセーブを選ぶ", str(self.data),
                                             "セーブ (*.sqlite3 *.db)")
        if not path:
            return
        try:
            if validate_save(path)["release"] == "ESI-03":
                destination, _ = QFileDialog.getSaveFileName(self, "ESI-04へのコピーを保存",
                    str(Path(path).with_name(Path(path).stem+"-ESI-04.sqlite3")), "セーブ (*.sqlite3)")
                if not destination:
                    return
                migrate_esi03(path, destination)
                path = destination
            inspect_database(path)
            self.save_path, self.preferences.save_path = Path(path), str(path)
            self.save_preferences()
            self.refresh_characters()
            self.update_save_label()
            self.start()
        except Exception as exc:
            self.record_error(exc)

    def save_diagnostics(self):
        if self.diagnostics_future or self.closing:
            return
        from esi04.diagnostics import collect_report, save_report
        try:
            settings = self.settings()
        except Exception as exc:
            self.record_error(exc)
            return
        self.diagnostics_button.setEnabled(False)
        self.diagnostics_hint.setText("起動環境を確認しています…")
        self.diagnostics_future = self.diagnostics_pool.submit(
            lambda: save_report(self.root, collect_report(self.root, settings, self.save_path)))


def main(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    os.chdir(root)
    data = root / "data/esi04"
    data.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    lock = QLockFile(str(data / "launcher.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "ESI-04", "ESI-04は起動済みです。タスクバーの画面を開いてください。")
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
