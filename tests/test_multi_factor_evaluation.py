"""MF-2 선호 평가를 가상 판단·합성 관측으로 검증한다. 실제 선수 품질 결과가 아니다."""

import builtins
import json
import random
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_multi_factor import EXAMPLE, OVR_KEYS
from test_performance import reinforcement
from test_schema import OBSERVED, VALID, card

from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.evaluation.multi_factor import (
    EVALUATOR_SPEC,
    PreferenceSet,
    content_hash,
    evaluate_preferences,
    main,
)
from FRS.schema.abilities import STAT_NAMES
from FRS.schema.models import PlayerCard

ROOT = Path(__file__).resolve().parents[1]
SET_EXAMPLE = ROOT / "docs/examples/multi-factor-preference-set.json"
MATERIAL_EXAMPLE = ROOT / "docs/examples/multi-factor-preference-material.json"


@pytest.fixture
def policy():
    return ScoringPolicy.model_validate_json(EXAMPLE.read_text())


def player(person, value=140, feet=(3, 5), ovr=128, season=123, **changes):
    """모든 능력치가 같은 가상 카드. ST 프로필 점수는 손으로 계산할 수 있다."""
    return card(
        **{
            "spid": season * 1_000_000 + person,
            "season_id": season,
            "person_id": person,
            "name": f"가상선수_{person:04d}",
            "stats": {name: value for name in STAT_NAMES},
            "position_ovr": {key: ovr for key in OVR_KEYS},
            "height": 180,
            "weight": 75,
            "physical": "가상체형_A",
            "left_foot": feet[0],
            "right_foot": feet[1],
            **changes,
        }
    )


# 설계 §7 가상 산술: A는 OVR128·그룹70·약발3=6378, B는 OVR125·그룹68·약발5=7369.
A = player(1)
B = player(2, value=136, feet=(5, 5), ovr=125)


def side(c, level=5, reinforcement_version=None):
    payload = dict(
        card=dict(spid=c.spid, batch_id=c.batch_id, version=c.version),
        reinforce_level=level,
    )
    if reinforcement_version is not None:
        payload["reinforcement"] = dict(
            batch_id=c.batch_id, version=reinforcement_version
        )
    return payload


def judge(verdict, judge_id="judge-1", kind="human", basis="ability", **changes):
    return dict(
        judge_id=judge_id,
        judge_kind=kind,
        verdict=verdict,
        basis=basis,
        rationale="가상 판단 근거",
        play_style="가상 성향",
        judged_at=OBSERVED.isoformat(),
        **changes,
    )


def pair(pair_id, a, b, *judgments, split="holdout", slot="ST", **changes):
    return dict(
        pair_id=pair_id,
        split=split,
        slot=slot,
        a=a if isinstance(a, dict) else side(a),
        b=b if isinstance(b, dict) else side(b),
        judgments=list(judgments),
        **changes,
    )


def preference_set(policy, *pairs, reinforce_batch=None, **material):
    return PreferenceSet.model_validate(
        dict(
            schema_version="mf-preference-set.v1",
            set_id="synthetic-set",
            material={
                "source": "synthetic",
                "player_card_batch": "synthetic-batch",
                "reinforce_stats_batch": reinforce_batch,
                "reference_time": OBSERVED.isoformat(),
                **material,
            },
            policies=[dict(policy_id=policy.policy_id, version=policy.version)],
            pairs=list(pairs),
        )
    )


def evaluate(policy, pset, cards=(A, B), **kwargs):
    return evaluate_preferences(pset, policies=[policy], cards=cards, **kwargs)


def section(report, split="holdout", kind="human"):
    return report["splits"][split][kind]


