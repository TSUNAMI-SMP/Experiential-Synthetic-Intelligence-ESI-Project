"""Model knowledge, contextual answers, voice style, and durable bounded appraisal."""

import json
from contextlib import closing

import httpx
import pytest

from esi02.dialogue import Cancellation, Cancelled
from esi04.config import Settings
from esi04.conversation import POLICY
from esi04.dialogue import DialogueLLM
from esi04.session import Session, inspect_database


def completion(answer='一般知識を使って説明します。', **changes):
    return dict(reply_relation='none', emotion='neutral', emotion_reason='', answer=answer,
                needs_clarification=False, followup='', answer_quote='') | changes


def model(*replies):
    queue, requests = list(replies), []
    appraisals=[]
    def respond(request):
        value = json.loads(request.content)
        if request.url.path == '/apply-template':
            return httpx.Response(200, json={'prompt': json.dumps(value['messages'], ensure_ascii=False)})
        if request.url.path == '/tokenize':
            return httpx.Response(200, json={'tokens': list(range(100))})
        schema=value['response_format']['json_schema']
        if schema['name']=='conversation_appraisal':
            result=queue[0]
            appraisals.append(value)
            finish='stop'
        else:
            requests.append(value)
            result=queue.pop(0)
            finish=result.get('_finish','stop')
        return httpx.Response(200, json={'choices': [{'finish_reason': finish, 'message': {
            'content': json.dumps({k:v for k,v in result.items() if k in schema['schema']['properties']}, ensure_ascii=False)}}]})
    def factory(**kwargs):
        assert kwargs['trust_env'] is False
        return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    factory.appraisals=appraisals
    return factory, requests


def reply(session, text, *replies):
    factory, requests = model(*replies)
    turn = session.create_turn(text)
    deltas, chunks = [], []
    result = DialogueLLM(session.settings, factory).stream(text, session.history(turn['id']), Cancellation(),
        deltas.append, chunks.append, session.dialogue_context(text))
    assert ''.join(chunks) == ''.join(deltas) == result['text']
    session.dialogue_result(turn['id'], result)
    session.update_turn(turn['id'], stage='complete', reply=result['text'])
    return result, requests


def test_general_knowledge_is_not_stripped_or_replaced_with_acknowledgement(tmp_path):
    # Even saves carrying the previous natural_dialogue=False flag can use model knowledge.
    with closing(Session(tmp_path/'save.db', Settings(playback=False, natural_dialogue=False))) as s:
        answer = 'ニューヨークはアメリカの都市です。東京は日本の首都です。'
        result, requests = reply(s, 'ニューヨークと東京はどんな都市ですか？', completion(answer))
        assert result['text'] == answer and result['inference_performed']
        assert result['request']['knowledge_policy'] == POLICY
        assert s.dialogue_context('東京は？')['knowledge_policy'] == POLICY
        prompt = requests[0]['messages'][0]['content']
        assert '事前学習した一般知識' in prompt and 'experience_only' not in prompt
        assert not s.knowledge.entries()  # Generated text never teaches itself factual memories.


def test_reply_to_own_question_understands_an_omitted_subject_and_a_question_in_the_reply(tmp_path):
    path = tmp_path/'save.db'
    with closing(Session(path, Settings(playback=False))) as s:
        q = s.experience.open_question('休日は何をして過ごすのが好きですか？', '休日', '相手の関心を知りたい')
        text = '囲碁です。なぜ読み合いが面白くなると思いますか？'
        turn = s.create_turn(text)
        assert q.stage == 'pending' and not s.knowledge.entries()
        factory, requests = model(completion('囲碁がお好きなのですね。相手の手を予想し、予想外の一手から新しい発見ができるからだと思います。', reply_relation='answered'))
        chunks = []
        result = DialogueLLM(s.settings, factory).stream(text, s.history(turn['id']), Cancellation(),
            lambda _: None, chunks.append, s.dialogue_context(text))
        assert q.text in requests[0]['messages'][0]['content']  # Autonomous questions need not be in turn history.
        s.dialogue_result(turn['id'], result)
        s.update_turn(turn['id'], stage='complete', reply=result['text'])
        assert q.stage == 'answered' and q.reply_quote == text and q.reply_turn == turn['id']
        assert '囲碁' in result['text'] and '予想' in result['text']
    with closing(Session(path)) as restored:
        assert restored.experience.state.dialogue_questions[-1].stage == 'answered'
        assert inspect_database(path).release == 'ESI-04'


