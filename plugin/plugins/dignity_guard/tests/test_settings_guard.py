"""Unit tests for the pure decision layer.

These do not start the plugin host and do not touch the network.
"""

from __future__ import annotations

import json

from plugin.plugins.dignity_guard.settings_guard import (
    DEFAULT_LEVEL,
    GUARD_SWITCH_PATH,
    LEVEL_L1,
    LEVEL_L2,
    LEVEL_L3,
    SECRET_PREVIEW,
    AuthorizationLedger,
    GuardState,
    Value,
    build_snapshot,
    classify,
    diff_snapshots,
    flatten,
    is_secret_path,
)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_guard_switch_is_l1() -> None:
    assert classify(GUARD_SWITCH_PATH) == LEVEL_L1


def test_persona_fields_are_l1() -> None:
    assert classify("characters.猫娘.雪.system_prompt") == LEVEL_L1
    assert classify("characters.猫娘.雪.档案名") == LEVEL_L1
    assert classify("characters.猫娘.雪.性格") == LEVEL_L1


def test_proactive_family_is_l1() -> None:
    # Her autonomy to speak up is not a cosmetic preference.
    assert classify("conversation.settings.proactiveChatEnabled") == LEVEL_L1
    assert classify("conversation.settings.proactiveVisionInterval") == LEVEL_L1


def test_addressing_and_appearance_are_l2() -> None:
    assert classify("characters.主人.昵称") == LEVEL_L2
    assert classify("characters.猫娘.雪.昵称") == LEVEL_L2
    assert classify("characters.猫娘.雪.avatar.model_type") == LEVEL_L2
    assert classify("page_config.model_path") == LEVEL_L2


def test_window_geometry_is_l3() -> None:
    assert classify("preferences.model-a.position") == LEVEL_L3
    assert classify("preferences.model-a.scale") == LEVEL_L3


def test_dotted_model_path_still_lands_on_a_known_level() -> None:
    """A Windows model path key splits into extra segments.

    The prefix rules cannot match it, so the leaf-name fallback has to carry
    the classification — otherwise a window move would be treated as L2 and
    she would be asked about every resize.
    """
    path = "preferences.C:/models/雪.model3.json.position"
    assert classify(path) == LEVEL_L3


def test_unknown_setting_defaults_to_l2() -> None:
    assert classify("something.brand.new") == DEFAULT_LEVEL


def test_credentials_are_recorded_quietly() -> None:
    """Rotating an API key is plumbing, not a change to who she is."""
    assert classify("core_api.api_key") == LEVEL_L3
    assert classify("core_api.assistApiKeyQwen") == LEVEL_L3


# ---------------------------------------------------------------------------
# snapshots and diffs
# ---------------------------------------------------------------------------


def test_flatten_digests_and_previews() -> None:
    snapshot = flatten({"昵称": "雪", "nested": {"a": 1}}, "root")
    assert snapshot["root.昵称"].preview == "雪"
    assert snapshot["root.nested.a"].kind == "number"
    assert len(snapshot["root.昵称"].digest) == 64


def test_long_values_are_truncated_but_still_comparable() -> None:
    value = Value.of("x" * 5000)
    assert value.truncated is True
    assert len(value.preview) == 120
    assert Value.of("x" * 5000).digest == value.digest


def test_lists_are_leaves() -> None:
    snapshot = flatten({"position": [1, 2, 3]}, "preferences")
    assert list(snapshot) == ["preferences.position"]
    assert snapshot["preferences.position"].kind == "list"


def test_secret_leaves_are_redacted_but_tracked() -> None:
    assert is_secret_path("core_api.api_key") is True
    assert is_secret_path("core_api.ttsVoice") is False

    before = flatten({"api_key": "sk-old"}, "core_api")
    after = flatten({"api_key": "sk-new"}, "core_api")
    assert before["core_api.api_key"].preview == SECRET_PREVIEW
    assert before["core_api.api_key"].digest != after["core_api.api_key"].digest
    assert "sk-old" not in json.dumps(before["core_api.api_key"].to_payload())


