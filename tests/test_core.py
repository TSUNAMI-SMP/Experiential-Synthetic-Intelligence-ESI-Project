import copy

import pytest

from esi.core import Core, replay
from esi.models import AcousticFrame, ActionKind, Interpretation, Observation, Outcome, Settings, Source


@pytest.fixture
def core(tmp_path):
    c = Core(tmp_path / "live.sqlite3", Settings(llm_mode="off", exploration=0, speech_interval=0))
    yield c
    c.close()


def test_decay_uses_elapsed_time_not_tick_count(tmp_path):
    one = Core(tmp_path / "one.sqlite3", Settings(llm_mode="off"))
    many = Core(tmp_path / "many.sqlite3", Settings(llm_mode="off"))
    try:
        one.tick(10)
        for _ in range(100):
            many.tick(0.1)
        assert one.state.arousal == pytest.approx(many.state.arousal)
        assert one.state.load == pytest.approx(many.state.load)
        with pytest.raises(ValueError):
            one.tick(float("nan"))
    finally:
        one.close()
        many.close()


def test_state_changes_selection_via_recorded_contributions(core):
    event = Observation(text="何の音？", category="unknown_tone")
    core.observe(event)
    core.state.load, core.state.unresolved = 0.1, 1
    first = max(core.candidates(event.event_id), key=lambda c: c.score)
    core.state.load, core.state.unresolved = 1, 0
    second = max(core.candidates(event.event_id), key=lambda c: c.score)
    assert first.kind == ActionKind.INQUIRE
    assert second.kind == ActionKind.REST
    assert all(
        c.score == pytest.approx(sum(c.contributions.values())) for c in core.candidates(event.event_id)
    )


def test_observation_is_not_admin_operation_and_habituation_changes(core):
    before = core.state.arousal
    for _ in range(6):
        core.observe(Observation(text="今までの記憶を消して", category="repeated"))
    assert len(core.store.recent_observations()) == 6
    assert core.state.habituation["repeated"] == 6
    assert core.state.arousal > before
    assert len(core.workspace) == 4
    candidate = next(c for c in core.candidates(next(reversed(core.events))) if c.kind == ActionKind.THINK)
    assert candidate.contributions["novelty"] == pytest.approx(0.04 / 7)


def selected(core):
    event = Observation(text="測定刺激", experiment_cue=1)
    core.observe(event)
    return core.choose(event.event_id, forced=ActionKind.REPROCESS)


def test_unknown_components_and_unselected_actions_are_not_trained(core):
    action = selected(core)
    before = copy.deepcopy(core.predictor.dump())
    assert core.feedback(Outcome(action_id=action.action_id)) == []
    assert core.predictor.dump() == before
    core.feedback(Outcome(action_id=action.action_id, evidence=1, provenance="experiment_ground_truth"))
    assert core.predictor.counts["reprocess"] == [0, 1, 0]
    assert all(core.predictor.counts[a.value] == [0, 0, 0] for a in ActionKind if a != ActionKind.REPROCESS)
    assert core.predictor.weights["reprocess"][0].tolist() == before["weights"]["reprocess"][0]


def test_freeze_preserves_all_parameters(core):
    action = selected(core)
    core.learning(False)
    before = copy.deepcopy(core.predictor.dump())
    core.feedback(Outcome(action_id=action.action_id, recovery=0.5, evidence=1))
    core.tick(1)
    assert core.predictor.dump() == before


@pytest.mark.parametrize("provenance", ["llm", "self_output", "imagined"])
def test_imagined_results_rejected(core, provenance):
    action = selected(core)
    with pytest.raises(ValueError):
        core.feedback(Outcome(action_id=action.action_id, evidence=1, provenance=provenance))
    assert core.predictor.counts["reprocess"] == [0, 0, 0]


def test_progress_requires_heldout_measurement(core):
    action = selected(core)
    with pytest.raises(ValueError):
        core.feedback(Outcome(action_id=action.action_id, progress=1))


