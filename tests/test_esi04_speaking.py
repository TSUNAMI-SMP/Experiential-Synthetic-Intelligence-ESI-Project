"""Free dialogue, actual work speech, cancellation and spontaneous speech durability."""

import json
import threading
from contextlib import closing

import httpx
import pytest
from test_esi04_conversation import completion, model, reply
from test_esi04_runtime import FileOnlyVoice, NoMemoryModel, wait_for

from esi02.dialogue import Cancellation
from esi03.tools import Policy
from esi04.config import Settings
from esi04.deliberation import FocusPlan, context, deliberate
from esi04.dialogue import DialogueLLM
from esi04.runtime import Runtime
from esi04.save_history import utterance_page
from esi04.session import Session
from esi04.speaking import effort


def session_at(path, **settings):
    s = Session(path, Settings(playback=False, idle_deliberation=False, **settings))
    s.character_operation('preferences', {'exploration': False})
    return s


def test_reported_recursive_question_is_never_spoken_or_made_a_topic(tmp_path):
    bad = 'それでは、この曲「夢」についてもっと詳しく教えていただけますか？について、もっと詳しく教えてもらえますか？'
    with closing(session_at(tmp_path/'s.db')) as s:
        result, requests = reply(s, 'これは「夢」という曲です', completion(bad), completion('落ち着いた曲名ですね。'))
        assert len(requests)==2 and result['text']=='落ち着いた曲名ですね。'
        assert not s.experience.pending_question() and not s.experience.state.question_actions
        assert not s.utter(bad, 'bad:old-question')
        assert not s.experience.interest(bad, bad, 'old wrapper', 'teaching', 'old-turn')


@pytest.mark.parametrize('text', ['ありがとう', 'おはよう', 'なるほど', 'わかりました', 'それについて教えてくれてありがとう。'])
def test_obvious_social_reply_needs_no_model_or_processing_announcement(tmp_path, text):
    with closing(session_at(tmp_path/'s.db')) as s:
        def forbidden(**kwargs):
            pytest.fail('an unambiguous social signal must not start inference')
        updates=[]
        ctx=s.dialogue_context(text) | {'_progress': updates.append}
        result=DialogueLLM(s.settings, forbidden).stream(text, [], Cancellation(), lambda _:None, lambda _:None, ctx)
        assert len(result['text'])<30 and not result['inference_performed']
        assert not updates and '?' not in result['text'] and '？' not in result['text']