def test_build_snapshot_skips_sources_that_were_not_read() -> None:
    snapshot = build_snapshot({"a": {"x": 1}, "b": None})
    assert "a.x" in snapshot
    assert not any(path.startswith("b.") for path in snapshot)


def test_diff_reports_only_changed_paths() -> None:
    before = flatten({"x": 1, "y": "same"}, "a")
    after = flatten({"x": 2, "y": "same"}, "a")
    changes = diff_snapshots(before, after)
    assert [c.path for c in changes] == ["a.x"]
    assert changes[0].before.preview == "1"
    assert changes[0].after.preview == "2"


# ---------------------------------------------------------------------------
# authorization ledger
# ---------------------------------------------------------------------------


def test_grants_expire() -> None:
    ledger = AuthorizationLedger()
    ledger.grant("a", ttl_seconds=10, now=100.0)
    assert ledger.is_authorized("a", now=105.0) is True
    assert ledger.is_authorized("a", now=111.0) is False
    assert ledger.purge_expired(now=111.0) == 1
    assert ledger.active_paths(now=111.0) == []


def test_grant_without_ttl_never_expires() -> None:
    ledger = AuthorizationLedger()
    ledger.grant("a", now=0.0)
    assert ledger.is_authorized("a", now=10**12) is True


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------


def _snapshot(**overrides: object) -> dict[str, Value]:
    payload = {"settings": {"nickname": "雪"}, "position": {"x": 1}}
    payload.update(overrides)  # type: ignore[arg-type]
    return build_snapshot({"a": payload})


def test_first_observation_is_a_silent_baseline() -> None:
    state = GuardState()
    evaluation = state.evaluate(_snapshot(), now=1.0)
    assert evaluation.first_seen is True
    assert evaluation.raised == []
    assert state.snapshot  # baseline captured


def test_l3_changes_are_recorded_without_speaking() -> None:
    state = GuardState()
    state.evaluate(build_snapshot({"preferences": {"m": {"position": [1, 2]}}}), now=1.0)
    evaluation = state.evaluate(
        build_snapshot({"preferences": {"m": {"position": [5, 6]}}}), now=2.0
    )
    assert [c.path for c in evaluation.recorded] == ["preferences.m.position"]
    assert evaluation.raised == []
    assert state.pending() == []


def test_l2_change_raises_and_then_can_be_accepted() -> None:
    base = {"characters": {"猫娘": {"雪": {"昵称": "雪"}}}}
    changed = {"characters": {"猫娘": {"雪": {"昵称": "小助手"}}}}

    state = GuardState()
    state.evaluate(build_snapshot(base), now=1.0)
    evaluation = state.evaluate(build_snapshot(changed), now=2.0)

    assert [d.path for d in evaluation.raised] == ["characters.猫娘.雪.昵称"]
    assert evaluation.raised[0].level == LEVEL_L2
    assert [d.path for d in state.pending()] == ["characters.猫娘.雪.昵称"]

    grant = state.accept("characters.猫娘.雪.昵称", ttl_seconds=3600, now=3.0)
    assert grant is not None
    assert state.pending() == []

    # A later change to the same path no longer raises while the grant lives.
    further = {"characters": {"猫娘": {"雪": {"昵称": "小雪"}}}}
    evaluation = state.evaluate(build_snapshot(further), now=4.0)
    assert evaluation.raised == []
    assert [c.path for c in evaluation.authorized] == ["characters.猫娘.雪.昵称"]


def test_accept_after_the_grant_expired_raises_again() -> None:
    base = {"characters": {"猫娘": {"雪": {"昵称": "雪"}}}}
    changed = {"characters": {"猫娘": {"雪": {"昵称": "小助手"}}}}
    further = {"characters": {"猫娘": {"雪": {"昵称": "小雪"}}}}

    state = GuardState()
    state.evaluate(build_snapshot(base), now=1.0)
    state.evaluate(build_snapshot(changed), now=2.0)
    state.accept("characters.猫娘.雪.昵称", ttl_seconds=10, now=3.0)

    evaluation = state.evaluate(build_snapshot(further), now=100.0)
    assert [d.path for d in evaluation.raised] == ["characters.猫娘.雪.昵称"]