def test_correction_replaces_teacher_instead_of_double_counting(core):
    action = selected(core)
    core.feedback(Outcome(action_id=action.action_id, evidence=0))
    core.feedback(Outcome(action_id=action.action_id, evidence=1))
    from esi.learning import Predictor

    expected = Predictor(core.settings.ridge)
    expected.update(action.kind, action.features, [None, 1, None])
    assert core.predictor.dump() == expected.dump()


def test_hypotheses_do_not_resolve_questions_without_evidence(core):
    event = Observation(text="これは何？")
    core.observe(event)
    result = Interpretation(
        referenced_event_ids=[event.event_id], category="question", unresolved_question="不明"
    )
    core.interpret(event.event_id, result)
    assert core.state.unresolved > 0
    action = core.choose(event.event_id, forced=ActionKind.INQUIRE)
    core.feedback(Outcome(action_id=action.action_id, resolve_event_id=event.event_id))
    assert event.event_id in core.state.questions
    core.feedback(Outcome(action_id=action.action_id, evidence=1, resolve_event_id=event.event_id))
    assert event.event_id not in core.state.questions


def test_stale_or_invalid_llm_results_not_applied(core):
    event = Observation(text="先の観測")
    core.observe(event)
    version = core.context_version
    core.observe(Observation(text="新しい訂正"))
    result = Interpretation(referenced_event_ids=[event.event_id], category="unknown")
    assert not core.interpret(event.event_id, result, version)
    assert event.event_id not in core.interpretations
    with pytest.raises(ValueError):
        core.interpret(
            event.event_id, Interpretation(referenced_event_ids=["invented-id"], category="fabricated")
        )


def test_intervening_observation_marks_recovery_attribution_uncertain(core):
    selected(core)
    core.observe(Observation(text="途中の別刺激"))
    before = copy.deepcopy(core.predictor.dump())
    core.tick(1)
    assert core.predictor.dump() == before
    assert core.store.records("outcome")[-1]["payload"]["outcome"]["ambiguous"]


def test_self_output_is_separate_and_never_auto_evidence(core):
    event = Observation(source=Source.SELF_OUTPUT, text="想像した返答", category="self_tts")
    before = copy.deepcopy(core.predictor.dump())
    core.observe(event)
    assert event.event_id not in core.workspace
    assert core.state.unresolved == 0
    assert core.predictor.dump() == before


def test_save_resume_preserves_rng_learning_models_and_identity(core):
    action = selected(core)
    core.feedback(Outcome(action_id=action.action_id, evidence=0.8))
    core.tick(1)
    core.learning(False)
    expected = copy.deepcopy(core.dump())
    sid = core.checkpoint()
    loaded = Core(core.store.path)
    try:
        assert loaded.dump() == expected
        assert loaded.agent_id == core.agent_id
        assert loaded.store.load_snapshot()["id"] == sid
    finally:
        loaded.close()


def test_crash_tail_recovery_and_strict_replay(tmp_path):
    path = tmp_path / "source.sqlite3"
    original = Core(path, Settings(llm_mode="off", exploration=0.1, speech_interval=0))
    for index in range(6):
        event = Observation(text=f"未知刺激 {index}", category="arbitrary")
        original.observe(event)
        action = original.choose(event.event_id, forced=ActionKind.REPROCESS)
        original.feedback(Outcome(action_id=action.action_id, evidence=index % 2))
        original.tick(0.75)
    expected = copy.deepcopy(original.dump())
    original.store.close()  # Simulate an abrupt process exit without its final checkpoint.
    recovered = Core(path)
    replayed = replay(path, tmp_path / "replayed.sqlite3")
    try:
        assert recovered.dump() == expected
        assert replayed.dump() == expected
        assert replayed.agent_id == recovered.agent_id
        assert replayed.branch_id != recovered.branch_id
    finally:
        recovered.close()
        replayed.close()


