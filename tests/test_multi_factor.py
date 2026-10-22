"""합성 관측과 명시 실험 정책으로 MF-1 산술·경계·출처를 검증한다."""

import copy
import json
from contextlib import nullcontext
from datetime import timedelta
from decimal import localcontext
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_model_input_contract import complete
from test_performance import reinforcement
from test_schema import OBSERVED

from FRS.core.squad_recommend.multi_factor import MultiFactorResult, score_multi_factor
from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.core.squad_recommend.performance import PerformanceQuery, score_performance
from FRS.schema.abilities import STAT_NAMES
from FRS.schema.scope import FORMATION_SLOTS

EXAMPLE = (
    Path(__file__).resolve().parents[1] / "docs/examples/multi-factor-core-policy.json"
)
SLOTS = sorted({s for slots in FORMATION_SLOTS.values() for s in slots})
OVR_KEYS = ("GK", "CB", "RB", "LB", "CDM", "CM", "CAM", "ST", "LW", "RW", "LM", "RM")


@pytest.fixture
def payload():
    return json.loads(EXAMPLE.read_text())


@pytest.fixture
def policy(payload):
    return ScoringPolicy.model_validate(payload)


def card(value=140, **changes):
    return complete(
        **{
            "stats": {name: value for name in STAT_NAMES},
            "position_ovr": {key: 128 for key in OVR_KEYS},
            **changes,
        }
    )


def score(policy, base=None, *, slot="ST", grade=5, when=OBSERVED, **kwargs):
    base = card() if base is None else base
    query = PerformanceQuery(
        spid=base.spid, slot=slot, reinforce_level=grade, when=when
    )
    return score_multi_factor(
        query,
        policy=policy,
        expected_policy_version=policy.version,
        profile_id=next(s.profile_id for s in policy.slots if s.slot == slot),
        card=base,
        **kwargs,
    )


def only_component(payload, name):
    for profile in payload["profiles"]:
        for weight in profile["weights"]:
            weight["weight"] = 100 if weight["name"] == name else 0
    return payload


def body_payload(payload):
    payload["mode"] = "body-aware"
    rule = dict(
        height_weight=30,
        weight_weight=20,
        shape_weight=50,
        height_curve=dict(
            points=[
                dict(value=160, score=0),
                dict(value=180, score=100),
                dict(value=200, score=0),
            ]
        ),
        weight_curve=dict(points=[dict(value=50, score=0), dict(value=100, score=100)]),
        shapes=[dict(name="가상체형_A", score=80), dict(name="가상체형_B", score=20)],
    )
    for role in payload["profiles"]:
        role["body"] = copy.deepcopy(rule)
    return payload


def test_documented_lower_ovr_better_weak_foot_example(policy):
    a = score(policy, card(left_foot=3))
    b = score(policy, card(136, left_foot=5, position_ovr={"ST": 125}))
    assert (a.raw_ovr, a.score_units, a.display_score) == (128, 6378, "63.78")
    assert (b.raw_ovr, b.score_units, b.display_score) == (125, 7369, "73.69")
    assert a.exact_score.fraction() == Fraction(574, 9)
    assert b.exact_score.fraction() == Fraction(3316, 45)
    assert a.scorer_version == b.scorer_version == policy.version
    assert "body" not in {c.name for c in a.components}
    assert a.input_version != b.input_version
    assert MultiFactorResult.model_validate_json(b.model_dump_json()) == b


@pytest.mark.parametrize("slot", SLOTS)
@pytest.mark.parametrize("stat,foot,expected", [(0, 1, 0), (200, 5, 10000)])
def test_all_supported_slots_use_a_fixed_bounded_scale(
    policy, slot, stat, foot, expected
):
    result = score(policy, card(stat, left_foot=foot), slot=slot)
    assert result.exclusion is None and result.score_units == expected
    assert sum(c.contribution.fraction() for c in result.components) == expected / 100


@pytest.mark.parametrize(
    "foot,expected", [(1, 0), (2, 1500), (3, 3500), (4, 8000), (5, 10000)]
)
def test_weak_foot_is_minimum_of_both_feet_with_explicit_nonlinear_table(
    payload, foot, expected
):
    p = ScoringPolicy.model_validate(only_component(payload, "weak_foot"))
    assert score(p, card(left_foot=foot)).score_units == expected
    assert score(p, card(left_foot=5, right_foot=foot)).score_units == expected


