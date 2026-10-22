"""MF-2 선호 비교 평가. 판단 자료와 scorer 출력을 분리하며 네트워크·DB를 쓰지 않는다.

정답은 평가 세트의 판단(사람 또는 명시 가상 규칙)이다. 현재 scorer 출력으로 정답을
만들지 않고, 판단이 없거나 보류 평가용 사람 판단이 없으면 품질을 미평가로 둔다.
"""

import argparse
import json
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from FRS.core.squad_recommend.multi_factor import _units, score_multi_factor
from FRS.core.squad_recommend.multi_factor_policy import (
    Frozen,
    Identifier,
    ScoringPolicy,
    content_hash,
)
from FRS.core.squad_recommend.performance import (
    PerformanceQuery,
    performance_spec,
    score_performance,
)
from FRS.schema.models import (
    CardId,
    PlayerCard,
    Position,
    ReinforceStats,
    SourceKind,
    Text,
)
from FRS.schema.scope import FORMATION_SLOTS

SET_SCHEMA = "mf-preference-set.v1"
REPORT_SCHEMA = "mf-preference-report.v1"
BASELINE = "ovr"
BASE_LEVEL = performance_spec()["base_reinforce_level"]
_SLOTS = frozenset(slot for slots in FORMATION_SLOTS.values() for slot in slots)
_SPLITS = ("tuning", "holdout")
_KINDS = ("human", "synthetic")
Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Verdict = Literal["prefer_a", "prefer_b", "equal", "undecidable"]

EVALUATOR_SPEC = dict(
    schema=REPORT_SCHEMA,
    set_schema=SET_SCHEMA,
    algorithm_version=1,
    unit="one judgment; several judges of a pair are separate rows",
    comparable="ability basis, decided, and every method scored both cards",
    agreement="agree / decisive comparable; method ties are not agreement",
    equal="equal judgments are reported apart; exact score equality is a tie",
    baseline="raw position OVR from score_performance",
    multi_factor="score_units from score_multi_factor with the pinned version",
    ablation="tuning only; weighted mean of exact components without one item",
    sensitivity="tuning only; one active weight += step, negatives skipped",
    rounding="ablation and sensitivity use the scorer final half up units",
    splits="tuning and holdout are never pooled; person groups are disjoint",
    judges="human and synthetic judgments are never pooled",
    quality="no pass criteria; measured only with holdout human judgments",
)


class CardKey(Frozen):
    spid: CardId
    batch_id: Text
    version: Text

    @property
    def person_id(self):
        return self.spid % 1_000_000


class ReinforcementKey(Frozen):
    batch_id: Text
    version: Text


class PairCard(Frozen):
    card: CardKey
    reinforce_level: Annotated[int, Field(strict=True, ge=5, le=11)]
    reinforcement: ReinforcementKey | None = None

    @model_validator(mode="after")
    def linked(self):
        if (self.reinforce_level != BASE_LEVEL) != (self.reinforcement is not None):
            raise ValueError("기준 강화 밖의 카드에만 강화 관측 키를 명시합니다.")
        return self


class Judgment(Frozen):
    judge_id: Identifier
    judge_kind: Literal["human", "synthetic"]
    verdict: Verdict
    basis: Literal["ability", "budget"]
    rationale: Text
    play_style: Text
    judged_at: AwareDatetime
    game_version: Text | None = None


class PreferencePair(Frozen):
    pair_id: Identifier
    split: Literal["tuning", "holdout"]
    slot: Position
    a: PairCard
    b: PairCard
    judgments: Annotated[tuple[Judgment, ...], Field(max_length=100)] = ()

    @field_validator("slot")
    @classmethod
    def supported_slot(cls, value):
        if value not in _SLOTS:
            raise ValueError("MVP 포메이션의 지원 슬롯이 아닙니다.")
        return value

    @model_validator(mode="after")
    def distinct(self):
        if self.a == self.b:
            raise ValueError("같은 카드·강화를 비교할 수 없습니다.")
        judges = [j.judge_id for j in self.judgments]
        if len(judges) != len(set(judges)):
            raise ValueError("한 쌍에 같은 판단자의 판단이 중복입니다.")
        return self

    def persons(self):
        return {self.a.card.person_id, self.b.card.person_id}