def test_design_example_compares_ovr_and_policy_on_the_same_judgment(policy):
    report = evaluate(
        policy, preference_set(policy, pair("p1", A, B, judge("prefer_b")))
    )
    rows = section(report)
    ovr, mf = rows["methods"]["ovr"], rows["methods"][policy.policy_id]
    assert (rows["judgments"], rows["comparable"]) == (1, 1)
    assert ovr["agreement"] == dict(numerator=0, denominator=1)
    assert mf["agreement"] == dict(numerator=1, denominator=1)
    assert (ovr["opposite"], mf["agree"]) == (1, 1)
    [case] = rows["cases"]
    assert case == dict(
        method="ovr",
        pair_id="p1",
        judge_id="judge-1",
        slot="ST",
        verdict="prefer_b",
        outcome="a",
        kind="opposite",
        a_value=128,
        b_value=125,
    )
    assert report["quality"] == dict(
        status="measured",
        basis="holdout split human judgments only",
        criteria=None,
        passed=None,
        holdout_human_comparable=1,
        synthetic_judgments=0,
    )


def test_policy_misrank_reports_exact_contribution_differences(policy):
    report = evaluate(
        policy, preference_set(policy, pair("p1", A, B, judge("prefer_a")))
    )
    [case] = [c for c in section(report)["cases"] if c["method"] != "ovr"]
    assert (case["a_value"], case["b_value"], case["profile_id"]) == (6378, 7369, "st")
    # 약발 기여: 16×35/90 − 16×100/90 = −104/9, 다른 그룹: 74×(70−68)/90 중 각 항목.
    assert case["contribution_delta"]["weak_foot"] == dict(
        numerator=-104, denominator=9
    )
    assert case["contribution_delta"]["shooting"] == dict(numerator=28, denominator=45)
    assert "body" not in case["contribution_delta"]


def test_denominators_keep_undecidable_equal_basis_pending_and_exclusions_apart(
    policy,
):
    twin = player(3)
    pset = preference_set(
        policy,
        pair("decisive", A, B, judge("prefer_b")),
        pair("undecidable", player(4), B, judge("undecidable")),
        pair("equal", A, twin, judge("equal")),
        pair("budget", player(5), B, judge("prefer_b", basis="budget")),
        pair("missing", player(6), B, judge("prefer_a")),
        pair("pending", player(7), B),
    )
    cards = (A, B, twin, player(4), player(5), player(7))
    rows = section(evaluate(policy, pset, cards=cards))
    assert report_counts(rows) == (5, 1, 1, 1, 2)
    for method in ("ovr", policy.policy_id):
        tally = rows["methods"][method]
        assert tally["decisive"] == 1 and tally["equal"] == 1
        assert tally["equal_tie"] == 1
        assert tally["equal_agreement"] == dict(numerator=1, denominator=1)
        assert rows["exclusions"][method] == {"a:missing_card": 1}
    report = evaluate(policy, pset, cards=cards)
    assert report["splits"]["holdout"]["pending_pairs"] == 1
    assert report["splits"]["holdout"]["pairs"] == 6


def report_counts(rows):
    return (
        rows["judgments"],
        rows["undecidable"],
        rows["unsupported_basis"],
        rows["data_excluded"],
        rows["comparable"],
    )


def test_method_tie_on_a_decisive_judgment_is_not_agreement(policy):
    twin = player(3)
    rows = section(
        evaluate(
            policy,
            preference_set(policy, pair("p1", A, twin, judge("prefer_a"))),
            cards=(A, twin),
        )
    )
    for method in ("ovr", policy.policy_id):
        tally = rows["methods"][method]
        assert (tally["method_tie"], tally["agree"]) == (1, 0)
        assert tally["agreement"] == dict(numerator=0, denominator=1)
    assert {c["kind"] for c in rows["cases"]} == {"method_tie"}


def test_policy_only_exclusion_removes_the_judgment_from_every_method(policy):
    footless = player(2, value=136, feet=(5, 5), ovr=125, left_foot=None)
    rows = section(
        evaluate(
            policy,
            preference_set(policy, pair("p1", A, footless, judge("prefer_b"))),
            cards=(A, footless),
        )
    )
    assert (rows["data_excluded"], rows["comparable"]) == (1, 0)
    assert rows["exclusions"] == {"ovr": {}, policy.policy_id: {"b:missing_feature": 1}}
    assert rows["methods"]["ovr"]["agreement"] is None