def test_weak_foot_sensitivity_depends_on_role(policy):
    diffs = []
    for slot in ("ST", "RCB"):
        a, b = (score(policy, card(left_foot=f), slot=slot) for f in (3, 4))
        diffs.append(b.exact_score.fraction() - a.exact_score.fraction())
    assert diffs == [Fraction(8), Fraction(1)]


@pytest.mark.parametrize("name", STAT_NAMES)
def test_missing_each_active_stat_is_excluded_without_reweighting(policy, name):
    group = next(g.name for g in policy.groups if any(s.name == name for s in g.stats))
    role = next(
        p.name
        for p in policy.profiles
        if any(w.name == group and w.weight for w in p.weights)
    )
    slot = next(s.slot for s in policy.slots if s.profile_id == role)
    base = card()
    del base.stats[name]
    result = score(policy, base, slot=slot)
    assert result.exclusion == "missing_feature" and result.fields == (name,)
    assert result.score_units is None and not result.components


def test_inactive_gk_body_fields_are_not_required_and_no_candidate_context(policy):
    base = card(height=None, weight=None, physical=None)
    for name in STAT_NAMES:
        if name.startswith("gk_"):
            del base.stats[name]
    a = score(policy, base)
    for another in (card(1), card(200)):
        score(policy, another)
    assert score(policy, base) == a and a.score_units == 6378
    keeper = score(policy, card(140, left_foot=None, right_foot=None), slot="GK")
    assert keeper.score_units == 7000


@pytest.mark.parametrize("level", range(5, 12))
def test_selected_reinforcement_uses_delta_once_and_preserves_foot(policy, level):
    base = card()
    delta = 2 * (level - 5)
    enhanced = reinforcement(
        base,
        reinforce_level=level,
        stats_delta={k: delta for k in STAT_NAMES},
        position_ovr={"ST": 128 + delta},
    )
    before = copy.deepcopy((base.model_dump(), enhanced.model_dump()))
    result = score(policy, base, grade=level, reinforcement=enhanced)
    expected = Fraction(74 * (70 + delta // 2) + 16 * 35, 90)
    assert result.exact_score.fraction() == expected
    assert result.raw_ovr == 128 + delta
    assert len(result.observations) == (1 if level == 5 else 2)
    assert (base.model_dump(), enhanced.model_dump()) == before


def test_delta_missing_is_not_zero_and_unused_base_row_is_ignored(policy):
    base = card()
    enhanced = reinforcement(base, position_ovr={"ST": 130})
    a = score(policy, base, grade=7, reinforcement=enhanced)
    assert a.exclusion == "missing_stat_delta" and not a.components
    unused = enhanced.model_copy(update={"status": "unknown", "valid_until": None})
    assert score(policy, base, reinforcement=unused) == score(policy, base)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"reinforce_level": 6}, "reinforcement_mismatch"),
        ({"baseline_card_version": "different"}, "reinforcement_mismatch"),
        ({"baseline_reinforce_level": 4}, "reinforcement_mismatch"),
        ({"status": "unknown", "valid_until": None}, "unusable_observation"),
        ({"valid_until": OBSERVED + timedelta(minutes=1)}, None),
    ],
)
def test_exact_reinforcement_identity_status_and_provenance(policy, changes, reason):
    base = card()
    enhanced = reinforcement(
        base,
        **{
            "stats_delta": {k: 2 for k in STAT_NAMES},
            "position_ovr": {"ST": 130},
            **changes,
        },
    )
    result = score(policy, base, grade=7, reinforcement=enhanced)
    assert result.exclusion == reason
    if reason is None:
        assert result.observations[1].valid_until == enhanced.valid_until


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("left_foot", None, "missing_feature"),
        ("right_foot", None, "missing_feature"),
        ("left_foot", 6, "invalid_foot"),
        ("right_foot", 6, "invalid_foot"),
    ],
)
def test_foot_missing_or_invalid_does_not_fall_back_to_ovr(
    policy, field, value, reason
):
    result = score(policy, card(**{field: value}))
    assert result.exclusion == reason and result.fields == (field,)
    assert result.score_units is None and result.raw_ovr == 128


