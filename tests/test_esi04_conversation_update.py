"""The update disables external life without destroying its save or conversation."""

import json
from contextlib import closing

import pytest

from esi04.config import Settings
from esi04.plugins import MINECRAFT_PAUSED
from esi04.runtime import Runtime
from esi04.session import Session
from esi04.state import ExperienceState


def test_previous_save_reopens_in_polite_conversation_with_plugin_experience_retained(tmp_path):
    path = tmp_path/'old.db'
    with closing(Session(path, Settings(playback=False))) as s:
        s.create_turn('私の名前はタロウです。')
        s.create_turn('星砂村は月の国です。')
        old = s.dump()
        old['plugins'].update(active_id='minecraft.fabric', autonomous=True, goal='木を調べる', discoveries={'minecraft:stone':4})
        old['plugins'].pop('suspended_id')
        old['experience'].pop('conversation_revision')
        old['experience']['character']['style'] = 'casual'
        old['cognition'].pop('affect')
        original_agent = s.agent_id
    # Mimic an actual 0.4.6 save, rather than calling a now-disabled connection method.
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(old, ensure_ascii=False), 'esi04_live'))
    with closing(Session(path)) as s:
        assert s.agent_id == original_agent
        assert s.experience.state.character.style == 'precise'
        assert not s.plugins.active_id and not s.plugins.autonomous
        assert s.plugins.suspended_id == 'minecraft.fabric'
        assert s.plugins.goal == '木を調べる' and s.plugins.discoveries == {'minecraft:stone':4}
        assert len(s.view()['turns']) == 2 and s.knowledge.entries()[0]['value'] == '月の国'
        assert not s.environment_context()['active']
        with pytest.raises(ValueError, match='一時無効'):
            s.environment_mode('minecraft.fabric')
        runtime = Runtime(s)
        try:
            runtime._handle('plugin', ('connect', {'id':'minecraft.fabric','connection': {'endpoint':'http://127.0.0.1:8766','token':'a'*32}}))
            assert runtime.environment_client is None and not runtime.jobs
            assert s.status['plugin_error'] == MINECRAFT_PAUSED
            assert not s.plugins.active_id
        finally:
            runtime.stop()
    with closing(Session(path)) as s:
        assert not s.plugins.active_id and s.plugins.discoveries == {'minecraft:stone':4}


def test_gentle_style_is_preserved_and_new_default_is_polite():
    assert ExperienceState().character.style == 'precise'
    for style in ['gentle','precise']:
        old = ExperienceState().model_dump()
        old.pop('conversation_revision')
        old['character']['style'] = style
        assert ExperienceState.model_validate(old).character.style == style
    current = ExperienceState().model_dump()
    current['character']['style'] = 'casual'
    assert ExperienceState.model_validate(current).character.style == 'casual'  # Explicit choice after update.


def test_suspended_plugin_panel_and_style_controls_are_clear_and_persistent(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QPushButton

    from esi02 import gui
    from esi02.desktop_preferences import DesktopPreferences, write_preferences
    from esi04.gui import Window
    monkeypatch.setattr(gui,'audio_devices',lambda: [])
    app=QApplication.instance() or QApplication([])
    path=tmp_path/'save.db'
    write_preferences(tmp_path/'esi04-desktop.json',DesktopPreferences(model_path=None))
    window=Window(Session(path,Settings(playback=False)))
    try:
        assert '一時無効' in window.plugin_panel.status.text()
        assert not window.plugin_panel.findChildren(QPushButton)
        panel=window.growth_panel
        assert panel.style.currentData() == 'precise'
        panel.style.setCurrentIndex(panel.style.findData('gentle'))
        panel.refresh()
        assert panel.style.currentData() == 'gentle'  # Refresh cannot overwrite an in-progress edit.
        panel.save_character()
        assert window.session.experience.state.character.style == 'gentle'
        assert '不安' in panel.affect_status.text()
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    with closing(Session(path)) as s:
        assert s.experience.state.character.style == 'gentle'