def test_expired_reference_time_excludes_without_extending_validity(policy):
    pset = preference_set(
        policy,
        pair("p1", A, B, judge("prefer_b")),
        reference_time=VALID.isoformat(),
    )
    report = evaluate(policy, pset)
    rows = section(report)
    assert rows["exclusions"]["ovr"] == {
        "a:unusable_observation": 1,
        "b:unusable_observation": 1,
    }
    assert report["quality"]["status"] == "not_evaluated"
    assert A.valid_until == VALID


@pytest.mark.parametrize(
    "first,second",
    [
        (player(1, season=123), player(1, season=124)),
        (side(A), side(A, level=7, reinforcement_version="synthetic-grade7")),
    ],
    ids=["season", "reinforcement"],
)
def test_person_group_leakage_between_tuning_and_holdout_is_rejected(
    policy, first, second
):
    with pytest.raises(ValidationError, match="같은 인물"):
        preference_set(
            policy,
            pair("p1", first, B, split="tuning"),
            pair("p2", second, player(9), split="holdout"),
            reinforce_batch="synthetic-batch",
        )
    same_split = preference_set(
        policy,
        pair("p1", first, B, split="tuning"),
        pair("p2", second, player(9), split="tuning"),
        reinforce_batch="synthetic-batch",
    )
    assert len(same_split.pairs) == 2


@pytest.mark.parametrize(
    "changes,message",
    [
        (dict(a=side(player(1, batch_id="other-batch"))), "명시한 배치"),
        (
            dict(a=side(A, level=7, reinforcement_version="synthetic-grade7")),
            "강화 관측이 명시한 배치",
        ),
        (dict(a={**side(A), "reinforce_level": 7}), "강화 관측 키"),
        (
            dict(a={**side(A), "reinforcement": dict(batch_id="b", version="v")}),
            "강화 관측 키",
        ),
        (dict(b=side(A)), "같은 카드"),
        (dict(slot="CF"), "지원 슬롯"),
        (
            dict(judgments=[judge("prefer_a"), judge("prefer_b")]),
            "같은 판단자",
        ),
        (dict(judgments=[judge("better")]), "verdict"),
    ],
)
def test_pair_contract_errors(policy, changes, message):
    payload = {**pair("p1", A, B), **changes}
    with pytest.raises(ValidationError, match=message):
        preference_set(policy, payload)


def test_duplicate_pairs_and_baseline_policy_name_are_rejected(policy):
    with pytest.raises(ValidationError, match="쌍 ID"):
        preference_set(policy, pair("p1", A, B), pair("p1", A, player(3)))
    with pytest.raises(ValidationError, match="같은 두 카드"):
        preference_set(policy, pair("p1", A, B), pair("p2", B, A))
    payload = preference_set(policy, pair("p1", A, B)).model_dump(mode="json")
    payload["policies"][0]["policy_id"] = "ovr"
    with pytest.raises(ValidationError, match="기준 방식"):
        PreferenceSet.model_validate(payload)


def test_policy_version_and_membership_mismatches_are_rejected(policy):
    pset = preference_set(policy, pair("p1", A, B, judge("prefer_b")))
    changed = json.loads(EXAMPLE.read_text())
    changed["weak_foot_scores"] = [0, 15, 35, 70, 100]
    other = ScoringPolicy.model_validate(changed)
    with pytest.raises(ValueError, match="정책 버전"):
        evaluate_preferences(pset, policies=[other], cards=(A, B))
    with pytest.raises(ValueError, match="고정한 정책과 제공한 정책"):
        evaluate_preferences(pset, policies=[], cards=(A, B))
    renamed = ScoringPolicy.model_validate({**changed, "policy_id": "other-policy"})
    with pytest.raises(ValueError, match="고정한 정책과 제공한 정책"):
        evaluate_preferences(pset, policies=[policy, renamed], cards=(A, B))
    with pytest.raises(ValueError, match="중복 제공"):
        evaluate_preferences(pset, policies=[policy, policy], cards=(A, B))
    with pytest.raises(TypeError):
        evaluate_preferences(pset, policies=[changed], cards=(A, B))


