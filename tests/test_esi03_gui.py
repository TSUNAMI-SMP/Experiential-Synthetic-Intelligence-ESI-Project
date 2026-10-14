import pytest

from esi03.config import Settings
from esi03.session import Session


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from esi02 import gui

    monkeypatch.setattr(gui, "audio_devices", lambda: [])
    return QApplication.instance() or QApplication([])


def dispose(window, app):
    from PySide6.QtCore import QEvent

    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def test_character_and_autonomy_settings_save_without_overwriting_live_edit(app, tmp_path):
    from esi03.gui import Window

    path = tmp_path / "character.db"
    window = Window(Session(path, Settings(playback=False)))
    try:
        panel = window.growth_panel
        panel.name.setText("リオン")
        panel.axes["traits", "curiosity"].setValue(95)
        panel.refresh()
        assert panel.name.text() == "リオン"
        panel.save_character()
        panel.proactive.setChecked(False)
        panel.budget.setValue(3)
        panel.save_preferences()
        assert window.session.experience.state.character.name == "リオン"
        assert window.session.experience.state.character.traits.curiosity == 0.95
        assert not window.session.experience.state.preferences.proactive
        assert window.release_name == "ESI-03"
        assert len(panel.needs) == 14
    finally:
        dispose(window, app)
    restored = Session(path)
    try:
        assert restored.experience.state.character.name == "リオン"
        assert restored.experience.state.preferences.daily_tool_budget == 3
    finally:
        restored.close()


def test_belief_view_shows_prior_wrong_information_and_correction(app, tmp_path):
    from esi03.gui import Window

    session = Session(tmp_path / "character.db", Settings(playback=False))
    session.create_turn("星砂村は月の国です。")
    session.create_turn("星砂村は月の国ではなく海の国です。")
    window = Window(session)
    try:
        panel = window.growth_panel
        panel.beliefs.selectRow(0)
        panel.show_belief()
        content = panel.belief_detail.toPlainText()
        assert "月の国" in content and "海の国" in content
        assert "訂正" in content
        assert "出典" in content
    finally:
        dispose(window, app)


def test_save_switch_restores_independent_character_and_controls(app, tmp_path):
    from esi03.gui import Window

    first = Session(tmp_path / "first.db", Settings(playback=False))
    second = Session(tmp_path / "second.db", Settings(playback=False))
    character = second.experience.state.character.model_dump()
    character["name"] = "別の子"
    second.character_operation("character", character)
    second.close()
    window = Window(first)
    try:
        window.switch_save(tmp_path / "second.db")
        assert window.session.experience.state.character.name == "別の子"
        assert window.growth_panel.name.text() == "別の子"
        assert window.session.store.path.name == "second.db"
    finally:
        dispose(window, app)


def test_exploration_scope_rejects_drive_root_and_preserves_saved_policy(app, tmp_path):
    from esi03.gui import Window

    window = Window(Session(tmp_path / "character.db", Settings(playback=False)))
    try:
        panel = window.growth_panel
        panel.roots.setPlainText("/")
        panel.save_policy()
        assert not window.session.research.policy.read_roots
        assert window.ui_error
        panel.roots.setPlainText(str(tmp_path))
        panel.save_policy()
        assert window.session.research.policy.read_roots == [str(tmp_path)]
    finally:
        dispose(window, app)


def test_gpu_settings_save_and_invalid_combination_is_rejected(app, tmp_path):
    from esi03.gui import Window

    path = tmp_path / "character.db"
    window = Window(Session(path, Settings(playback=False)))
    try:
        panel = window.performance_panel
        panel.flash.setCurrentIndex(panel.flash.findData("off"))
        panel.save_settings()
        assert window.session.settings.flash_attention == "auto"
        panel.cache.setCurrentIndex(panel.cache.findData("f16"))
        panel.save_settings()
        assert window.session.settings.flash_attention == "off"
        assert window.session.settings.cache_type == "f16"
    finally:
        dispose(window, app)
    restored = Session(path)
    try:
        assert restored.settings.cache_type == "f16"
    finally:
        restored.close()