@pytest.mark.parametrize(
    "field,value",
    [("left_foot", True), ("right_foot", 2.5), ("stats", {"speed": True})],
)
def test_observation_copy_bypass_is_revalidated(policy, field, value):
    base = card().model_copy(update={field: value})
    warning = (
        pytest.warns(UserWarning, match="serializer")
        if isinstance(value, float)
        else nullcontext()
    )
    with warning:
        result = score(policy, base)
    assert result.exclusion == "invalid_observation" and result.score_units is None


@pytest.mark.parametrize("offset", [timedelta(microseconds=-1), timedelta(hours=1)])
def test_observation_time_boundaries_exclude_pair(policy, offset):
    result = score(policy, card(), when=OBSERVED + offset)
    assert result.exclusion == "unusable_observation"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"position_ovr": {}}, "missing_ovr"),
        ({"position_ovr": {"ST": 201}}, "ovr_out_of_range"),
        ({"base_reinforce_level": 1}, "baseline_mismatch"),
        ({"status": "unknown", "valid_until": None}, "unusable_observation"),
    ],
)
def test_initial_comparison_keeps_existing_ovr_eligibility(policy, change, reason):
    assert score(policy, card(**change)).exclusion == reason


def test_stat_normalization_boundaries_and_overflow(policy, payload):
    assert score(policy, card(201)).exclusion == "stat_out_of_range"
    base = card()
    negative = reinforcement(
        base, stats_delta={k: -141 for k in STAT_NAMES}, position_ovr={"ST": 130}
    )
    assert (
        score(policy, base, grade=7, reinforcement=negative).exclusion
        == "stat_out_of_range"
    )
    for group in payload["groups"]:
        for stat in group["stats"]:
            stat["upper"] = 2**63 - 1
    p = ScoringPolicy.model_validate(payload)
    base = card(2**63 - 1, left_foot=5)
    assert score(p, base).score_units == 10000
    overflow = reinforcement(
        base, stats_delta={k: 1 for k in STAT_NAMES}, position_ovr={"ST": 130}
    )
    assert score(p, base, grade=7, reinforcement=overflow).exclusion == "stat_overflow"


@pytest.mark.parametrize("raw,units", [(0, 0), (1, 1), (3, 2), (5, 3)])
def test_exact_half_up_once_independent_of_decimal_context(payload, raw, units):
    only_component(payload, "pace")
    for group in payload["groups"]:
        if group["name"] == "pace":
            for stat in group["stats"]:
                stat.update(weight=100 if stat["name"] == "speed" else 0, upper=20000)
    p = ScoringPolicy.model_validate(payload)
    with localcontext() as context:
        context.prec = 2
        result = score(p, card(raw))
    assert result.exact_score.fraction() == Fraction(raw, 200)
    assert result.score_units == units and result.display_score == f"0.{units:02d}"


def test_body_piecewise_interpolation_categorical_preference_and_core_separation(
    payload, policy
):
    p = ScoringPolicy.model_validate(body_payload(payload))
    a = card(height=170, weight=75, left_foot=5)
    result = score(p, a)
    assert result.score_units == 7430
    body = next(c for c in result.components if c.name == "body")
    assert body.value.fraction() == 65 and body.contribution.fraction() == Fraction(
        13, 2
    )
    assert score(p, card(height=190, weight=75, left_foot=5)).score_units == 7430
    assert score(p, card(height=180, weight=75, left_foot=5)).score_units == 7580
    assert (
        score(
            p, card(height=170, weight=75, left_foot=5, physical="가상체형_B")
        ).score_units
        == 7130
    )
    assert p.version != policy.version
    assert score(policy, card(physical=None)).exclusion is None