def test_source_and_duplicate_observation_conflicts_are_rejected(policy):
    pset = preference_set(policy, pair("p1", A, B, judge("prefer_b")))
    web = player(2, value=136, feet=(5, 5), ovr=125, source="nexon_web")
    with pytest.raises(ValueError, match="출처"):
        evaluate(policy, pset, cards=(A, web))
    with pytest.raises(ValueError, match="카드 관측 키가 중복"):
        evaluate(policy, pset, cards=(A, B, player(1, value=10)))
    row = reinforcement(A)
    with pytest.raises(ValueError, match="강화 관측 키가 중복"):
        evaluate(policy, pset, reinforcements=(row, row))


@pytest.mark.parametrize("steps", [(0,), (5, 5), (1.5,)])
def test_invalid_sensitivity_steps_are_rejected(policy, steps):
    pset = preference_set(policy, pair("p1", A, B, judge("prefer_b")))
    with pytest.raises(ValueError, match="민감도"):
        evaluate(policy, pset, sensitivity_steps=steps)


def test_reinforced_cards_use_the_linked_observation_and_report_missing_links(policy):
    delta = {name: 10 for name in STAT_NAMES}
    row = reinforcement(A, stats_delta=delta, position_ovr={"ST": 131})
    pset = preference_set(
        policy,
        pair(
            "p1",
            side(A, level=7, reinforcement_version=row.version),
            B,
            judge("prefer_a"),
        ),
        reinforce_batch="synthetic-batch",
    )
    rows = section(evaluate(policy, pset, reinforcements=(row,)))
    assert rows["methods"]["ovr"]["agree"] == 1
    # 강화7: 능력치 150 → 75점, (74×75+16×35)/90 = 67.88.
    assert (
        rows["comparable"] == 1 and rows["methods"][policy.policy_id]["opposite"] == 1
    )
    [case] = [c for c in rows["cases"] if c["method"] != "ovr"]
    assert case["a_value"] == 6789
    report = evaluate(policy, pset)
    assert section(report)["exclusions"]["ovr"] == {"a:missing_reinforcement": 1}
    assert {(i["field"], i["issue"]) for i in report["audit"]["issues"]} == {
        ("reinforcement", "missing_observation")
    }


def test_audit_lists_every_missing_and_out_of_range_field(policy):
    stats = {name: 140 for name in STAT_NAMES if name != "dribble"}
    broken = player(
        2, stats={**stats, "speed": 201}, left_foot=None, right_foot=6, height=None
    )
    pset = preference_set(policy, pair("p1", A, broken, judge("prefer_a")))
    report = evaluate(policy, pset, cards=(A, broken))
    found = {
        (i["spid"], i["field"], i["issue"])
        for i in report["audit"]["issues"]
        if i["policy_id"] == policy.policy_id
    }
    assert found == {
        (broken.spid, "dribble", "missing"),
        (broken.spid, "speed", "out_of_range"),
        (broken.spid, "left_foot", "missing"),
        (broken.spid, "right_foot", "out_of_range"),
    }
    assert report["audit"]["references"] == 2
    assert report["audit"]["missing_optional_fields"] == {
        "height": 1,
        "left_foot": 1,
    }
    counts = {
        (c["field"], c["issue"]): c["references"] for c in report["audit"]["counts"]
    }
    assert counts[("speed", "out_of_range")] == 1
    # scorer는 첫 오류 하나만 제외 사유로 보고한다.
    assert section(report)["exclusions"][policy.policy_id] == {"b:missing_feature": 1}