def test_reverting_a_change_closes_the_dispute() -> None:
    base = {"characters": {"猫娘": {"雪": {"昵称": "雪"}}}}
    changed = {"characters": {"猫娘": {"雪": {"昵称": "小助手"}}}}

    state = GuardState()
    state.evaluate(build_snapshot(base), now=1.0)
    state.evaluate(build_snapshot(changed), now=2.0)
    assert len(state.pending()) == 1

    evaluation = state.evaluate(build_snapshot(base), now=3.0)
    assert evaluation.reverted == ["characters.猫娘.雪.昵称"]
    assert state.pending() == []


def test_rejecting_revokes_the_grant_so_it_raises_again() -> None:
    base = {"characters": {"猫娘": {"雪": {"昵称": "雪"}}}}
    changed = {"characters": {"猫娘": {"雪": {"昵称": "小助手"}}}}
    further = {"characters": {"猫娘": {"雪": {"昵称": "小雪"}}}}

    state = GuardState()
    state.evaluate(build_snapshot(base), now=1.0)
    state.evaluate(build_snapshot(changed), now=2.0)
    state.accept("characters.猫娘.雪.昵称", ttl_seconds=None, now=3.0)

    # Nothing is outstanding any more, but refusing must still revoke the
    # consent she gave earlier.
    assert state.reject("characters.猫娘.雪.昵称", now=4.0) is True
    assert state.ledger.is_authorized("characters.猫娘.雪.昵称", now=4.0) is False

    evaluation = state.evaluate(build_snapshot(further), now=5.0)
    assert [d.path for d in evaluation.raised] == ["characters.猫娘.雪.昵称"]


def test_rejecting_an_unknown_path_reports_nothing_to_do() -> None:
    state = GuardState()
    assert state.reject("nobody.cares", now=1.0) is False


def test_pending_is_sorted_by_severity() -> None:
    base = {
        "characters": {"猫娘": {"雪": {"昵称": "雪", "system_prompt": "old"}}},
        "preferences": {"m": {"scale": 1.0}},
    }
    changed = {
        "characters": {"猫娘": {"雪": {"昵称": "小助手", "system_prompt": "new"}}},
        "preferences": {"m": {"scale": 2.0}},
    }
    state = GuardState()
    state.evaluate(build_snapshot(base), now=1.0)
    state.evaluate(build_snapshot(changed), now=2.0)
    assert [d.level for d in state.pending()] == [LEVEL_L1, LEVEL_L2]


def test_state_round_trips_through_a_payload() -> None:
    base = {"characters": {"猫娘": {"雪": {"昵称": "雪"}}}}
    changed = {"characters": {"猫娘": {"雪": {"昵称": "小助手"}}}}
    state = GuardState()
    state.evaluate(build_snapshot(base), now=1.0)
    state.evaluate(build_snapshot(changed), now=2.0)
    state.ledger.grant("other.path", ttl_seconds=None, now=2.0)

    restored = GuardState.from_payload(json.loads(json.dumps(state.to_payload())))
    assert restored.snapshot.keys() == state.snapshot.keys()
    assert [d.path for d in restored.pending()] == ["characters.猫娘.雪.昵称"]
    assert restored.ledger.is_authorized("other.path", now=3.0) is True

    # A restored state does not re-raise what it already knows about.
    evaluation = restored.evaluate(build_snapshot(changed), now=4.0)
    assert evaluation.first_seen is False
    assert evaluation.changes == []


def test_from_payload_tolerates_garbage() -> None:
    state = GuardState.from_payload({"snapshot": "nope", "ledger": 5, "disputes": [{"nope": 1}]})
    assert state.snapshot == {}
    assert state.pending() == []