class MaterialBinding(Frozen):
    source: SourceKind
    player_card_batch: Text
    reinforce_stats_batch: Text | None = None
    reference_time: AwareDatetime


class PolicyBinding(Frozen):
    policy_id: Identifier
    version: Hash


class PreferenceSet(Frozen):
    """비교 쌍·판단·분할을 호출자가 고정한다. 분할 비율·시드·배치 기본값은 없다."""

    schema_version: Literal["mf-preference-set.v1"]
    set_id: Identifier
    material: MaterialBinding
    policies: Annotated[tuple[PolicyBinding, ...], Field(min_length=1, max_length=8)]
    pairs: Annotated[tuple[PreferencePair, ...], Field(min_length=1, max_length=10000)]

    @model_validator(mode="after")
    def consistent(self):
        ids = [p.policy_id for p in self.policies]
        if len(ids) != len(set(ids)) or BASELINE in ids:
            raise ValueError("정책 ID가 중복이거나 기준 방식 이름과 같습니다.")
        if len({p.pair_id for p in self.pairs}) != len(self.pairs):
            raise ValueError("비교 쌍 ID가 중복입니다.")
        seen = set()
        for pair in self.pairs:
            content = (pair.slot, frozenset((pair.a, pair.b)))
            if content in seen:
                raise ValueError("같은 슬롯의 같은 두 카드 비교가 중복입니다.")
            seen.add(content)
            for side in (pair.a, pair.b):
                if side.card.batch_id != self.material.player_card_batch:
                    raise ValueError("카드 관측이 명시한 배치와 다릅니다.")
                if side.reinforcement is not None and (
                    side.reinforcement.batch_id != self.material.reinforce_stats_batch
                ):
                    raise ValueError("강화 관측이 명시한 배치와 다릅니다.")
        groups = {
            split: set().union(*(p.persons() for p in self.pairs if p.split == split))
            for split in _SPLITS
        }
        if groups["tuning"] & groups["holdout"]:
            raise ValueError("같은 인물이 조정용과 보류 평가용에 함께 있습니다.")
        return self

    @property
    def version(self):
        spec = self.model_dump(mode="json")
        spec["policies"].sort(key=lambda p: p["policy_id"])
        spec["pairs"].sort(key=lambda p: p["pair_id"])
        for pair in spec["pairs"]:
            pair["judgments"].sort(key=lambda j: j["judge_id"])
        return content_hash(spec)


class PreferenceMaterial:
    """명시 관측의 정확한 키 조회. 같은 키의 중복을 최신값으로 해소하지 않는다."""

    def __init__(self, cards=(), reinforcements=()):
        self._cards, self._reinforcements = {}, {}
        for card in cards:
            card = PlayerCard.model_validate(card.model_dump())
            key = (card.spid, card.batch_id, card.version)
            if key in self._cards:
                raise ValueError("카드 관측 키가 중복입니다.")
            self._cards[key] = card
        for row in reinforcements:
            row = ReinforceStats.model_validate(row.model_dump())
            key = (row.spid, row.reinforce_level, row.batch_id, row.version)
            if key in self._reinforcements:
                raise ValueError("강화 관측 키가 중복입니다.")
            self._reinforcements[key] = row

    def resolve(self, side):
        key = side.card
        card = self._cards.get((key.spid, key.batch_id, key.version))
        if side.reinforcement is None:
            return card, None
        ref = side.reinforcement
        return card, self._reinforcements.get(
            (key.spid, side.reinforce_level, ref.batch_id, ref.version)
        )


def _ratio(numerator, denominator):
    if not denominator:
        return None
    return dict(numerator=numerator, denominator=denominator)


