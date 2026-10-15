"""Editable autonomy is not reset by refresh, and connection settings are scoped to each save."""

from types import SimpleNamespace

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget
from test_esi04_plugins import connected

from esi04.config import Settings
from esi04.plugin_gui import PluginPanel
from esi04.session import Session


def test_plugin_panel_preserves_pending_edits_and_reloads_for_a_different_character(tmp_path):
    app = QApplication.instance() or QApplication([])
    first = Session(tmp_path / "one.db", Settings(playback=False))
    second = Session(tmp_path / "two.db", Settings(playback=False))
    window = QWidget()
    calls = []
    window.session, window.runtime = first, SimpleNamespace()
    window.send = lambda *args: calls.append(args) or True
    panel = PluginPanel(window)
    try:
        connected(first)
        panel.refresh()
        assert "手に持っている物：なし" in panel.details.toPlainText()
        assert "minecraft:bread" in panel.details.toPlainText()
        assert "未実行の計画" in panel.learning_view.toPlainText()
        assert "minecraft:stone" in panel.places_view.toPlainText()
        panel.autonomous.setChecked(True)
        panel.goal.setText("近くの木を調べる")
        for _ in range(3):
            panel.refresh()
        first.plugin_options(goal="板材を作って確かめる")
        panel.refresh()  # A changed intention must appear even without a new sensor snapshot.
        assert "板材を作って確かめる" in panel.learning_view.toPlainText()
        assert panel.autonomous.isChecked() and panel.goal.text() == "近くの木を調べる"
        panel.options()
        assert calls[-1][1][1] == {"autonomous": True, "goal": "近くの木を調べる"}
        panel.token.setText("private-code-not-in-save")
        panel.remember.setChecked(True)
        panel.connect()
        assert panel.credentials_path.exists()
        assert "private-code" not in str(first.dump())
        window.session = second
        panel.refresh()
        assert not panel.token.text() and not panel.remember.isChecked() and not panel.autonomous.isChecked()
    finally:
        panel.close()
        panel.deleteLater()
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        first.close()
        second.close()
        app.processEvents()