@pytest.mark.parametrize(
    "changes,reason,field",
    [
        ({"height": None}, "missing_feature", "height"),
        ({"weight": None}, "missing_feature", "weight"),
        ({"physical": None}, "missing_feature", "physical"),
        ({"height": 159}, "body_out_of_range", "height"),
        ({"height": 201}, "body_out_of_range", "height"),
        ({"weight": 101}, "body_out_of_range", "weight"),
        ({"physical": "가상체형_C"}, "unknown_body_shape", "physical"),
    ],
)
def test_body_missing_unknown_or_outside_anchor_never_uses_neutral_score(
    payload, changes, reason, field
):
    p = ScoringPolicy.model_validate(body_payload(payload))
    result = score(p, card(**changes))
    assert result.exclusion == reason and result.fields == (field,)
    assert result.score_units is None and not result.components


def test_body_rules_can_differ_by_role_and_ignore_disabled_fields(payload):
    body_payload(payload)
    for role in payload["profiles"]:
        role["body"] = dict(
            height_weight=0,
            weight_weight=0,
            shape_weight=100,
            shapes=[dict(name="가상체형_A", score=100 if role["name"] == "st" else 0)],
        )
    p = ScoringPolicy.model_validate(payload)
    base = card(height=None, weight=None)
    st = next(c for c in score(p, base).components if c.name == "body")
    cb = next(c for c in score(p, base, slot="RCB").components if c.name == "body")
    assert st.value.fraction() == 100 and cb.value.fraction() == 0


@pytest.mark.parametrize(
    "kind",
    [
        "group_sum",
        "group_duplicate",
        "unknown_stat",
        "duplicate_stat",
        "upper_zero",
        "negative_weight",
        "bool_weight",
        "float_weight",
        "profile_sum",
        "profile_duplicate",
        "slot_duplicate",
        "unsupported_slot",
        "unknown_profile",
        "weak_length",
        "weak_decline",
        "core_body_only",
        "core_body_function",
        "missing_body_function",
        "duplicate_shape",
        "unordered_curve",
    ],
)
def test_invalid_or_ambiguous_policies_are_rejected(payload, kind):
    if kind == "group_sum":
        payload["groups"][0]["stats"][0]["weight"] += 1
    elif kind == "group_duplicate":
        payload["groups"][0]["name"] = payload["groups"][1]["name"]
    elif kind == "unknown_stat":
        payload["groups"][0]["stats"][0]["name"] = "unknown"
    elif kind == "duplicate_stat":
        payload["groups"][0]["stats"][0]["name"] = payload["groups"][1]["stats"][0][
            "name"
        ]
    elif kind == "upper_zero":
        payload["groups"][0]["stats"][0]["upper"] = 0
    elif kind in ("negative_weight", "bool_weight", "float_weight"):
        payload["profiles"][0]["weights"][0]["weight"] = {
            "negative_weight": -1,
            "bool_weight": True,
            "float_weight": 14.0,
        }[kind]
    elif kind == "profile_sum":
        payload["profiles"][0]["weights"][0]["weight"] += 1
    elif kind == "profile_duplicate":
        payload["profiles"][0]["name"] = payload["profiles"][1]["name"]
    elif kind == "slot_duplicate":
        payload["slots"][0] = payload["slots"][1]
    elif kind == "unsupported_slot":
        payload["slots"][0]["slot"] = "CF"
    elif kind == "unknown_profile":
        payload["slots"][0]["profile_id"] = "unknown"
    elif kind == "weak_length":
        payload["weak_foot_scores"].pop()
    elif kind == "weak_decline":
        payload["weak_foot_scores"][0] = 100
    elif kind == "core_body_only":
        only_component(payload, "body")
    elif kind == "core_body_function":
        body_payload(payload)
        payload["mode"] = "core-stat-foot"
    elif kind == "missing_body_function":
        payload["mode"] = "body-aware"
    elif kind == "duplicate_shape":
        body_payload(payload)
        payload["profiles"][0]["body"]["shapes"] *= 2
    elif kind == "unordered_curve":
        body_payload(payload)
        payload["profiles"][0]["body"]["height_curve"]["points"].reverse()
    with pytest.raises(ValidationError):
        ScoringPolicy.model_validate(payload)


