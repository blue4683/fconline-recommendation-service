"""MF-2 판단 수집 준비를 합성 카드로 검증한다. 판단은 테스트가 명시한 가상 값이다."""

import hashlib
import json
import random
from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_multi_factor import EXAMPLE
from test_multi_factor_evaluation import SET_EXAMPLE, player
from test_schema import OBSERVED

import FRS.core.squad_recommend.multi_factor as scorer
from FRS.core.squad_recommend.multi_factor_policy import ScoringPolicy
from FRS.evaluation import multi_factor as evaluation
from FRS.evaluation.preference_prep import (
    DISCLOSED,
    HIDDEN,
    JudgmentSheet,
    SelectionSpec,
    import_judgments,
    judgment_template,
    main,
    render_form,
    select_pairs,
)

FAMILIES = dict(
    st="ST", wide="LW", cam="CAM", cm="CM", cdm="CDM", cb="CB", fb="LB", gk="GK"
)
PROFILES = tuple(FAMILIES)


@pytest.fixture
def policy():
    return ScoringPolicy.model_validate_json(EXAMPLE.read_text())


def spec(policy, **changes):
    return SelectionSpec.model_validate(
        {
            "schema_version": "mf-pair-selection.v1",
            "set_id": "synthetic-prep",
            "source": "synthetic",
            "player_card_batch": "synthetic-batch",
            "reference_time": OBSERVED.isoformat(),
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
            "profiles": list(PROFILES),
            "pairs_per_profile": 3,
            "holdout_per_profile": 1,
            "seed": 123,
            "reinforce_level": 5,
            "max_ovr_gap": 2,
            "min_pace_gap": 10,
            **changes,
        }
    )


def pool(per_family=8, families=FAMILIES, start=1):
    """계열마다 OVR 차이 2 이내·약발이 번갈아 다른 가상 카드. OVR은 187~189."""
    cards, person = [], start
    for family in families.values():
        for index in range(per_family):
            feet = (3, 5) if index % 2 else (5, 5)
            cards.append(
                player(person, feet=feet, ovr=187 + index % 3, positions=[family])
            )
            person += 1
    return cards


def test_selection_is_deterministic_balanced_and_person_disjoint(policy):
    cards = pool()
    pset, manifest = select_pairs(spec(policy), policy=policy, cards=cards)
    shuffled = cards[:]
    random.Random(7).shuffle(shuffled)
    again, other = select_pairs(spec(policy), policy=policy, cards=shuffled)
    assert again.version == pset.version and other == manifest
    assert len(pset.pairs) == 24 and manifest["shortfall"] == 0
    for profile in PROFILES:
        rows = [p for p in pset.pairs if p.pair_id.startswith(f"{profile}-")]
        assert sorted(p.split for p in rows) == ["holdout", "tuning", "tuning"]
        assert manifest["profiles"][profile]["chosen"] == 3
    persons = [s.card.person_id for p in pset.pairs for s in (p.a, p.b)]
    assert len(persons) == len(set(persons)) == manifest["persons"] == 48
    assert all(not p.judgments for p in pset.pairs)
    assert manifest["set_version_before_judgments"] == pset.version
    assert pset.material.reinforce_stats_batch is None
    assert {p.a.reinforce_level for p in pset.pairs} == {5}
    assert manifest["hidden_fields"] == list(HIDDEN)
    assert "position_ovr" not in manifest["disclosed_fields"] == list(DISCLOSED)
    changed, _ = select_pairs(spec(policy, seed=124), policy=policy, cards=cards)
    assert changed.version != pset.version


