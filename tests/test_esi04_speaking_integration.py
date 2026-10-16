"""Actual Qwen acceptance for ordinary short speech, free decisions and longer work."""

import json
import os
import time
from contextlib import closing

import httpx
import pytest
from test_esi04_runtime import FileOnlyVoice, NoMemoryModel, wait_for

from esi02.dialogue import Cancellation
from esi04.config import Settings
from esi04.deliberation import context, deliberate
from esi04.dialogue import DialogueLLM
from esi04.runtime import Runtime
from esi04.session import Session
from esi04.speaking import recursive_question

pytestmark=[pytest.mark.integration,pytest.mark.skipif(
    os.environ.get('ESI_RUN_SPEAKING')!='1',reason='actual Qwen3-4B server required')]


@pytest.fixture
def logged(tmp_path):
    exchanges=[]
    def capture(response):
        if response.request.url.path=='/v1/chat/completions':
            response.read()
            exchanges.append({'request':json.loads(response.request.content),'response':response.json()})
            (tmp_path/'model-exchanges.json').write_text(json.dumps(exchanges,ensure_ascii=False,indent=2))
    def factory(**kwargs):
        return httpx.Client(**kwargs,event_hooks={'response':[capture]})
    with closing(Session(tmp_path/'save.db',Settings(playback=False,idle_deliberation=False,llm_timeout=300))) as s:
        s.character_operation('preferences',{'proactive':True,'exploration':False,'idle_activity':False})
        def talk(text):
            turn=s.create_turn(text)
            speech,progress=[],[]
            start=time.monotonic()
            ctx=s.dialogue_context(text)|{'_progress':progress.append}
            result=DialogueLLM(s.settings,factory).stream(text,s.history(turn['id']),Cancellation(),
                lambda _:None,speech.append,ctx)
            s.dialogue_result(turn['id'],result)
            s.update_turn(turn['id'],reply=result['text'],stage='complete')
            record={'user':text,'answer':result['text'],'effort':result.get('response_effort'),
                    'seconds':round(time.monotonic()-start,2),'progress':progress}
            print(json.dumps(record,ensure_ascii=False),flush=True)
            with (tmp_path/'conversations.jsonl').open('a') as out:
                out.write(json.dumps(record,ensure_ascii=False)+'\n')
            assert ''.join(speech)==result['text'] and not recursive_question(result['text'])
            assert not s.experience.pending_question() and not s.experience.state.dialogue_questions
            return result,progress
        yield s,talk,factory,exchanges


def test_real_short_and_mixed_conversation_without_question_protocol(logged):
    s,talk,_,exchanges=logged
    greeting,_=talk('おはよう')
    assert greeting['text']=='おはようございます。' and not exchanges
    s.utter('好きな色は何ですか？','color:1')
    result,progress=talk('青です。一言だけ返してください。')
    assert len(result['text'])<100 and not progress and '何かお手伝い' not in result['text']
    s.utter('この曲「夢」は、どんな雰囲気ですか？','song:1')
    result,_=talk('静かな曲です。タイトルは「夢」です。どう感じますか？')
    assert any(w in result['text'] for w in ['静か','夢','曲','穏やか'])
    assert result['text']!='静かな曲です。タイトルは「夢」です。どう感じますか？'
    result,_=talk('それについて教えてくれてありがとう。')
    assert 'について、もっと詳しく教えて' not in result['text']
    assert result['text']=='どういたしまして。' and result['inference_performed'] is False
    assert all('？' not in i.topic for i in s.experience.state.interests)


def test_real_deep_reply_has_actual_planning_and_audible_work_notice(logged):
    s,_,factory,exchanges=logged
    s.character_operation('preferences',{'proactive':False})
    class Voice(FileOnlyVoice):
        def __init__(self):
            self.texts=[]
        def synthesize(self,text,identifier):
            self.texts.append(text)
            return super().synthesize(text,identifier)
    voice=Voice()
    r=Runtime(s,llm=DialogueLLM(s.settings,factory),tts=voice,memory_llm=NoMemoryModel())
    try:
        start=time.monotonic()
        r.start()
        r.send('text','毎日20分勉強する案と、週末にまとめて140分勉強する案をじっくり比較してください。復習しやすさと続けやすさを考え、平日は忙しいという条件も含めて提案してください。')
        wait_for(lambda:bool(s.turns) and next(reversed(s.turns.values()))['stage'] in {'complete','failed'},timeout=440)
        turn=next(reversed(s.turns.values()))
        print(json.dumps({'deep':turn['reply'],'stage':turn['stage'],'error':turn.get('error'),
                          'seconds':round(time.monotonic()-start,2),'spoken':voice.texts},ensure_ascii=False),flush=True)
        assert turn['stage']=='complete',turn.get('error')
        assert turn['response_effort']=='deep' and len(turn['reply'])>60
        assert any(w in turn['reply'] for w in ['毎日','平日']) and '週末' in turn['reply']
        assert any('時間をかけて' in text for text in voice.texts)
        assert any(e['request']['response_format']['json_schema']['name']=='answer_outline' for e in exchanges)
        assert turn['processing_updates'][0]['stage']=='file_only'
        assert '少し時間をかけて考えます。' not in ''.join(m['content'] for m in s.history(''))
        assert not s.knowledge.entries()
    finally:
        r.stop()


def test_real_idle_model_can_share_a_comment_without_creating_another_question(logged,tmp_path):
    s,talk,_,_=logged
    talk('さっき話した本は、花と星の絵がたくさん載っている絵本です。今はお互いに自由に過ごしましょう。')
    key,value=context(s)
    # A new observation creates an opportunity, not a timer or a forced question.
    value['objects']=[]
    value['skills']=[]
    value['interests']=[]
    value['body']['energy']=.9
    value['body']['fatigue']=.1
    value['recent_dialogue'].append({'role':'assistant','content':'絵本の花と星の組み合わせが印象に残っています。'})
    plan=deliberate(s.settings,value,Cancellation())
    print(json.dumps({'idle':plan.model_dump()},ensure_ascii=False),flush=True)
    (tmp_path/'idle-plan.json').write_text(plan.model_dump_json(indent=2))
    assert plan.action in {'speak','review','none','rest'}
    if plan.action=='speak':
        assert plan.utterance and not recursive_question(plan.utterance)
        assert s.apply_focus(key,plan)
        assert not s.experience.pending_question() and not s.experience.state.dialogue_questions


def test_real_spontaneous_comment_after_an_observed_snack(tmp_path):
    with closing(Session(tmp_path/'spontaneous.db',Settings(playback=False,idle_deliberation=False,llm_timeout=300))) as s:
        s.character_operation('preferences',{'proactive':True,'exploration':False})
        s.brain.state.enabled=False
        event=s.scene_operation('place',{'kind':'snack','position':{'x':3.7,'y':0.,'z':2.5}},'ui:snack')
        s.scene_operation('eat',{'id':event['object_id']},'ate:snack')
        s.experience.state.needs.social=.9
        s.experience.state.needs.curiosity=.6
        key,value=context(s)
        plan=deliberate(s.settings,value,Cancellation())
        print(json.dumps({'observed_snack':plan.model_dump()},ensure_ascii=False),flush=True)
        (tmp_path/'snack-plan.json').write_text(plan.model_dump_json(indent=2))
        assert plan.action=='speak' and plan.utterance and '？' not in plan.utterance
        assert s.apply_focus(key,plan)
        assert s.cognition.utterances[-1].text==plan.utterance
        assert not s.turns and not s.experience.pending_question()