def test_confirmation_can_be_brief_but_a_factual_question_still_needs_an_answer(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        result, requests=reply(s, 'わかりましたか？', completion('はい。'))
        assert len(requests)==1 and result['text']=='はい。'
        result, requests=reply(s, '水はなぜ凍りますか？', completion('はい。'),
                              completion('冷えると水分子が氷の結晶を作るためです。'))
        assert len(requests)==2 and '結晶' in result['text']


def test_spontaneous_comment_has_normal_history_no_deadline_reward_or_automatic_repeat(tmp_path):
    path=tmp_path/'s.db'
    with closing(session_at(path)) as s:
        trials={k:v.trials for k,v in s.experience.state.habits.items()}
        u=s.utter('少し休んでから、本の続きを考えたいです。', 'room:book', ['book'])
        assert u and not s.experience.pending_question()
        assert not s.utter(u.text, 'room:other') and not s.utter('別の発言です。', 'room:book')
        turn=s.create_turn('どんな本ですか？')
        history=s.history(turn['id'])
        assert history==[{'role':'assistant','content':u.text}]
        s.advance_world(60, autonomy=False, allow_requests=True)
        assert {k:v.trials for k,v in s.experience.state.habits.items()}==trials
        assert not s.experience.state.dialogue_questions
        assert all(not c['kind'].startswith('ask_') for c in s.brain.candidates(s.world))
        s.utterance_delivery(u.id,'file_only')
    with closing(Session(path)) as restored:
        assert restored.cognition.utterances[-1].text==u.text
        assert restored.cognition.utterances[-1].stage=='file_only'
        assert restored.speech_archive()[0]['text']==u.text
        assert utterance_page(path)[0]['text']==u.text


def test_history_orders_repeated_words_and_multiple_spontaneous_entries_correctly(tmp_path):
    with closing(session_at(tmp_path/'s.db', history_pairs=2)) as s:
        for _ in range(4):
            turn=s.create_turn('同じ言葉です')
            s.update_turn(turn['id'], stage='complete', reply='そうなのですね。')
        u=s.utter('話題を変えてみたいです。', 'new:topic')
        t=s.create_turn('はい')
        h=s.history(t['id'])
        assert h[-1]['content']==u.text
        assert h[-2]['content']=='そうなのですね。'
        assert len(h)==4
        assert '処理中' not in json.dumps(h,ensure_ascii=False)


def test_old_pending_requests_are_archived_once_without_deleting_knowledge(tmp_path):
    path=tmp_path/'s.db'
    with closing(session_at(path)) as s:
        s.create_turn('星砂村は月の国です。')
        s.experience.free_speech=False
        q=s.experience.open_question('この曲について教えてもらえますか？','この曲について教えてもらえますか？','old')
        s.cognition.speech_revision=0
        s.checkpoint()
        agent=s.agent_id
    with closing(Session(path)) as restored:
        assert restored.agent_id==agent and restored.knowledge.entries()[0]['value']=='月の国'
        assert restored.experience.state.dialogue_questions[-1].id==q.id
        assert restored.experience.state.dialogue_questions[-1].stage=='cancelled'
        assert not restored.experience.pending_question()
        assert not restored.dialogue_context('続けましょう')['dialogue'].get('question_candidate')
        before=restored.dump()
        restored.migrate_speaking()
        assert restored.dump()==before


def test_idle_decision_can_choose_a_monologue_and_does_not_trigger_itself(tmp_path, monkeypatch):
    with closing(session_at(tmp_path/'s.db')) as s:
        key,data=context(s)
        plan=FocusPlan(action='speak',target_id='',reason='今の自分の関心を話す',alternatives=['review'],
                       emotion='interested',utterance='覚えたことを、自分でも比べてみたいです。')
        monkeypatch.setattr('esi04.deliberation.request',lambda *args,**kwargs:plan)
        assert deliberate(s.settings,data,Cancellation()).action=='speak'
        assert s.apply_focus(key,plan)
        assert s.cognition.utterances[-1].text==plan.utterance
        assert context(s)[0]==key  # Speech itself cannot form a periodic feedback loop.
        assert not s.apply_focus(key,plan)
        assert not s.experience.pending_question()
        s.character_operation('preferences',{'proactive':False})
        with pytest.raises(ValueError):
            deliberate(s.settings,context(s)[1],Cancellation())


def test_busy_person_and_muted_preferences_do_not_create_spontaneous_speech(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        s.experience.state.availability='busy'
        assert not s.utter('少し話したいです。','room:a')
        s.experience.state.availability='present'
        s.character_operation('preferences',{'proactive':False})
        assert not s.utter('少し話したいです。','room:b')
        s.character_operation('preferences',{'proactive':True})
        assert s.utter('少し話したいです。','room:c')


class HoldingDialogue:
    def __init__(self):
        self.started=threading.Event()
        self.release=threading.Event()
    def stream(self,text,history,cancellation,on_delta,on_sentence,context=None):
        context['_progress']('organizing')
        self.started.set()
        while not self.release.wait(.01):
            cancellation.check()
        cancellation.check()
        on_delta('整理した答えです。')
        on_sentence('整理した答えです。')
        return {'text':'整理した答えです。','finish_reason':'stop','grounding':{},'request':{}}


class Voice:
    def __init__(self, fail_notices=False):
        self.texts=[]
        self.fail_notices=fail_notices
    def synthesize(self,text,identifier):
        self.texts.append(text)
        if self.fail_notices and '整理しています' in text:
            raise ValueError('test synthesis failure')
        return FileOnlyVoice().synthesize(text,identifier)


@pytest.mark.parametrize('fail_notice',[False,True])
def test_real_owner_announces_actual_work_without_losing_reply_or_teaching_itself(tmp_path,fail_notice):
    s=session_at(tmp_path/'s.db')
    s.character_operation('preferences',{'proactive':False})
    dialogue,voice=HoldingDialogue(),Voice(fail_notice)
    runtime=Runtime(s,llm=dialogue,tts=voice,memory_llm=NoMemoryModel())
    try:
        runtime.start()
        runtime.send('text','二つの案を整理してください')
        wait_for(dialogue.started.is_set)
        wait_for(lambda:bool(s.turns[next(reversed(s.turns))].get('processing_updates')))
        for item in runtime.speech_queue:
            item['created']-=2
        wait_for(lambda:'情報を整理しています。' in voice.texts)
        wait_for(lambda:runtime.aux_speech is None)
        dialogue.release.set()
        wait_for(lambda:next(reversed(s.turns.values()))['stage']=='complete')
        turn=next(reversed(s.turns.values()))
        assert turn['reply']=='整理した答えです。' and turn['chunks']
        assert turn['processing_updates'][0]['stage']==('failed' if fail_notice else 'file_only')
        assert '整理しています' not in json.dumps(s.history(''),ensure_ascii=False)
        assert not s.knowledge.entries() and not s.status.get('error')
        if fail_notice:
            assert s.status.get('announcement_error')
    finally:
        dialogue.release.set()
        runtime.stop()
        s.close()


def test_new_input_cancels_an_announcement_and_no_voices_overlap(tmp_path):
    s=session_at(tmp_path/'s.db')
    dialogue, voice=HoldingDialogue(),Voice()
    playing=threading.Event()
    counter={'active':0,'max':0}
    def playback(path,stop,device,audio):
        counter['active']+=1
        counter['max']=max(counter['max'],counter['active'])
        playing.set()
        try:
            return not stop.wait(1)
        finally:
            counter['active']-=1
    s.settings.playback=True
    runtime=Runtime(s,llm=dialogue,tts=voice,playback=playback,memory_llm=NoMemoryModel())
    try:
        runtime.start()
        runtime.send('text','最初の説明をお願いします')
        wait_for(dialogue.started.is_set)
        wait_for(lambda:bool(runtime.speech_queue))
        for item in runtime.speech_queue:
            item['created']-=2
        wait_for(playing.is_set)
        runtime.send('text','別のことを話します')
        wait_for(lambda:len(s.turns)==2)
        first=list(s.turns.values())[0]
        assert first['stage']=='cancelled' and first['processing_updates'][0]['stage']=='interrupted'
        dialogue.release.set()
        wait_for(lambda:list(s.turns.values())[-1]['stage']=='complete')
        assert counter['max']==1 and not s.status.get('error')
    finally:
        dialogue.release.set()
        runtime.stop()
        s.close()


def test_expired_notice_and_muted_processing_never_reach_tts(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        voice=Voice()
        r=Runtime(s,tts=voice,memory_llm=NoMemoryModel())
        try:
            r._new_turn('整理してください')
            r._handle('work_progress',{'turn_id':r.current['turn_id'],'kind':'organizing'})
            r.current['generation_done']=True
            r._pump_speech()
            assert not r.speech_queue and not voice.texts
            s.character_operation('preferences',{'processing_voice':False})
            r.current['generation_done']=False
            r._work_notice('deliberating',r.current['turn_id'])
            assert not r.speech_queue
        finally:
            r.stop()


def test_search_announcement_requires_successful_authorized_tool_start(tmp_path,monkeypatch):
    with closing(session_at(tmp_path/'s.db')) as s:
        r=Runtime(s,tts=Voice(),memory_llm=NoMemoryModel())
        try:
            s.research.configure(Policy(web_enabled=False).model_dump())
            with pytest.raises(PermissionError):
                r._start_tool('web_search',{'query':'example'})
            assert not r.speech_queue
            def authorized_start(self,action,args,goal_id=None):
                self.tool_task={'id':'actual-job','cancellation':Cancellation()}
            monkeypatch.setattr('esi03.runtime.Runtime._start_tool',authorized_start)
            r._start_tool('web_search',{'query':'example'})
            assert r.speech_queue[0]['text']=='検索しています。'
            assert r._notice_live(r.speech_queue[0])
            r.tool_task=None
            assert not r._notice_live(r.speech_queue[0])
        finally:
            r.tool_task=None
            r.stop()


def test_deep_work_has_a_real_extra_stage_and_a_larger_bounded_answer_budget(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        factory, main_requests=model(completion('それぞれの案には利点があります。条件に合わせて選びます。',response_effort='deep'))
        requests=[]
        def extended(**kwargs):
            original=factory(**kwargs)
            transport=original._transport
            def respond(request):
                body=json.loads(request.content)
                if body.get('response_format',{}).get('json_schema',{}).get('name')=='answer_outline':
                    requests.append(body)
                    return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                        {'points':['利点を比較する'],'constraints':['予算を守る'],'unknowns':['実際の金額']},ensure_ascii=False)}}]})
                return transport.handle_request(request)
            return httpx.Client(transport=httpx.MockTransport(respond),**kwargs)
        progress=[]
        result=DialogueLLM(s.settings,extended).stream('二つの案をじっくり比較して理由を説明してください',[],Cancellation(),
            lambda _:None,lambda _:None,s.dialogue_context('比較')|{'_progress':progress.append})
        assert progress==['deliberating'] and len(requests)==1
        assert main_requests[0]['max_tokens']==s.settings.deep_reply_tokens>s.settings.reply_tokens
        assert result['response_effort']=='deep'
        assert '利点を比較する' in main_requests[0]['messages'][0]['content']
        assert '実際の金額' not in result['text'] and not s.knowledge.entries()
        assert effort('ひとことで比較して')=='brief'