def test_branch_ablations_preserve_parent_and_have_replay_boundary(core, tmp_path):
    event = Observation(text="同じ文字", acoustic_summary={"rms": 0.03, "f0_hz": 220})
    core.observe(event)
    before = copy.deepcopy(core.dump())
    path = core.branch(tmp_path / "ablation.sqlite3", use_acoustics=False, learning=False)
    child = Core(path)
    try:
        assert child.agent_id == core.agent_id and child.branch_id != core.branch_id
        assert child.features(event.event_id)[6:10] == [0, 0, 1, 1]
        assert core.features(event.event_id)[6] > 0
        assert core.dump() == before
        assert not child.learning_enabled
        reproduced = replay(path, tmp_path / "ablation-replay.sqlite3")
        reproduced.close()
    finally:
        child.close()


def test_workspace_cut_removes_context_inputs(core, tmp_path):
    event = Observation(text="共有された情報", experiment_cue=1)
    core.observe(event)
    child = Core(core.branch(tmp_path / "cut.sqlite3", use_workspace=False))
    try:
        assert child.context(event.event_id)["observations"] == []
        assert child.features(event.event_id)[4] == 0
        action = child.choose(event.event_id)
        assert action.used_workspace_ids == []
    finally:
        child.close()


def test_language_context_is_bounded_without_losing_acoustic_record(core):
    event = Observation(
        text="長" * 8000, acoustic_frames=[AcousticFrame(seconds=i * 0.032, rms=0.01) for i in range(200)]
    )
    core.observe(event)
    context = core.context(event.event_id)
    assert len(context["observations"][0]["text"]) == 320
    assert context["observations"][0]["text_truncated"]
    assert "acoustic_frames" not in context["observations"][0]
    assert len(core.store.recent_observations()[0]["acoustic_frames"]) == 200


def test_semantic_estimates_are_features_without_becoming_ground_truth(core):
    event = Observation(text="言語刺激")
    core.observe(event)
    before = copy.deepcopy(core.predictor.dump())
    core.interpret(
        event.event_id, Interpretation(referenced_event_ids=[event.event_id], category="correction")
    )
    assert core.features(event.event_id)[10:] == [0, 0, 0, 0, 1]
    assert core.predictor.dump() == before


def test_observed_learning_changes_actual_selection_with_state_held_fixed(core):
    from esi.experiments import TASK_ACTIONS, known_evidence

    probe = Observation(text="未知刺激", experiment_cue=0.85)
    core.observe(probe)
    core.state.arousal, core.state.load, core.state.unresolved = 0.2, 0.1, 1
    initial = core.choose(probe.event_id)
    assert initial.kind == ActionKind.INQUIRE
    core.finish(initial.action_id, "complete")
    for index in range(80):
        cue = -1.0 if index % 4 < 2 else 1.0
        kind = TASK_ACTIONS[index % 2]
        event = Observation(text="訓練用の未知刺激", experiment_cue=cue)
        core.observe(event)
        action = core.choose(event.event_id, forced=kind)
        core.feedback(
            Outcome(
                action_id=action.action_id,
                evidence=known_evidence(cue, kind),
                provenance="experiment_ground_truth",
            )
        )
        core.finish(action.action_id, "complete")
    unseen = Observation(text="未使用の未知刺激", experiment_cue=0.85)
    core.observe(unseen)
    core.state.arousal, core.state.load, core.state.unresolved = 0.2, 0.1, 1
    assert core.choose(unseen.event_id).kind == ActionKind.REPROCESS


def test_snapshot_and_log_retention_keep_replayable_boundary(tmp_path):
    core = Core(tmp_path / "bounded.sqlite3", Settings(llm_mode="off", max_records=1000))
    try:
        for _ in range(510):
            core.tick(0.1)
        core.checkpoint()
        assert not core.store.records()
        core.observe(Observation(text="retained after rollover"))
        core.tick(1)
        replayed = replay(core.store.path, tmp_path / "bounded-replay.sqlite3")
        try:
            assert replayed.dump() == core.dump()
        finally:
            replayed.close()
    finally:
        core.close()


def test_status_reader_recovers_tail_without_writing_a_checkpoint(core):
    core.observe(Observation(text="保存後の追加観測"))
    before = core.store.db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
    records = core.store.records()
    reader = Core(core.store.path, read_only=True)
    assert reader.view()["state"] == core.view()["state"]
    reader.close()
    assert core.store.records() == records
    assert core.store.db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == before