def _fraction(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def _outcome(a, b):
    return "a" if a > b else "b" if b > a else "tie"


def _weighted_units(result, weights):
    total = sum(weights.values())
    if not total:
        return None
    score = sum(
        c.value.fraction() * weights[c.name] for c in result.components
    ) / Fraction(total)
    return _units(score)


def _tally(rows):
    tally = Counter()
    for verdict, outcome in rows:
        if verdict == "equal":
            tally["equal"] += 1
            tally["equal_tie" if outcome == "tie" else "equal_decisive"] += 1
            continue
        tally["decisive"] += 1
        if outcome == verdict.removeprefix("prefer_"):
            tally["agree"] += 1
        else:
            tally["method_tie" if outcome == "tie" else "opposite"] += 1
    keys = ("decisive", "agree", "opposite", "method_tie", "equal", "equal_tie")
    return dict(
        **{key: tally[key] for key in (*keys, "equal_decisive")},
        agreement=_ratio(tally["agree"], tally["decisive"]),
        equal_agreement=_ratio(tally["equal_tie"], tally["equal"]),
    )


def _case_kind(verdict, outcome):
    if verdict == "equal":
        return None if outcome == "tie" else "equal_decisive"
    if outcome == verdict.removeprefix("prefer_"):
        return None
    return "method_tie" if outcome == "tie" else "opposite"


def _audit(pset, policies, material, scored):
    """정책이 요구하는 필드를 scorer와 별도로 모두 확인한다. 첫 오류에서 멈추지 않는다."""
    issues, presence, references = set(), Counter(), set()
    for pair in pset.pairs:
        for side_name, side in (("a", pair.a), ("b", pair.b)):
            card, reinforcement = material.resolve(side)
            linked = "" if side.reinforcement is None else side.reinforcement.version
            ref = (side.card.spid, side.card.version, side.reinforce_level, linked)
            if ref in references:
                continue
            references.add(ref)
            if card is None:
                issues.add((*ref, "*", "*", "card", "missing_observation"))
                continue
            for field in ("height", "weight", "physical", "left_foot", "right_foot"):
                if getattr(card, field) is None:
                    presence[field] += 1
            needs_delta = side.reinforce_level != card.base_reinforce_level
            unrestorable = needs_delta and reinforcement is None
            if unrestorable:
                issues.add((*ref, "*", "*", "reinforcement", "missing_observation"))
            for policy in policies:
                profile_id = scored[policy.policy_id][pair.slot]
                role = next(p for p in policy.profiles if p.name == profile_id)
                weights = {w.name: w.weight for w in role.weights}

                def issue(field, kind):
                    issues.add((*ref, policy.policy_id, profile_id, field, kind))

                for group in policy.groups:
                    if not weights[group.name] or unrestorable:
                        continue
                    for stat in group.stats:
                        if not stat.weight:
                            continue
                        if stat.name not in card.stats:
                            issue(stat.name, "missing")
                            continue
                        raw = card.stats[stat.name]
                        if needs_delta:
                            if stat.name not in reinforcement.stats_delta:
                                issue(stat.name, "missing_delta")
                                continue
                            raw += reinforcement.stats_delta[stat.name]
                        if not 0 <= raw <= stat.upper:
                            issue(stat.name, "out_of_range")
                if weights["weak_foot"]:
                    for field in ("left_foot", "right_foot"):
                        foot = getattr(card, field)
                        if foot is None:
                            issue(field, "missing")
                        elif not 1 <= foot <= 5:
                            issue(field, "out_of_range")
                if policy.mode == "body-aware" and weights["body"]:
                    body = role.body
                    for field, weight, curve in (
                        ("height", body.height_weight, body.height_curve),
                        ("weight", body.weight_weight, body.weight_curve),
                    ):
                        value = getattr(card, field)
                        if not weight:
                            continue
                        if value is None:
                            issue(field, "missing")
                        elif not (
                            curve.points[0].value <= value <= curve.points[-1].value
                        ):
                            issue(field, "out_of_range")
                    if body.shape_weight:
                        if card.physical is None:
                            issue("physical", "missing")
                        elif card.physical not in {s.name for s in body.shapes}:
                            issue("physical", "unknown_shape")
    counts = Counter((p, f, k) for *_, p, _profile, f, k in issues)
    return dict(
        references=len(references),
        missing_optional_fields=dict(sorted(presence.items())),
        counts=[
            dict(policy_id=p, field=f, issue=k, references=n)
            for (p, f, k), n in sorted(counts.items())
        ],
        issues=[
            dict(
                spid=spid,
                version=version,
                reinforce_level=level,
                reinforcement_version=linked or None,
                policy_id=p,
                profile_id=profile,
                field=f,
                issue=k,
            )
            for spid, version, level, linked, p, profile, f, k in sorted(issues)
        ],
    )


def evaluate_preferences(
    pset: PreferenceSet,
    *,
    policies,
    cards=(),
    reinforcements=(),
    sensitivity_steps=(),
):
    """명시 세트·정책·관측으로 재현 가능한 보고서를 만든다. 계약 오류는 거부한다."""
    if not isinstance(pset, PreferenceSet):
        raise TypeError("PreferenceSet으로 평가 세트를 명시해야 합니다.")
    pset = PreferenceSet.model_validate(pset.model_dump())
    provided = {}
    for policy in policies:
        if not isinstance(policy, ScoringPolicy):
            raise TypeError("ScoringPolicy로 명시 정책을 제공해야 합니다.")
        policy = ScoringPolicy.model_validate(policy.model_dump())
        if policy.policy_id in provided:
            raise ValueError("같은 정책 ID를 중복 제공했습니다.")
        provided[policy.policy_id] = policy
    bindings = {b.policy_id: b.version for b in pset.policies}
    if set(provided) != set(bindings):
        raise ValueError("평가 세트에 고정한 정책과 제공한 정책이 다릅니다.")
    for policy_id, version in bindings.items():
        if provided[policy_id].version != version:
            raise ValueError("평가 세트에 고정한 정책 버전과 내용이 다릅니다.")
    policies = [provided[k] for k in sorted(provided)]
    steps = tuple(sorted(set(sensitivity_steps)))
    if len(steps) != len(tuple(sensitivity_steps)) or any(
        type(s) is not int or not s for s in steps
    ):
        raise ValueError("민감도 단계는 중복 없는 0이 아닌 정수여야 합니다.")
    material = PreferenceMaterial(cards, reinforcements)
    binding = pset.material
    profiles = {p.policy_id: {s.slot: s.profile_id for s in p.slots} for p in policies}
    methods = (BASELINE, *profiles)

    used, side_scores = {}, {}
    for pair in pset.pairs:
        for name, side in (("a", pair.a), ("b", pair.b)):
            card, reinforcement = material.resolve(side)
            for row in (card, reinforcement):
                if row is None:
                    continue
                if row.source != binding.source:
                    raise ValueError("관측 출처가 평가 세트에 명시한 출처와 다릅니다.")
                level = getattr(row, "reinforce_level", 0)
                key = (type(row).__name__, row.spid, level, row.batch_id, row.version)
                used[key] = row.model_dump(mode="json")
            query = PerformanceQuery(
                spid=side.card.spid,
                slot=pair.slot,
                reinforce_level=side.reinforce_level,
                when=binding.reference_time,
            )
            ovr = score_performance(query, card=card, reinforcement=reinforcement)
            scores = {BASELINE: ovr}
            for policy in policies:
                scores[policy.policy_id] = score_multi_factor(
                    query,
                    policy=policy,
                    expected_policy_version=policy.version,
                    profile_id=profiles[policy.policy_id][pair.slot],
                    card=card,
                    reinforcement=reinforcement,
                )
            side_scores[(pair.pair_id, name)] = scores

    def value(method, result):
        return result.raw_ovr if method == BASELINE else result.score_units

    splits = {}
    for split in _SPLITS:
        pairs = [p for p in pset.pairs if p.split == split]
        section = dict(
            pairs=len(pairs),
            pending_pairs=sum(not p.judgments for p in pairs),
            persons=len(set().union(*(p.persons() for p in pairs))),
        )
        for kind in _KINDS:
            rows = [
                (pair, j)
                for pair in pairs
                for j in pair.judgments
                if j.judge_kind == kind
            ]
            section[kind] = _evaluate_rows(
                rows,
                split=split,
                methods=methods,
                profiles=profiles,
                side_scores=side_scores,
                value=value,
                steps=steps,
            )
        splits[split] = section
    holdout = splits["holdout"]["human"]
    report = dict(
        schema_version=REPORT_SCHEMA,
        evaluator_version=content_hash(EVALUATOR_SPEC),
        set_id=pset.set_id,
        set_version=pset.version,
        material=dict(
            source=binding.source.value,
            player_card_batch=binding.player_card_batch,
            reinforce_stats_batch=binding.reinforce_stats_batch,
            reference_time=binding.reference_time.isoformat(),
            observations=len(used),
            observations_version=content_hash([used[k] for k in sorted(used)]),
        ),
        baseline=dict(
            method="position_ovr", scorer_version=performance_spec()["scorer_version"]
        ),
        policies=[
            dict(policy_id=p.policy_id, version=p.version, mode=p.mode)
            for p in policies
        ],
        sensitivity_steps=list(steps),
        splits=splits,
        audit=_audit(pset, policies, material, profiles),
        quality=dict(
            status="measured" if holdout["comparable"] else "not_evaluated",
            basis="holdout split human judgments only",
            criteria=None,
            passed=None,
            holdout_human_comparable=holdout["comparable"],
            synthetic_judgments=sum(
                splits[s]["synthetic"]["judgments"] for s in _SPLITS
            ),
        ),
    )
    report["report_version"] = content_hash(report)
    return report


def _evaluate_rows(rows, *, split, methods, profiles, side_scores, value, steps):
    section = dict(
        judgments=len(rows),
        undecidable=0,
        unsupported_basis=0,
        data_excluded=0,
        exclusions={m: Counter() for m in methods},
        comparable=0,
    )
    comparable = []
    for pair, judgment in rows:
        if judgment.verdict == "undecidable":
            section["undecidable"] += 1
            continue
        if judgment.basis != "ability":
            section["unsupported_basis"] += 1
            continue
        scores = {name: side_scores[(pair.pair_id, name)] for name in ("a", "b")}
        excluded = False
        for method in methods:
            for name in ("a", "b"):
                reason = scores[name][method].exclusion
                if reason is not None:
                    section["exclusions"][method][f"{name}:{reason}"] += 1
                    excluded = True
        if excluded:
            section["data_excluded"] += 1
            continue
        comparable.append((pair, judgment, scores))
    section["comparable"] = len(comparable)
    section["exclusions"] = {
        m: dict(sorted(c.items())) for m, c in section["exclusions"].items()
    }

    outcomes = {
        method: [
            _outcome(value(method, s["a"][method]), value(method, s["b"][method]))
            for _, _, s in comparable
        ]
        for method in methods
    }
    verdicts = [j.verdict for _, j, _ in comparable]
    section["methods"] = {
        m: _tally(zip(verdicts, outcomes[m], strict=True)) for m in methods
    }
    by_slot = defaultdict(list)
    for index, (pair, _, _) in enumerate(comparable):
        by_slot[pair.slot].append(index)
    section["by_slot"] = {
        slot.value: {
            m: _tally((verdicts[i], outcomes[m][i]) for i in indices) for m in methods
        }
        for slot, indices in sorted(by_slot.items())
    }
    section["by_profile"] = {}
    for policy_id, slots in profiles.items():
        groups = defaultdict(list)
        for index, (pair, _, _) in enumerate(comparable):
            groups[slots[pair.slot]].append(index)
        section["by_profile"][policy_id] = {
            profile: _tally((verdicts[i], outcomes[policy_id][i]) for i in indices)
            for profile, indices in sorted(groups.items())
        }

    cases = []
    for index, (pair, judgment, scores) in enumerate(comparable):
        for method in methods:
            kind = _case_kind(judgment.verdict, outcomes[method][index])
            if kind is None:
                continue
            a, b = scores["a"][method], scores["b"][method]
            case = dict(
                method=method,
                pair_id=pair.pair_id,
                judge_id=judgment.judge_id,
                slot=pair.slot.value,
                verdict=judgment.verdict,
                outcome=outcomes[method][index],
                kind=kind,
                a_value=value(method, a),
                b_value=value(method, b),
            )
            if method != BASELINE:
                contributions = {
                    c.name: c.contribution.fraction() for c in b.components
                }
                case["profile_id"] = a.profile_id
                case["contribution_delta"] = {
                    c.name: _fraction(c.contribution.fraction() - contributions[c.name])
                    for c in a.components
                }
            cases.append(case)
    section["cases"] = sorted(
        cases, key=lambda c: (c["method"], c["pair_id"], c["judge_id"])
    )

    if split != "tuning":
        section["ablation"] = section["sensitivity"] = None
        return section
    section["ablation"] = {}
    section["sensitivity"] = []
    for policy_id in profiles:
        results = [(s["a"][policy_id], s["b"][policy_id]) for _, _, s in comparable]
        names = sorted({c.name for a, _ in results for c in a.components})
        section["ablation"][policy_id] = {
            name: _variant(
                verdicts,
                outcomes[policy_id],
                results,
                lambda r, name=name: {
                    c.name: 0 if c.name == name else c.weight for c in r.components
                },
            )
            for name in names
        }
        for profile in sorted({a.profile_id for a, _ in results}):
            indices = [i for i, (a, _) in enumerate(results) if a.profile_id == profile]
            weights = {c.name: c.weight for c in results[indices[0]][0].components}
            for name in sorted(weights):
                for step in steps:
                    entry = dict(
                        policy_id=policy_id,
                        profile_id=profile,
                        component=name,
                        step=step,
                        base_weight=weights[name],
                    )
                    if weights[name] + step < 0:
                        section["sensitivity"].append({**entry, "skipped": "negative"})
                        continue
                    variant = _variant(
                        [verdicts[i] for i in indices],
                        [outcomes[policy_id][i] for i in indices],
                        [results[i] for i in indices],
                        lambda r, name=name, step=step: {
                            c.name: c.weight + (step if c.name == name else 0)
                            for c in r.components
                        },
                    )
                    section["sensitivity"].append({**entry, **variant})
    return section


def _variant(verdicts, base_outcomes, results, weights_of):
    rows, changed, not_applicable = [], 0, 0
    for verdict, base, (a, b) in zip(verdicts, base_outcomes, results, strict=True):
        units = _weighted_units(a, weights_of(a)), _weighted_units(b, weights_of(b))
        if None in units:
            not_applicable += 1
            continue
        outcome = _outcome(*units)
        changed += outcome != base
        rows.append((verdict, outcome))
    return dict(**_tally(rows), changed=changed, not_applicable=not_applicable)


def _jsonl(path, model):
    return [
        model.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="MF-2 명시 선호 세트 오프라인 평가 (네트워크·DB·모델 호출 없음)"
    )
    parser.add_argument("--set", required=True, type=Path)
    parser.add_argument("--policy", required=True, action="append", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--material", type=Path, help="player_card/reinforce_stats JSON"
    )
    source.add_argument(
        "--material-dir", type=Path, help="player_card.jsonl·reinforce_stats.jsonl"
    )
    parser.add_argument("--sensitivity", action="append", type=int, default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("기존 보고서를 덮어쓰지 않습니다.")
    pset = PreferenceSet.model_validate_json(args.set.read_text(encoding="utf-8"))
    policies = [
        ScoringPolicy.model_validate_json(p.read_text(encoding="utf-8"))
        for p in args.policy
    ]
    if args.material is not None:
        raw = json.loads(args.material.read_text(encoding="utf-8"))
        if set(raw) != {"player_card", "reinforce_stats"}:
            parser.error("자료 파일에는 player_card·reinforce_stats만 둡니다.")
        cards = [PlayerCard.model_validate(c) for c in raw["player_card"]]
        rows = [ReinforceStats.model_validate(r) for r in raw["reinforce_stats"]]
    else:
        cards = _jsonl(args.material_dir / "player_card.jsonl", PlayerCard)
        rows = _jsonl(args.material_dir / "reinforce_stats.jsonl", ReinforceStats)
    report = evaluate_preferences(
        pset,
        policies=policies,
        cards=cards,
        reinforcements=rows,
        sensitivity_steps=args.sensitivity,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(
        json.dumps(
            dict(
                report_version=report["report_version"],
                quality=report["quality"]["status"],
                holdout_human_comparable=report["quality"]["holdout_human_comparable"],
                synthetic_judgments=report["quality"]["synthetic_judgments"],
            ),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