def test_free_speech_ui_has_no_request_buttons_and_displays_monologues(tmp_path):
    from PySide6.QtWidgets import QApplication

    from esi04.gui import Window
    app=QApplication.instance() or QApplication([])
    s=session_at(tmp_path/'s.db')
    w=Window(s)
    try:
        s.utter('この本の続きが気になります。','book:1')
        w.show()
        w.refresh()
        app.processEvents()
        assert not w.request_card.isVisible() and not w.request_accept.isVisible()
        assert 'この本の続きが気になります。' in w.conversation.toPlainText()
        assert '独り言' in w.growth_panel.proactive.text()
        assert w.growth_panel.processing_voice.isChecked()
        w.history_panel.reload()
        assert 'この本の続きが気になります。' in w.history_panel.text.toPlainText()
    finally:
        w.close()


def test_idle_owner_speaks_without_a_user_turn_or_request_action(tmp_path,monkeypatch):
    s=session_at(tmp_path/'s.db')
    s.settings.idle_deliberation=True
    s.brain.state.enabled=False  # Body activity and speech have separate controls.
    voice=Voice()
    def choose(settings,value,cancellation):
        return FocusPlan(action='speak',target_id='',reason='気になったことを一度話す',alternatives=['none'],
                         emotion='interested',utterance='花と星の組み合わせが気になります。')
    monkeypatch.setattr('esi04.runtime.deliberate',choose)
    r=Runtime(s,tts=voice,memory_llm=NoMemoryModel())
    try:
        r.start()
        wait_for(lambda:bool(s.cognition.utterances) and s.cognition.utterances[-1].stage=='file_only')
        assert not s.turns and not s.experience.pending_question() and not s.brain.active
        assert voice.texts==['花と星の組み合わせが気になります。']
        assert not s.knowledge.entries()
    finally:
        r.stop()
        s.close()