def test_policy_version_and_explicit_profile_are_checked_before_data(policy):
    q = PerformanceQuery(spid=123000001, slot="ST", reinforce_level=5, when=OBSERVED)
    with pytest.raises(ValueError, match="버전"):
        score_multi_factor(
            q,
            policy=policy,
            expected_policy_version="0" * 64,
            profile_id="st",
            card=None,
        )
    with pytest.raises(ValueError, match="프로필"):
        score_multi_factor(
            q,
            policy=policy,
            expected_policy_version=policy.version,
            profile_id="cb",
            card=None,
        )
    broken = policy.model_copy(update={"weak_foot_scores": (100, 0, 0, 0, 0)})
    with pytest.raises(ValidationError):
        score_multi_factor(
            q,
            policy=broken,
            expected_policy_version=policy.version,
            profile_id="st",
            card=None,
        )


def test_semantic_row_order_and_caller_mutation_do_not_change_policy(policy, payload):
    for key in ("groups", "profiles", "slots"):
        payload[key].reverse()
    for g in payload["groups"]:
        g["stats"].reverse()
    for p in payload["profiles"]:
        p["weights"].reverse()
    reordered = ScoringPolicy.model_validate(payload)
    assert reordered.version == policy.version and score(reordered) == score(policy)
    exported = policy.spec()
    exported["policy"]["groups"].clear()
    assert len(policy.groups) == 7
    with pytest.raises(ValidationError):
        policy.profiles[0].weights[0].weight = 0
    payload["groups"][0]["stats"][0]["upper"] += 1
    assert ScoringPolicy.model_validate(payload).version != policy.version


def test_score_has_no_io_and_existing_ovr_scorer_remains_unchanged(policy, monkeypatch):
    base = card()
    before = copy.deepcopy((base.model_dump(), policy.model_dump()))
    monkeypatch.setattr(
        "builtins.open", lambda *a, **k: pytest.fail("점수 함수 파일 I/O 금지")
    )
    result = score(policy, base)
    assert result.score_units == 6378
    q = result.query
    assert score_performance(q, card=base).performance == 64
    assert (base.model_dump(), policy.model_dump()) == before


@pytest.mark.parametrize(
    "group,key,expected",
    [
        ("pace", "speed", 7750),
        ("shooting", "positioning", 6000),
        ("passing", "cross", 5750),
        ("control", "dribble", 6250),
        ("defending", "intercept", 6500),
        ("physical", "header", 5750),
        ("gk", "gk_kick", 5250),
    ],
)
def test_internal_weights_on_different_stat_values_have_hand_calculated_results(
    payload, group, key, expected
):
    p = ScoringPolicy.model_validate(only_component(payload, group))
    base = card(100)
    base.stats[key] = 200
    assert score(p, base).score_units == expected


def test_missing_invalid_card_and_non_model_arguments(policy):
    q = PerformanceQuery(spid=123000001, slot="ST", reinforce_level=5, when=OBSERVED)
    args = dict(policy=policy, expected_policy_version=policy.version, profile_id="st")
    assert score_multi_factor(q, card=None, **args).exclusion == "missing_card"
    assert score_multi_factor(q, card={}, **args).exclusion == "invalid_observation"
    with pytest.raises(TypeError, match="ScoringPolicy"):
        score_multi_factor(q, card=None, **{**args, "policy": policy.model_dump()})
    with pytest.raises(TypeError, match="PerformanceQuery"):
        score_multi_factor({}, card=None, **args)


def test_unusable_reinforcement_only_affects_selected_grade_and_expiry_boundary(policy):
    base = card()
    enhanced = reinforcement(
        base,
        stats_delta={k: 2 for k in STAT_NAMES},
        position_ovr={"ST": 130},
        valid_until=OBSERVED + timedelta(minutes=1),
    )
    assert (
        score(
            policy, base, grade=7, when=enhanced.valid_until, reinforcement=enhanced
        ).exclusion
        == "unusable_observation"
    )
    assert score(policy, base, grade=7).exclusion == "missing_reinforcement"
    assert score(policy, base, when=enhanced.valid_until).exclusion is None


def test_mixed_denominators_round_only_final_total(payload):
    only_component(payload, "pace")
    group = next(g for g in payload["groups"] if g["name"] == "pace")
    group["stats"][0].update(weight=50, upper=3)
    group["stats"][1].update(weight=50, upper=7)
    p = ScoringPolicy.model_validate(payload)
    base = card(1)
    result = score(p, base)
    assert result.exact_score.fraction() == Fraction(500, 21)
    assert result.score_units == 2381