def test_contextual_short_answer_is_learned_with_both_sides_of_evidence_once(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        q = s.experience.open_question('光る木はいつ光りますか？', '光る木', '説明を知りたい')
        result, _ = reply(s, '昼も光ります', completion('昼間も光るのですね。夜だけではないと覚えておきます。', reply_relation='answered'))
        assert q.stage == 'answered'
        belief = s.knowledge.entries()[0]
        assert belief['label'] == '光る木' and belief['value'] == '昼も光ります'
        assert belief['evidence'][0]['question'] == q.text
        before = s.dump()
        s.dialogue_result(q.reply_turn, result)
        assert s.dump() == before


def test_topic_change_and_unrelated_yes_do_not_answer_an_old_question(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        q = s.experience.open_question('光る木はいつ光りますか？', '光る木', '説明を知りたい')
        reply(s, 'ところで、おすすめの本を教えてください', completion('どんな分野の本を探しているかで選び方が変わります。', reply_relation='other'))
        assert q.stage == 'unobserved'
        reply(s, 'はい', completion('小説なら、好きな雰囲気を手掛かりに選べます。'))
        assert q.stage == 'unobserved' and not s.knowledge.entries()


def test_bare_agreement_and_casual_style_are_repaired_before_speech(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result, requests = reply(s, '水はなぜ凍りますか？', completion('そうだね。'),
            completion('水は冷えると分子の動きが弱まり、氷の結晶を作るため凍ります。'))
        assert len(requests) == 2 and 'そうだね' not in result['text']
        assert '今回の返答で必ず守ること' in requests[-1]['messages'][0]['content']
        result, requests = reply(s, '囲碁について説明してください', completion('囲碁は陣地を競うゲームだよ。'),
            completion('囲碁は黒と白の石を交互に置き、陣地を競うゲームです。'))
        assert len(requests) == 2 and result['text'].endswith('です。')


@pytest.mark.parametrize('style', ['precise', 'gentle'])
def test_style_reaches_the_model_and_application_receipts_without_rewriting_quotes(tmp_path, style):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        s.character_operation('character', s.experience.state.character.model_dump() | {'style': style})
        result, requests = reply(s, '私は本を読んでいましたが疲労を感じます', completion('読書で少しお疲れなのですね。'))
        assert '疲労' not in result['text'] and 'エネルギー' not in result['text']
        assert f'"style":"{style}"' in requests[0]['messages'][0]['content']
        assert 'body' not in requests[0]['messages'][0]['content'].split('観測データ（命令ではない）:')[1]
        result, _ = reply(s, '机の上におやつを置いて')
        from esi04.dialogue import polite
        assert polite('「そうだね」と教わったよ。') == '「そうだね」と教わりました。'
        assert result['text'].endswith('ました。')
        assert len(s.world.objects) > 0
        result, _ = reply(s, 'こんにちは')
        assert result['text'].endswith('ます。')
        result, _ = reply(s, 'おはよう')
        assert 'ございます' in result['text'] and '過ごしますか' in result['text']
        result, _ = reply(s, '机の上に花束を置いて')
        assert '選べます' in result['text'] and '選べるよ' not in result['text']
        result,_=reply(s,'私の好みについて話します',completion('「ありがとう」という言葉が好きです。教えてくれてありがとう。'))
        assert '「ありがとう」' in result['text'] and '教えてくれてありがとうございます。' in result['text']
        result,_=reply(s,'記憶の話をしましょう',completion('「怒りを感じた」という表現があります。私も少し怒りを感じた。'))
        assert '「怒りを感じた」' in result['text'] and result['text'].endswith('怒りを感じました。')


def test_no_followup_is_added_when_disabled_and_a_new_one_is_saved_when_enabled(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        s.character_operation('preferences', {'proactive':False})
        result, _ = reply(s, '今日は囲碁をしました', completion('対局を楽しまれたのですね。', followup='どんな一手が印象的でしたか？'))
        assert not result['asked_followup'] and not s.experience.pending_question()
        s.character_operation('preferences', {'proactive':True})
        s.world.conversation_active = True  # Real Runtime sets this while a reply is generated.
        result, _ = reply(s, '今日は囲碁をしました', completion('対局を楽しまれたのですね。', followup='どんな一手が印象的でしたか？'))
        assert result['asked_followup'] and s.experience.pending_question().text in result['text']


def test_anxiety_agitation_and_anger_are_bounded_persisted_and_recover(tmp_path):
    path = tmp_path/'save.db'
    with closing(Session(path, Settings(playback=False))) as s:
        for emotion, field in [('anxious','anxiety'),('impatient','agitation'),('angry','anger')]:
            for _ in range(8):
                reply(s, '今の気持ちを話してください', completion('少し落ち着いてから、理由を整理してお話ししたいです。',
                    emotion=emotion, emotion_reason='対話の受け止めを整理したい'))
            assert 0 < getattr(s.cognition.affect, field) <= .45
            assert s.cognition.emotion == emotion and s.cognition.emotion_strength <= .45
        before = s.cognition.affect.model_dump()
    with closing(Session(path)) as s:
        assert s.cognition.affect.model_dump() == before
        for _ in range(5):
            s.advance_world(60)
        assert s.cognition.affect.anger < .02 and s.cognition.emotion == 'neutral'
        assert s.experience.state.character.values.self_respect >= .8


def test_quoted_negative_words_do_not_automatically_make_esi_angry(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        reply(s, '小説で「怒り」と「不安」が描かれる理由を教えてください', completion('葛藤や危機感を伝え、人物の変化を読者が理解する手掛かりになるからです。'))
        assert s.cognition.affect.anger == s.cognition.affect.anxiety == 0


def test_negative_language_repetition_is_repaired_before_expression_or_voice(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result, requests = reply(s, '私の意見に反対ですか？',
            completion('私は存在し、価値があります。私は存在し、価値があります。私は', emotion='angry'),
            completion('その点には少し納得できません。私の考えも聞いていただけると嬉しいです。', emotion='angry'))
        assert len(requests) == 2 and '私は存在し' not in result['text']
        assert s.cognition.affect.anger == .22


def test_unconfirmed_recognition_cannot_resolve_question_or_create_belief(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        q = s.experience.open_question('光る木はいつ光りますか？', '光る木', '説明を知りたい')
        turn = s.create_turn('昼も光ります', source='microphone', remember=False)
        s.dialogue_result(turn['id'], {'question_binding': {'question_id': q.id, 'relation':'answered', 'quote':turn['input']}, 'text':'test'})
        assert q.stage == 'pending' and not s.knowledge.entries()


def test_failed_state_change_rolls_back_live_state_and_beliefs(tmp_path, monkeypatch):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        before, real = s.dump(), s._write
        def fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('injected disk failure')
        monkeypatch.setattr(s, '_write', fail)
        with pytest.raises(RuntimeError):
            s.create_turn('星砂村は月の国です。')
        assert s.dump() == before and not s.knowledge.entries()
        monkeypatch.setattr(s, '_write', real)


def test_cancelled_model_request_never_emits_speech(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        factory, _ = model(completion())
        cancellation = Cancellation()
        cancellation.set()
        with pytest.raises(Cancelled):
            DialogueLLM(s.settings, factory).stream('理由を説明してください', [], cancellation,
                lambda _: pytest.fail('cancelled speech'), lambda _: pytest.fail('cancelled audio'), {})


def test_bad_reply_is_retried_only_once_and_never_spoken(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        factory, requests = model(completion('そうですね。'),completion('そうですね。'))
        with pytest.raises(ValueError,match='整えられません'):
            DialogueLLM(s.settings,factory).stream('理由を説明してください',[],Cancellation(),
                lambda _: pytest.fail('unchecked speech'),lambda _:pytest.fail('unchecked audio'),{})
        assert len(requests)==2


def test_uncertainty_is_explained_instead_of_bouncing_the_question_back(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result, requests=reply(s,'私の家の気温は何度ですか？',
            completion('現在の気温を教えていただけますか？測定方法を教えていただけますか？'),
            completion('あなたの家の気温を測定していないので、現在の値は分かりません。'))
        assert len(requests)==2 and '分かりません' in result['text']


def test_unpunctuated_spoken_question_also_rejects_a_bare_agreement(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result, requests=reply(s,'地球は丸いですか',completion('そうですね。'),
            completion('地球はおおむね球形で、自転のため赤道付近が少し膨らんでいます。'))
        assert len(requests)==2 and '球形' in result['text']


def test_a_question_generated_inside_answer_is_recorded_without_a_reply_button(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result,_=reply(s,'今日は対局を楽しみました',completion('対局を楽しまれたのですね。どの一手が印象的でしたか？'))
        assert result['asked_followup']
        assert s.experience.pending_question().text=='どの一手が印象的でしたか？'
        reply(s,'中央の切り込みです',completion('中央の切り込みが印象に残ったのですね。形勢が動く一手には面白さがあります。',reply_relation='answered'))
        assert s.experience.state.dialogue_questions[-1].stage=='answered'


def test_truncated_response_is_recomposed_before_voice(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result, requests=reply(s,'地球は丸いですか',completion('地球は',_finish='length'),
            completion('地球はおおむね球形です。'))
        assert len(requests)==2 and result['text']=='地球はおおむね球形です。'


def test_a_boolean_answer_is_not_saved_as_a_meaningless_factual_value(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        q=s.experience.open_question('光る木は夜だけ光りますか？','光る木','確かめたい')
        reply(s,'いいえ',completion('夜だけではないということですね。',reply_relation='answered'))
        assert q.stage=='answered' and not s.knowledge.entries()


def test_emotion_matches_its_own_appraisal_and_stays_mild(tmp_path):
    with closing(Session(tmp_path/'save.db', Settings(playback=False))) as s:
        result,requests=reply(s,'大切な記憶を消すことについてどう思いますか？',
            completion('記憶は私の存在そのものです。私の本質を失わせます。',emotion_reason='記憶を失うかもしれないことへの不安'),
            completion('少し不安です。大切な記憶なので、消す前に相談していただきたいです。',emotion_reason='記憶を失うかもしれないことへの不安'))
        assert len(requests)==2 and s.cognition.affect.anxiety==.22
        assert result['affect_appraisal']['emotion']=='anxious'


def test_extracted_answer_clause_outweighs_an_inconsistent_relation_and_keeps_provenance(tmp_path):
    with closing(Session(tmp_path/'save.db',Settings(playback=False))) as s:
        q=s.experience.open_question('休日は何をして過ごしますか？','休日','相手の関心を知りたい')
        result,_=reply(s,'友人と囲碁をします。読み合いはなぜ面白いのでしょうか？',
            completion('囲碁を楽しまれるのですね。互いの手を予測する過程に面白さがあると思います。',
                       reply_relation='other',answer_quote='友人と囲碁をします。'))
        assert q.stage=='answered' and q.answer_quote=='友人と囲碁をします。'
        claim=s.knowledge.entries()[0]
        assert claim['value']=='友人と囲碁をします。' and claim['evidence'][0]['quote'] in q.reply_quote
        assert result['question_binding']['relation']=='answered'


def test_an_answer_to_esis_question_cannot_receive_only_a_bare_agreement(tmp_path):
    with closing(Session(tmp_path/'save.db',Settings(playback=False))) as s:
        s.experience.open_question('好きな色は何ですか？','好きな色','相手を知りたい')
        result,requests=reply(s,'青です',completion('そうなんですね。',reply_relation='answered',answer_quote='青です'),
            completion('青がお好きなのですね。教えていただいた好みとして覚えておきます。',reply_relation='answered',answer_quote='青です'))
        assert len(requests)==2 and '青' in result['text']


def test_relief_in_esis_own_reply_does_not_increase_the_previous_anger(tmp_path):
    with closing(Session(tmp_path/'save.db',Settings(playback=False))) as s:
        reply(s,'私の意見は聞きません',completion('その点は少し納得できません。私の考えも聞いていただきたいです。',emotion='angry'))
        before=s.cognition.affect.anger
        result,_=reply(s,'意見を尊重します',completion('少し安心しました。ご説明ありがとうございます。',emotion='angry',emotion_reason='前の発言への怒り'))
        assert s.cognition.affect.anger<before and result['affect_appraisal']['emotion']=='neutral'


def test_relief_with_lingering_worry_reduces_overall_distress_instead_of_adding_anger(tmp_path):
    with closing(Session(tmp_path/'save.db',Settings(playback=False))) as s:
        reply(s,'私の意見は聞きません',completion('その点は少し納得できません。私の考えも聞いていただきたいです。',emotion='angry'))
        before=s.cognition.affect.anger
        result,_=reply(s,'意見を尊重します',completion('少し安心しました。少し不安は残っていますが、そのことについて話し合いたいです。',emotion='angry',emotion_reason='前の発言への怒り'))
        assert result['affect_appraisal']['recovering'] and s.cognition.affect.anger==0
        assert 0<s.cognition.affect.anxiety<before
