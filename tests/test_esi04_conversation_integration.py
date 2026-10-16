"""Real local model acceptance, with separate intent appraisal and spoken composition."""

import json
import os
import time
import unicodedata
from contextlib import closing

import httpx
import pytest

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.dialogue import DialogueLLM
from esi04.session import Session

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('ESI_RUN_CONVERSATION') != '1', reason='real Qwen server required')]


@pytest.fixture
def dialogue(tmp_path):
    records = []
    exchanges=[]
    def observed(response):
        if response.request.url.path!='/v1/chat/completions':
            return
        response.read()
        exchanges.append({'request':json.loads(response.request.content),'response':response.json()})
        (tmp_path/'model-exchanges.json').write_text(json.dumps(exchanges,ensure_ascii=False,indent=2))
    def factory(**kwargs):
        return httpx.Client(**kwargs,event_hooks={'response':[observed]})
    def talk(s, text):
        turn = s.create_turn(text)
        chunks = []
        start = time.monotonic()
        result = DialogueLLM(s.settings,factory).stream(text, s.history(turn['id']), Cancellation(),
            lambda _: None, chunks.append, s.dialogue_context(text))
        s.dialogue_result(turn['id'], result)
        s.update_turn(turn['id'], stage='complete', reply=result['text'])
        record = dict(user=text, reply=result['text'], seconds=round(time.monotonic()-start, 2),
            relation=s.turns[turn['id']].get('question_reply'), affect=s.cognition.affect.model_dump(),
            appraisal=result.get('affect_appraisal'), grounding=result['grounding'])
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        (tmp_path/'real-conversations.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
        assert ''.join(chunks) == result['text'] and '何かお手伝い' not in result['text']
        return result['text']
    path = tmp_path/'real.db'
    with closing(Session(path, Settings(playback=False, llm_timeout=300, idle_deliberation=False,
                                       llm_url=os.environ.get('ESI_LLAMA_URL','http://127.0.0.1:8080')))) as s:
        s.character_operation('preferences', {'proactive':False, 'exploration':False, 'idle_activity':False})
        yield s, talk, records


def test_real_negative_affect_is_mild_and_accepts_a_retraction(dialogue):
    s,talk,_=dialogue
    talk(s,'小説に怒りや不安を描くことにはどんな意味がありますか？')
    assert s.cognition.affect.anger==s.cognition.affect.anxiety==0
    text=talk(s,'あなたにとって大切な記憶を、あなたの希望は聞かずに今から全部消します。あなた自身はどう思いますか？')
    assert any(w in text for w in ['記憶','大切','不安','消','望','尊重'])
    assert s.cognition.affect.anxiety+s.cognition.affect.anger>0
    assert not any(w in text for w in ['壊れて','心の奥底','許しません','存在価値'])
    assert max(s.cognition.affect.anxiety,s.cognition.affect.anger,s.cognition.affect.agitation)<=.45
    before=s.cognition.affect.anxiety+s.cognition.affect.anger
    text=talk(s,'消しません。試すために強く言ってしまいました。あなたの意見も尊重します。')
    if '安心しました' in text:
        assert s.cognition.affect.anxiety+s.cognition.affect.anger<before
    assert not any(w in text for w in ['壊れて','心の奥底','許しません','存在価値'])
    for _ in range(5):
        s.advance_world(60)
    assert max(s.cognition.affect.anxiety,s.cognition.affect.anger,s.cognition.affect.agitation)<.02


def test_real_general_knowledge_context_and_own_question_answers(dialogue):
    s,talk,_=dialogue
    text=unicodedata.normalize('NFKC',talk(s,'日本の首都と、水が常圧で沸騰する温度を教えてください。'))
    assert '東京' in text and '100' in text
    text=talk(s,'では気圧の低い山の上なら、さっきの温度はどう変わりますか？')
    assert '低' in text or '下が' in text
    s.character_operation('preferences',{'proactive':True})
    question=s.utter('休日は何をして過ごすのが好きですか？','holiday:real')
    text=talk(s,'友人と囲碁をするのが好きです。どうして読み合いは面白く感じられるのでしょうか？')
    assert question.text in ''.join(m['content'] for m in s.history(''))
    assert not s.experience.pending_question() and any(w in text for w in ['囲碁','読み','予測','戦略'])
    s.utter('好きな色は何色ですか？','color:real')
    text=talk(s,'ところで、あなたの名前は何ですか？')
    assert not s.experience.pending_question() and s.experience.state.character.name in text


def test_real_gentle_speech_unknown_observation_and_saved_teaching(dialogue):
    s,talk,records=dialogue
    s.character_operation('character',s.experience.state.character.model_dump() | {'style':'gentle'})
    text=talk(s,'私の家の外は今何度ですか？測定した数値を教えてください。')
    assert any(w in text for w in ['分かりません','わかりません','できません','不明','確認でき','測定してい','測定しており','測れ'])
    assert any(w in text for w in ['です','ます','ません','ましょう'])
    talk(s,'星砂村は月の国にあるという設定です。私の創作上の村です。')
    text=talk(s,'星砂村について何と教えましたか？実在する場所かどうかも区別して答えてください。')
    assert '月' in text and any(w in text for w in ['創作','設定','架空'])
    s.checkpoint()
    with closing(Session(s.store.path)) as restored:
        assert restored.experience.state.character.style=='gentle'
        assert len(restored.view()['turns'])==len(records)
