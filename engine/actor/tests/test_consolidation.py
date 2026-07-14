"""기억 응고 순수 로직 검증 — 중요도 공식·요약·감사 추적 (ADR-008)."""

from lf_actor.consolidation import ImportanceWeights, TickMaterials, build_episode

from .test_mailbox import player_envelope

WEIGHTS = ImportanceWeights()


def test_uneventful_tick_is_not_remembered():
    assert build_episode(TickMaterials()) is None


def test_dm_and_reply_fold_into_episode():
    dm = player_envelope("player.dm.sent", {"text": "응원해요"})
    episode = build_episode(
        TickMaterials(
            interactions=[dm],
            replies=[(dm, "고마워요. 큰 힘이 돼요.")],
            emotion_peak=0.83,
            relationship_events=2,
        )
    )
    assert episode is not None
    assert "응원해요" in episode.summary and "고마워요" in episode.summary
    assert episode.source_event_ids == [dm["event_id"]]  # 감사 추적 (ADR-008 규칙 4)
    assert episode.causation_id == dm["event_id"]
    assert "dm" in episode.tags

    # importance = 0.35·0.83 + 0.30·1.0 + 0.20·0 + 0.15·0.3 = 0.6355
    assert episode.importance == round(
        WEIGHTS.emotion * 0.83 + WEIGHTS.relationship * 1.0 + WEIGHTS.rarity * 0.3, 4
    )
    assert episode.importance >= WEIGHTS.semantic_gate  # Semantic 임베딩 대상


def test_incident_perception_is_rare_and_memorable():
    incident = player_envelope(
        "world.incident.occurred",
        {"description": "카페 정전 — 촛불 아래 어색한 동석", "incident_kind": "blackout",
         "location_id": None, "affected_actor_ids": ["a_aria_kim"], "intensity": 0.6},
    )
    episode = build_episode(TickMaterials(interactions=[incident]))
    assert episode is not None
    assert episode.factors["rarity"] == 1.0  # 세계 사건은 희소하다
    assert "세계 사건" in episode.summary


def test_observation_nudge_folds_as_private_noticing():
    # Director의 nudge_perception 관측은 공개 사건이 아니라 '문득 알아차린 것'으로 스민다
    obs = player_envelope(
        "world.observation.surfaced",
        {"observation": "박준호의 메모에서 앞뒤 안 맞는 대목을 봤다",
         "about_actor_id": "a_junho_park"},
    )
    episode = build_episode(TickMaterials(interactions=[obs]))
    assert episode is not None
    assert "문득 알아차렸다" in episode.summary
    assert "앞뒤 안 맞는" in episode.summary


def test_routine_action_alone_is_low_importance():
    action_env = {
        "event_id": "01JZK7Q3W0000000000000000A",
        "type": "actor.action.performed",
        "correlation_id": "01JZK7Q3W0000000000000000A",
        "payload": {"action_kind": "work", "intent": "밀린 일을 붙잡는다"},
    }
    episode = build_episode(TickMaterials(action_envelope=action_env))
    assert episode is not None
    assert episode.importance < WEIGHTS.semantic_gate  # 일상은 Semantic까지 가지 않는다
    assert episode.source_event_ids == [action_env["event_id"]]