def test_ablation_and_sensitivity_run_on_tuning_only(policy):
    pset = preference_set(
        policy,
        pair("tune", A, B, judge("prefer_b"), split="tuning"),
        pair("hold", player(3), player(4, value=136, feet=(5, 5)), judge("prefer_b")),
    )
    cards = (A, B, player(3), player(4, value=136, feet=(5, 5)))
    report = evaluate(policy, pset, cards=cards, sensitivity_steps=(-17, -16, 10))
    tuning, holdout = section(report, "tuning"), section(report, "holdout")
    assert holdout["ablation"] is None and holdout["sensitivity"] is None
    ablation = tuning["ablation"][policy.policy_id]
    assert sorted(ablation) == sorted(
        ("control", "passing", "pace", "physical", "shooting", "weak_foot")
    )
    # 약발을 빼면 A(70)가 B(68)보다 높아져 사람 판단과 반대가 된다.
    assert ablation["weak_foot"]["opposite"] == ablation["weak_foot"]["changed"] == 1
    assert ablation["shooting"]["agree"] == 1 and ablation["shooting"]["changed"] == 0
    entries = {
        e["step"]: e
        for e in tuning["sensitivity"]
        if e["component"] == "weak_foot" and e["profile_id"] == "st"
    }
    assert entries[-17] == dict(
        policy_id=policy.policy_id,
        profile_id="st",
        component="weak_foot",
        step=-17,
        base_weight=16,
        skipped="negative",
    )
    assert entries[-16]["opposite"] == entries[-16]["changed"] == 1
    assert entries[10]["agree"] == 1 and entries[10]["changed"] == 0
    assert report["sensitivity_steps"] == [-17, -16, 10]


def test_slot_and_profile_breakdowns_use_the_policy_slot_map(policy):
    cb_a, cb_b = player(5), player(6, value=136, feet=(5, 5), ovr=125)
    pset = preference_set(
        policy,
        pair("st", A, B, judge("prefer_b")),
        pair(
            "rs",
            player(3),
            player(4, value=136, feet=(5, 5), ovr=125),
            judge("prefer_b"),
            slot="RS",
        ),
        pair("cb", cb_a, cb_b, judge("prefer_b"), slot="RCB"),
    )
    cards = (A, B, player(3), player(4, value=136, feet=(5, 5), ovr=125), cb_a, cb_b)
    rows = section(evaluate(policy, pset, cards=cards))
    assert sorted(rows["by_slot"]) == ["RCB", "RS", "ST"]
    profiles = rows["by_profile"][policy.policy_id]
    assert profiles["st"]["decisive"] == 2 and profiles["st"]["agree"] == 2
    # CB 약발 가중치2: (98×70+2×35)/100=69.30 > (98×68+2×100)/100=68.64.
    assert profiles["cb"]["opposite"] == 1


def test_human_and_synthetic_judgments_are_never_pooled(policy):
    pset = preference_set(
        policy,
        pair(
            "p1",
            A,
            B,
            judge("prefer_b", "judge-1"),
            judge("prefer_a", "synthetic-rule", kind="synthetic"),
        ),
    )
    report = evaluate(policy, pset)
    human, synthetic = section(report), section(report, kind="synthetic")
    assert human["methods"][policy.policy_id]["agree"] == 1
    assert synthetic["methods"][policy.policy_id]["opposite"] == 1
    assert report["quality"]["synthetic_judgments"] == 1


@pytest.mark.parametrize(
    "judgments,split,status",
    [
        ((), "holdout", "not_evaluated"),
        ((judge("prefer_b", kind="synthetic"),), "holdout", "not_evaluated"),
        ((judge("prefer_b"),), "tuning", "not_evaluated"),
        ((judge("undecidable"),), "holdout", "not_evaluated"),
        ((judge("prefer_b"),), "holdout", "measured"),
    ],
)
def test_quality_is_never_passed_and_unmeasured_without_holdout_humans(
    policy, judgments, split, status
):
    report = evaluate(
        policy, preference_set(policy, pair("p1", A, B, *judgments, split=split))
    )
    assert report["quality"]["status"] == status
    assert report["quality"]["criteria"] is None
    assert report["quality"]["passed"] is None


def test_report_is_reproducible_and_order_independent(policy):
    pairs = [
        pair("p1", A, B, judge("prefer_b", "judge-1"), judge("equal", "judge-2")),
        pair("p2", player(3), player(4, value=136, feet=(5, 5)), judge("prefer_a")),
        pair("p3", player(5), player(6), judge("prefer_b"), split="tuning"),
    ]
    cards = [A, B, player(3), player(4, value=136, feet=(5, 5)), player(5), player(6)]
    first = evaluate(policy, preference_set(policy, *pairs), cards=cards)
    again = evaluate(policy, preference_set(policy, *pairs), cards=cards)
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    shuffled = [dict(p, judgments=p["judgments"][::-1]) for p in pairs[::-1]]
    random.Random(123).shuffle(cards)
    other = evaluate(policy, preference_set(policy, *shuffled), cards=cards)
    assert other["set_version"] == first["set_version"]
    assert other["report_version"] == first["report_version"]
    body = {k: v for k, v in first.items() if k != "report_version"}
    assert first["report_version"] == content_hash(body)
    assert first["evaluator_version"] == content_hash(EVALUATOR_SPEC)
    changed = [dict(pairs[0], judgments=[judge("prefer_a")]), *pairs[1:]]
    flipped = evaluate(policy, preference_set(policy, *changed), cards=cards)
    assert flipped["set_version"] != first["set_version"]
    assert flipped["report_version"] != first["report_version"]