def test_selection_never_computes_multi_factor_scores(policy, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("쌍 선정에는 다요소 점수를 쓰지 않는다.")

    monkeypatch.setattr(scorer, "score_multi_factor", blocked)
    monkeypatch.setattr(evaluation, "score_multi_factor", blocked)
    pset, _ = select_pairs(spec(policy), policy=policy, cards=pool())
    assert len(pset.pairs) == 24


def test_chosen_pairs_follow_the_gap_and_contrast_rule(policy):
    close = [player(1, ovr=187, positions=["ST"]), player(2, ovr=189, positions=["ST"])]
    far = [
        player(3, feet=(5, 5), ovr=170, positions=["ST"]),
        player(4, feet=(3, 5), ovr=173, positions=["ST"]),
    ]
    paced = player(
        5,
        ovr=150,
        positions=["ST"],
        stats={**close[0].stats, "speed": 150, "accelartion": 140},
    )
    slow = player(6, ovr=151, positions=["ST"])
    one = spec(policy, profiles=["st"], pairs_per_profile=3)
    pset, manifest = select_pairs(one, policy=policy, cards=[*close, *far, paced, slow])
    chosen = {frozenset((p.a.card.spid, p.b.card.spid)) for p in pset.pairs}
    # 같은 발·같은 주력은 대비가 없고, OVR 3 차이는 범위 밖이다. 주력 합 차이10은 대비다.
    assert chosen == {frozenset((paced.spid, slow.spid))}
    assert manifest["profiles"]["st"]["candidates"] == 1
    assert manifest["profiles"]["st"]["shortfall"] == 2
    assert one.pairs_per_profile == 3


def test_shortfall_is_reported_without_relaxing_or_reusing_persons(policy):
    cards = pool(per_family=3, families=dict(st="ST"))
    pset, manifest = select_pairs(
        spec(policy, profiles=["st"]), policy=policy, cards=cards
    )
    assert len(pset.pairs) == 1
    assert manifest["profiles"]["st"]["shortfall"] == 2
    assert manifest["profiles"]["st"]["holdout"] == 1
    assert manifest["selection_spec"]["pairs_per_profile"] == 3
    with pytest.raises(ValueError, match="비교 쌍이 없습니다"):
        select_pairs(spec(policy, profiles=["gk"]), policy=policy, cards=cards)


def test_eligibility_exclusions_are_counted_by_reason(policy):
    cards = [
        player(1, positions=["ST"]),
        player(2, positions=["ST"], valid_until=OBSERVED + timedelta(seconds=1)),
        player(3, positions=["CB"]),
        player(4, positions=["ST"], left_foot=None),
        player(5, positions=["ST"], feet=(5, 5)),
    ]
    one = spec(policy, profiles=["st"])
    _, manifest = select_pairs(one, policy=policy, cards=cards)
    # 기한 직전까지만 유효한 카드도 기준 시각 이후는 사용하지 않는다.
    late = (OBSERVED + timedelta(seconds=2)).isoformat()
    _, expired = select_pairs(
        spec(policy, profiles=["st"], reference_time=late), policy=policy, cards=cards
    )
    assert manifest["profiles"]["st"]["excluded"]["ST"] == {
        "field:left_foot:missing": 1,
        "placement:unregistered_family": 1,
    }
    assert expired["profiles"]["st"]["excluded"]["ST"] == {
        "field:left_foot:missing": 1,
        "placement:unregistered_family": 1,
        "placement:unusable_observation": 1,
    }
    assert manifest["profiles"]["st"]["eligible"]["ST"] == 3
    assert expired["profiles"]["st"]["eligible"]["ST"] == 2


@pytest.mark.parametrize(
    "changes,message",
    [
        (dict(holdout_per_profile=3), "조정용"),
        (dict(profiles=["st", "st"]), "중복"),
        (dict(reinforce_level=7), "reinforce_level"),
        (dict(pairs_per_profile=0), "pairs_per_profile"),
    ],
)
def test_spec_contract_errors(policy, changes, message):
    with pytest.raises(ValidationError, match=message):
        spec(policy, **changes)


def test_selection_rejects_policy_profile_batch_and_duplicate_conflicts(policy):
    cards = pool(per_family=4)
    with pytest.raises(ValueError, match="정책"):
        select_pairs(spec(policy, policy_version="0" * 64), policy=policy, cards=cards)
    with pytest.raises(ValueError, match="프로필"):
        select_pairs(spec(policy, profiles=["striker"]), policy=policy, cards=cards)
    with pytest.raises(ValueError, match="배치"):
        select_pairs(
            spec(policy), policy=policy, cards=[*cards, player(900, batch_id="x")]
        )
    with pytest.raises(ValueError, match="중복"):
        select_pairs(spec(policy), policy=policy, cards=[*cards, cards[0]])


def test_form_hides_ovr_scores_and_weights_and_escapes_text(policy):
    cards = pool()
    cards[0] = player(1, feet=(5, 5), ovr=187, positions=["ST"], name="가상<b>선수")
    pset, _ = select_pairs(spec(policy), policy=policy, cards=cards)
    form = render_form(pset, cards=cards, policy=policy)
    for pair in pset.pairs:
        assert f'data-pair="{pair.pair_id}"' in form
    for hidden in ("187", "188", "189", "score_units", '"weight"', "contribution"):
        assert hidden not in form
    assert "<b>선수" not in form
    assert pset.version in form and "판단 불가" in form
    assert form.count('type="radio"') == 4 * len(pset.pairs)


def test_template_and_import_keep_the_frozen_set_and_mark_human_judgments(policy):
    cards = pool()
    pset, _ = select_pairs(spec(policy), policy=policy, cards=cards)
    template = judgment_template(pset)
    assert template["set_version"] == pset.version
    assert {e["verdict"] for e in template["entries"]} == {None}
    first, second = pset.pairs[0].pair_id, pset.pairs[1].pair_id
    template.update(judge_id="judge-01", play_style="가상 성향")
    template["entries"][0].update(
        verdict="prefer_a", rationale="가상 근거", judged_at=OBSERVED.isoformat()
    )
    template["entries"][1].update(
        verdict="undecidable", rationale="가상 근거", judged_at=OBSERVED.isoformat()
    )
    judged = import_judgments(pset, JudgmentSheet.model_validate(template))
    assert judged.version != pset.version
    rows = {p.pair_id: p for p in judged.pairs}
    [j] = rows[first].judgments
    assert (j.judge_kind, j.basis, j.judge_id, j.verdict) == (
        "human",
        "ability",
        "judge-01",
        "prefer_a",
    )
    assert rows[second].judgments[0].verdict == "undecidable"
    assert sum(not p.judgments for p in judged.pairs) == 22
    strip = judged.model_dump()
    for pair in strip["pairs"]:
        pair["judgments"] = ()
    assert strip == pset.model_dump()
    with pytest.raises(ValueError, match="판단을 넣기 전"):
        import_judgments(judged, JudgmentSheet.model_validate(template))


@pytest.mark.parametrize(
    "mutate,error,message",
    [
        (lambda t: t.update(set_version="0" * 64), ValueError, "세트 버전"),
        (
            lambda t: t["entries"].append(dict(pair_id="other-01")),
            ValueError,
            "세트에 없는",
        ),
        (
            lambda t: t["entries"].append(dict(t["entries"][0])),
            ValidationError,
            "중복",
        ),
        (
            lambda t: t["entries"][0].update(verdict="prefer_b", rationale=" "),
            ValidationError,
            "근거",
        ),
        (
            lambda t: t["entries"][0].update(verdict="prefer_b", rationale="근거"),
            ValidationError,
            "판단 시점",
        ),
        (lambda t: t.update(judge_id=""), ValidationError, "judge_id"),
    ],
)
def test_sheet_errors_are_rejected(policy, mutate, error, message):
    pset, _ = select_pairs(spec(policy), policy=policy, cards=pool())
    template = judgment_template(pset)
    template.update(judge_id="judge-01", play_style="가상 성향")
    mutate(template)
    with pytest.raises(error, match=message):
        import_judgments(pset, JudgmentSheet.model_validate(template))


def test_existing_synthetic_example_cannot_be_relabelled_as_human(policy):
    example = evaluation.PreferenceSet.model_validate_json(SET_EXAMPLE.read_text())
    sheet = JudgmentSheet.model_validate(
        {**judgment_template(example), "judge_id": "judge-01", "play_style": "가상"}
    )
    with pytest.raises(ValueError, match="판단을 넣기 전"):
        import_judgments(example, sheet)


def write_batch(folder, cards, run_id="synthetic-batch", tamper=False):
    folder.mkdir()
    text = "".join(
        json.dumps(c.model_dump(mode="json"), ensure_ascii=False) + "\n" for c in cards
    )
    (folder / "player_card.jsonl").write_text(text)
    (folder / "reinforce_stats.jsonl").write_text("")
    digest = hashlib.sha256(text.encode()).hexdigest()
    (folder / "report.json").write_text(
        json.dumps(
            dict(
                run_id=run_id,
                processed_hashes={"player_card.jsonl": "0" * 64 if tamper else digest},
            )
        )
    )


def test_cli_prepare_import_and_evaluate_end_to_end(policy, tmp_path, capsys):
    cards = pool()
    batch = tmp_path / "batch"
    write_batch(batch, cards)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(spec(policy).model_dump_json())
    out = tmp_path / "prep"
    args = ["prepare", "--material-dir", str(batch), "--policy", str(EXAMPLE)]
    args += ["--spec", str(spec_file), "--output-dir", str(out)]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    files = sorted(p.name for p in out.iterdir())
    assert files == [
        "form.html",
        "judgments.template.json",
        "manifest.json",
        "selection-spec.json",
        "set.pre.json",
    ]
    manifest = json.loads((out / "manifest.json").read_text())
    form = (out / "form.html").read_text()
    assert manifest["form_sha256"] == hashlib.sha256(form.encode()).hexdigest()
    assert set(manifest["source_hashes"]) == {"player_card.jsonl", "report.json"}
    assert (
        summary["set_version_before_judgments"]
        == manifest["set_version_before_judgments"]
    )
    with pytest.raises(SystemExit):
        main(args)

    pre = evaluation.PreferenceSet.model_validate_json(
        (out / "set.pre.json").read_text()
    )
    sheet = json.loads((out / "judgments.template.json").read_text())
    sheet.update(judge_id="judge-01", play_style="가상 성향")
    for entry in sheet["entries"]:
        entry.update(
            verdict="prefer_a", rationale="가상 근거", judged_at=OBSERVED.isoformat()
        )
    (tmp_path / "judgments.json").write_text(json.dumps(sheet, ensure_ascii=False))
    judged_file = tmp_path / "set.judged.json"
    assert (
        main(
            [
                "import",
                "--set",
                str(out / "set.pre.json"),
                "--judgments",
                str(tmp_path / "judgments.json"),
                "--output",
                str(judged_file),
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["set_version_before_judgments"] == pre.version
    assert imported["judgments"] == 24 and imported["pending_pairs"] == 0

    report_file = tmp_path / "report.json"
    assert (
        evaluation.main(
            [
                "--set",
                str(judged_file),
                "--policy",
                str(EXAMPLE),
                "--material-dir",
                str(batch),
                "--output",
                str(report_file),
            ]
        )
        == 0
    )
    report = json.loads(report_file.read_text())
    assert report["quality"]["status"] == "measured"
    assert report["quality"]["passed"] is None
    assert report["splits"]["holdout"]["human"]["comparable"] == 8
    assert report["splits"]["tuning"]["human"]["comparable"] == 16


def test_cli_rejects_tampered_or_mismatched_batches(policy, tmp_path):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(spec(policy).model_dump_json())
    for name, changes in (("tampered", dict(tamper=True)), ("other", dict(run_id="x"))):
        batch = tmp_path / name
        write_batch(batch, pool(), **changes)
        with pytest.raises(SystemExit):
            main(
                [
                    "prepare",
                    "--material-dir",
                    str(batch),
                    "--policy",
                    str(EXAMPLE),
                    "--spec",
                    str(spec_file),
                    "--output-dir",
                    str(tmp_path / f"{name}-out"),
                ]
            )
        assert not (tmp_path / f"{name}-out").exists()
