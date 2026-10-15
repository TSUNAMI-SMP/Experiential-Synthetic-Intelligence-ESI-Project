"""The operation panel keeps editable state, can expand, and cancels pending starts on F8."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from esi04.computer_gui import ComputerSupervisor
from esi04.computer_models import Foreground
from esi04.computer_panel import ComputerPanel


class Native:
    def __init__(self):
        self.stop = False
        self.released = 0

    def foreground(self):
        return Foreground(handle=17, application="game")

    def elements(self, foreground):
        return []

    def emergency_pressed(self):
        return self.stop

    def release_inputs(self):
        self.released += 1


@pytest.fixture
def panel(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.resize(1200, 800)
    layout = QVBoxLayout(window)
    window.tabs = QTabWidget()
    layout.addWidget(window.tabs)
    window.runtime = None
    window.desktop = SimpleNamespace(avatar=QWidget(), preferences=SimpleNamespace(avatar_visible=False))
    window.send = lambda *args: True
    supervisor = ComputerSupervisor(tmp_path, native=Native())
    supervisor.timer.stop()
    widget = ComputerPanel(window, supervisor)
    window.computer_scroll = QScrollArea()
    window.computer_scroll.setWidgetResizable(True)
    window.computer_scroll.setWidget(widget)
    window.tabs.addTab(window.computer_scroll, "画面")
    window.show()
    app.processEvents()
    yield widget
    widget.close_wide()
    supervisor.shutdown()
    window.close()
    window.deleteLater()
    app.processEvents()


def test_full_size_operation_window_restores_the_same_controls(panel):
    original = panel.window.computer_scroll
    panel.rules.setPlainText("この目標へ進む")
    panel.open_wide()
    QApplication.processEvents()
    assert panel.expanded_dialog.isVisible() and original.widget() is None
    panel.close_wide()
    QApplication.processEvents()
    assert original.widget() is panel and panel.expanded_dialog is None
    assert panel.rules.toPlainText() == "この目標へ進む"


def test_f8_cancels_a_delayed_start_even_before_screen_access_is_granted(panel, monkeypatch):
    pending, calls = [], []
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: pending.append(callback))
    panel.later(lambda: calls.append("started"), "予約")
    panel.supervisor.hub.native.stop = True
    panel.supervisor.poll()
    pending[0]()
    assert not calls
    assert "F8" in panel.message.text()
    assert not panel.supervisor.hub.grant.control


def test_reading_an_error_does_not_erase_a_new_teaching_message(panel):
    panel.runtime_status({"computer_error": "画像AIの結果が途中です"})
    assert "途中" in panel.message.text()
    panel.message.setText("操作を保存しました")
    panel.runtime_status({"computer_error": "画像AIの結果が途中です"})
    assert panel.message.text() == "操作を保存しました"


def test_taught_chord_and_hold_are_saved_per_character(panel):
    panel.game_name.setText("長押しテスト")
    panel.bindings.item(0, 1).setText("W+SPACE")
    panel.bindings.item(0, 3).setText("650")
    assert panel.save_profile()
    saved = panel.supervisor.hub.memory.games()[0]
    assert saved.bindings[0].keys == ["W", "SPACE"]
    assert saved.bindings[0].milliseconds == 650