def test_model_cannot_parrot_the_users_whole_question_instead_of_answering(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        text='静かな曲です。タイトルは「夢」です。どう感じますか？'
        result,requests=reply(s,text,completion(text,followup='どう感じますか？'),
            completion('実際の音はまだ聴いていませんが、静かで夢のような雰囲気を想像します。'))
        assert len(requests)==2 and result['text']!=text and '聴いていません' in result['text']


def test_a_truncated_idle_decision_never_becomes_speech(tmp_path,monkeypatch):
    from esi04.structured import request
    with closing(session_at(tmp_path/'s.db')) as s:
        def respond(req):
            if req.url.path=='/apply-template':
                return httpx.Response(200,json={'prompt':'test'})
            if req.url.path=='/tokenize':
                return httpx.Response(200,json={'tokens':[1]})
            complete_json=FocusPlan(action='speak',target_id='',reason='test',alternatives=[],
                                   emotion='neutral',utterance='この文は発声しません。').model_dump_json()
            return httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':complete_json}}]})
        original=httpx.Client
        monkeypatch.setattr('esi04.structured.httpx.Client',lambda **kwargs:original(
            transport=httpx.MockTransport(respond),**kwargs))
        before=s.dump()
        with pytest.raises(ValueError,match='途中で途切れ'):
            request(s.settings,'test',{},FocusPlan,Cancellation())
        assert s.dump()==before and not s.cognition.utterances


def test_feelings_about_a_fictional_character_do_not_become_esis_distress(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        result,_=reply(s,'小説で怒りを描く意味を教えてください',completion(
            '登場人物の葛藤を伝えるためです。',appraisal_target='general',emotion='angry',emotion_reason='登場人物の怒り'))
        assert result['affect_appraisal']['emotion']=='neutral' and not s.cognition.affect.anger


def test_a_simple_personal_reaction_does_not_expand_into_philosophical_deliberation(tmp_path):
    with closing(session_at(tmp_path/'s.db')) as s:
        result,requests=reply(s,'あなたの希望は聞かず記憶を消します。どう思いますか？',completion(
            '少し不安です。消す前に相談していただきたいです。',emotion='anxious',response_effort='deep'))
        assert len(requests)==1 and result['response_effort']=='standard'
        assert not result['request']['outline_request'] and s.cognition.affect.anxiety==.22
