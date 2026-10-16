"""Actual Rinne voice files for public work speech and unsolicited ordinary speech."""

import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from test_esi04_runtime import NoMemoryModel, wait_for
from test_esi04_speaking import HoldingDialogue

from esi04.config import Settings
from esi04.runtime import Runtime
from esi04.session import Session

pytestmark=[pytest.mark.integration,pytest.mark.skipif(
    os.environ.get('ESI_RUN_SPEAKING_AUDIO')!='1',reason='actual AivisSpeech with Rinne voice required')]


def test_real_tts_for_work_final_reply_and_free_comment(tmp_path):
    settings=Settings(playback=False,idle_deliberation=False)
    s=Session(tmp_path/'voice.db',settings)
    s.character_operation('preferences',{'exploration':False})
    dialogue=HoldingDialogue()
    r=Runtime(s,llm=dialogue,memory_llm=NoMemoryModel())
    try:
        r.start()
        r.send('text','二つの案を整理してください')
        wait_for(dialogue.started.is_set)
        wait_for(lambda:bool(r.speech_queue))
        for item in r.speech_queue:
            item['created']-=2
        wait_for(lambda:next(reversed(s.turns.values())).get('processing_updates',[{}])[0].get('stage') in {'file_only','failed'},timeout=90)
        dialogue.release.set()
        wait_for(lambda:next(reversed(s.turns.values()))['stage'] in {'complete','failed'},timeout=90)
        turn=next(reversed(s.turns.values()))
        assert turn['stage']=='complete',turn.get('error')
        notice=turn['processing_updates'][0]
        assert notice['stage']=='file_only',notice
        speech=s.utter('星と花の絵を、もう少し見てみたいです。','art:1')
        wait_for(lambda:speech.stage in {'file_only','failed'},timeout=90)
        assert speech.stage=='file_only'
        audio=[notice['audio']['audio'],turn['chunks'][0]['audio'],speech.audio['audio']]
        stats=[]
        for entry in audio:
            samples,rate=sf.read(entry['path'],dtype='float32')
            assert entry['model_name']=='凛音エル' and Path(entry['path']).is_file()
            assert np.isfinite(samples).all() and float(np.max(np.abs(samples)))>.01
            stop=threading.Event()
            generation=r.playback_meter.begin(stop)
            block=samples[np.argmax(np.abs(samples)):][:int(rate*.04)]
            now=time.monotonic()
            r.playback_meter.publish(generation,now,.04,block)
            assert r.playback_meter.level(now)>0
            stop.set()
            assert r.playback_meter.level(now)==0
            r.playback_meter.clear()
            stats.append({'model_name':entry['model_name'],'rate':rate,'duration':len(samples)/rate,'path':entry['path']})
        (tmp_path/'actual-voice.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2))
        assert not s.experience.pending_question() and not s.knowledge.entries()
        assert not s.status.get('error')
    finally:
        dialogue.release.set()
        r.stop()
        s.close()