def test_evaluation_is_pure_and_does_not_mutate_inputs(policy, monkeypatch):
    pset = preference_set(policy, pair("p1", A, B, judge("prefer_b")))
    before = pset.model_dump(), A.model_dump(), policy.model_dump()

    def blocked(*args, **kwargs):
        raise AssertionError("평가기는 파일을 열지 않는다.")

    monkeypatch.setattr(builtins, "open", blocked)
    monkeypatch.setattr(Path, "read_text", blocked)
    evaluate(policy, pset)
    monkeypatch.undo()
    assert (pset.model_dump(), A.model_dump(), policy.model_dump()) == before


def test_examples_are_synthetic_and_unevaluated(policy):
    pset = PreferenceSet.model_validate_json(SET_EXAMPLE.read_text())
    raw = json.loads(MATERIAL_EXAMPLE.read_text())
    assert {c["source"] for c in raw["player_card"]} == {"synthetic"}
    assert all(c["name"].startswith("가상선수_") for c in raw["player_card"])
    judgments = [j for p in pset.pairs for j in p.judgments]
    assert judgments and {j.judge_kind for j in judgments} == {"synthetic"}
    report = evaluate_preferences(
        pset,
        policies=[policy],
        cards=[PlayerCard.model_validate(c) for c in raw["player_card"]],
    )
    assert report["quality"]["status"] == "not_evaluated"
    assert report["quality"]["synthetic_judgments"] == len(judgments)


def test_cli_writes_once_from_json_or_jsonl_material(policy, tmp_path, capsys):
    raw = json.loads(MATERIAL_EXAMPLE.read_text())
    output = tmp_path / "out" / "report.json"
    args = [
        "--set",
        str(SET_EXAMPLE),
        "--policy",
        str(EXAMPLE),
        "--output",
        str(output),
    ]
    assert main([*args, "--material", str(MATERIAL_EXAMPLE), "--sensitivity", "5"]) == 0
    written = json.loads(output.read_text())
    summary = json.loads(capsys.readouterr().out)
    assert summary["quality"] == "not_evaluated"
    assert summary["report_version"] == written["report_version"]
    expected = evaluate_preferences(
        PreferenceSet.model_validate_json(SET_EXAMPLE.read_text()),
        policies=[policy],
        cards=[PlayerCard.model_validate(c) for c in raw["player_card"]],
        sensitivity_steps=(5,),
    )
    assert written == expected
    with pytest.raises(SystemExit):
        main([*args, "--material", str(MATERIAL_EXAMPLE)])
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "player_card.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in raw["player_card"]) + "\n"
    )
    (folder / "reinforce_stats.jsonl").write_text("")
    other = tmp_path / "jsonl.json"
    args[-1] = str(other)
    assert main([*args, "--material-dir", str(folder), "--sensitivity", "5"]) == 0
    assert json.loads(other.read_text()) == expected
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**raw, "card_price": []}))
    with pytest.raises(SystemExit):
        main([*args[:-1], str(tmp_path / "x.json"), "--material", str(bad)])


def test_example_reference_time_is_within_material_validity():
    pset = PreferenceSet.model_validate_json(SET_EXAMPLE.read_text())
    raw = json.loads(MATERIAL_EXAMPLE.read_text())
    reference = pset.material.reference_time
    for c in (PlayerCard.model_validate(row) for row in raw["player_card"]):
        assert c.observed_at <= reference < c.valid_until
        assert c.valid_until - c.observed_at == timedelta(hours=1)
