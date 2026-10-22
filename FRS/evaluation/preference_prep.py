"""MF-2 실제 판단 수집 준비. 규칙 기반 쌍 추출·블라인드 판단 양식·판단 가져오기.

쌍 선정에는 다요소 점수를 쓰지 않는다. 판단 양식에는 OVR·점수·방식의 선택·가격을
보여주지 않는다. 사람 판단을 대신 만들지 않으며 네트워크·DB·모델을 쓰지 않는다.
"""

import argparse
import hashlib
import html
import json
import random
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from FRS.core.squad_recommend.multi_factor_policy import (
    Frozen,
    Identifier,
    ScoringPolicy,
    content_hash,
)
from FRS.core.squad_recommend.performance import PerformanceQuery, score_performance
from FRS.core.squad_recommend.placement import PlacementQuery, placement
from FRS.evaluation.multi_factor import (
    EVALUATOR_SPEC,
    PreferenceSet,
    Verdict,
    field_issues,
)
from FRS.schema.models import PlayerCard, SourceKind, Text

SELECTION_SCHEMA = "mf-pair-selection.v1"
SHEET_SCHEMA = "mf-judgment-sheet.v1"
MANIFEST_SCHEMA = "mf-preference-prep.v1"
Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=100)]

DISCLOSED = (
    "name",
    "season_id",
    "spid",
    "reinforce_level",
    "positions",
    "height",
    "weight",
    "physical",
    "left_foot",
    "right_foot",
    "dribble_skill",
    "skills",
    "stats",
    "batch_id",
    "observed_at",
    "reference_time",
)
HIDDEN = (
    "position_ovr",
    "multi_factor_score",
    "method_choice",
    "price",
    "salary",
    "used",
    "team_color",
    "policy_weights",
)
SELECTION_RULE = dict(
    version=1,
    eligibility=(
        "usable at reference time, base placement at the slot, OVR present, "
        "no policy field issue"
    ),
    candidates=(
        "same slot, different person, |OVR gap| <= max_ovr_gap, and weak foot grade "
        "differs or |speed+accelartion gap| >= min_pace_gap"
    ),
    dedupe="one unordered card pair per profile; first slot in sorted order",
    sampling=(
        "Random(seed); profiles in spec order; sorted candidates shuffled; greedy "
        "with each person used at most once in the whole set"
    ),
    order="per chosen pair Random.random() < 0.5 swaps A and B",
    split="per profile Random.sample of holdout_per_profile pairs; rest tuning",
    shortfall="report missing pairs; never relax the rule or reuse persons",
    scores="multi-factor scores are never computed for selection",
)
_GROUP_LABELS = dict(
    pace="주력",
    shooting="슈팅",
    passing="패스",
    control="볼 다루기",
    defending="수비",
    physical="경합·활동",
    gk="GK",
)
_VERDICT_LABELS = (
    ("prefer_a", "A 선호"),
    ("prefer_b", "B 선호"),
    ("equal", "동등"),
    ("undecidable", "판단 불가"),
)


class SelectionSpec(Frozen):
    """쌍 추출 규칙의 모든 수치를 호출자가 명시한다. 기본값은 없다."""

    schema_version: Literal["mf-pair-selection.v1"]
    set_id: Identifier
    source: SourceKind
    player_card_batch: Text
    reference_time: AwareDatetime
    policy_id: Identifier
    policy_version: Hash
    profiles: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=20)]
    pairs_per_profile: Annotated[int, Field(strict=True, ge=1, le=100)]
    holdout_per_profile: Count
    seed: Annotated[int, Field(strict=True)]
    reinforce_level: Literal[5]
    max_ovr_gap: Annotated[int, Field(strict=True, ge=0, le=200)]
    min_pace_gap: Annotated[int, Field(strict=True, ge=1, le=400)]

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.profiles)) != len(self.profiles):
            raise ValueError("프로필이 중복입니다.")
        if self.holdout_per_profile >= self.pairs_per_profile:
            raise ValueError("프로필마다 조정용 쌍이 하나 이상 남아야 합니다.")
        return self

    @property
    def version(self):
        return content_hash(self.model_dump(mode="json"))


class SheetEntry(Frozen):
    pair_id: Identifier
    verdict: Verdict | None = None
    rationale: str = ""
    judged_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def complete(self):
        if self.verdict is not None and (
            not self.rationale.strip() or self.judged_at is None
        ):
            raise ValueError("판단에는 근거와 판단 시점이 필요합니다.")
        return self


class JudgmentSheet(Frozen):
    """판단 양식에서 저장한 파일. 판단 결과만 담고 쌍 내용은 바꿀 수 없다."""

    schema_version: Literal["mf-judgment-sheet.v1"]
    set_id: Identifier
    set_version: Hash
    judge_id: Identifier
    play_style: Text
    entries: Annotated[tuple[SheetEntry, ...], Field(max_length=10000)]

    @model_validator(mode="after")
    def unique(self):
        ids = [e.pair_id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("같은 쌍의 판단이 중복입니다.")
        return self


def _eligibility(card, slot, profile_id, *, spec, policy):
    query = dict(
        spid=card.spid,
        slot=slot,
        reinforce_level=spec.reinforce_level,
        when=spec.reference_time,
    )
    allowed = placement(PlacementQuery(**query), card=card)
    if not allowed.allowed:
        return f"placement:{allowed.exclusion}", None
    ovr = score_performance(PerformanceQuery(**query), card=card)
    if ovr.exclusion is not None:
        return f"ovr:{ovr.exclusion}", None
    issues = field_issues(card, None, policy, profile_id, level=spec.reinforce_level)
    if issues:
        return f"field:{issues[0][0]}:{issues[0][1]}", None
    return None, ovr.raw_ovr


def _candidates(eligible, spec):
    rows = sorted(eligible, key=lambda r: (r[1], r[0].spid))
    found = []
    for i, (a, ovr_a) in enumerate(rows):
        for b, ovr_b in rows[i + 1 :]:
            if ovr_b - ovr_a > spec.max_ovr_gap:
                break
            if a.person_id == b.person_id:
                continue
            pace = (a.stats["speed"] + a.stats["accelartion"]) - (
                b.stats["speed"] + b.stats["accelartion"]
            )
            weak = min(a.left_foot, a.right_foot) != min(b.left_foot, b.right_foot)
            if weak or abs(pace) >= spec.min_pace_gap:
                found.append(tuple(sorted((a.spid, b.spid))))
    return found


def select_pairs(spec, *, policy, cards):
    """판단 전 세트와 선정 기록을 만든다. 같은 입력·시드는 같은 결과를 낸다."""
    spec = SelectionSpec.model_validate(spec.model_dump())
    policy = ScoringPolicy.model_validate(policy.model_dump())
    if (policy.policy_id, policy.version) != (spec.policy_id, spec.policy_version):
        raise ValueError("선정 규칙에 고정한 정책과 제공한 정책이 다릅니다.")
    roles = {p.name for p in policy.profiles}
    if not set(spec.profiles) <= roles:
        raise ValueError("정책에 없는 프로필입니다.")
    by_spid = {}
    for card in cards:
        card = PlayerCard.model_validate(card.model_dump())
        if card.batch_id != spec.player_card_batch or card.source != spec.source:
            raise ValueError("카드 관측이 명시한 배치·출처와 다릅니다.")
        if card.spid in by_spid:
            raise ValueError("카드 관측 키가 중복입니다.")
        by_spid[card.spid] = card
    ordered = [by_spid[k] for k in sorted(by_spid)]
    rng = random.Random(spec.seed)
    used_persons, pairs, profiles = set(), [], {}
    for profile_id in spec.profiles:
        slots = sorted(s.slot for s in policy.slots if s.profile_id == profile_id)
        seen, candidates, report = set(), [], dict(slots=[s.value for s in slots])
        report["eligible"], report["excluded"] = {}, {}
        for slot in slots:
            eligible, excluded = [], Counter()
            for card in ordered:
                reason, ovr = _eligibility(
                    card, slot, profile_id, spec=spec, policy=policy
                )
                if reason is None:
                    eligible.append((card, ovr))
                else:
                    excluded[reason] += 1
            report["eligible"][slot.value] = len(eligible)
            report["excluded"][slot.value] = dict(sorted(excluded.items()))
            for key in _candidates(eligible, spec):
                if key not in seen:
                    seen.add(key)
                    candidates.append((slot, *key))
        candidates.sort()
        report["candidates"] = len(candidates)
        rng.shuffle(candidates)
        chosen = []
        for slot, first, second in candidates:
            if len(chosen) == spec.pairs_per_profile:
                break
            persons = {by_spid[first].person_id, by_spid[second].person_id}
            if persons & used_persons:
                continue
            used_persons |= persons
            chosen.append([slot, first, second])
        for row in chosen:
            if rng.random() < 0.5:
                row[1], row[2] = row[2], row[1]
        holdout = set(
            rng.sample(range(len(chosen)), min(spec.holdout_per_profile, len(chosen)))
        )
        for index, (slot, first, second) in enumerate(chosen):
            pairs.append(
                dict(
                    pair_id=f"{profile_id}-{index + 1:02d}",
                    split="holdout" if index in holdout else "tuning",
                    slot=slot.value,
                    a=_side(by_spid[first], spec),
                    b=_side(by_spid[second], spec),
                    judgments=[],
                )
            )
        report["chosen"] = len(chosen)
        report["holdout"] = len(holdout)
        report["shortfall"] = spec.pairs_per_profile - len(chosen)
        profiles[profile_id] = report
    if not pairs:
        raise ValueError("규칙을 만족하는 비교 쌍이 없습니다.")
    pset = PreferenceSet.model_validate(
        dict(
            schema_version="mf-preference-set.v1",
            set_id=spec.set_id,
            material=dict(
                source=spec.source.value,
                player_card_batch=spec.player_card_batch,
                reinforce_stats_batch=None,
                reference_time=spec.reference_time.isoformat(),
            ),
            policies=[dict(policy_id=policy.policy_id, version=policy.version)],
            pairs=pairs,
        )
    )
    manifest = dict(
        schema_version=MANIFEST_SCHEMA,
        selection_spec=spec.model_dump(mode="json"),
        selection_spec_version=spec.version,
        selection_rule=SELECTION_RULE,
        selection_rule_version=content_hash(SELECTION_RULE),
        policy=dict(policy_id=policy.policy_id, version=policy.version),
        evaluator_version=content_hash(EVALUATOR_SPEC),
        set_id=pset.set_id,
        set_version_before_judgments=pset.version,
        pairs=len(pset.pairs),
        persons=len(used_persons),
        cards_read=len(ordered),
        profiles=profiles,
        shortfall=sum(r["shortfall"] for r in profiles.values()),
        disclosed_fields=list(DISCLOSED),
        hidden_fields=list(HIDDEN),
    )
    return pset, manifest


def _side(card, spec):
    return dict(
        card=dict(spid=card.spid, batch_id=card.batch_id, version=card.version),
        reinforce_level=spec.reinforce_level,
    )


def judgment_template(pset):
    return dict(
        schema_version=SHEET_SCHEMA,
        set_id=pset.set_id,
        set_version=pset.version,
        judge_id="",
        play_style="",
        entries=[
            dict(pair_id=p.pair_id, verdict=None, rationale="", judged_at=None)
            for p in pset.pairs
        ],
    )


def import_judgments(pset, sheet):
    """판단 전 세트에 사람 판단을 붙인다. 쌍·분할·관측·정책은 바꾸지 않는다."""
    pset = PreferenceSet.model_validate(pset.model_dump())
    sheet = JudgmentSheet.model_validate(sheet.model_dump())
    if any(p.judgments for p in pset.pairs):
        raise ValueError("판단을 넣기 전의 세트가 아닙니다.")
    if (sheet.set_id, sheet.set_version) != (pset.set_id, pset.version):
        raise ValueError("판단 파일이 고정한 세트 버전과 다릅니다.")
    entries = {e.pair_id: e for e in sheet.entries}
    if not set(entries) <= {p.pair_id for p in pset.pairs}:
        raise ValueError("세트에 없는 쌍의 판단입니다.")
    payload = pset.model_dump(mode="json")
    for pair in payload["pairs"]:
        entry = entries.get(pair["pair_id"])
        if entry is None or entry.verdict is None:
            continue
        pair["judgments"] = [
            dict(
                judge_id=sheet.judge_id,
                judge_kind="human",
                verdict=entry.verdict,
                basis="ability",
                rationale=entry.rationale.strip(),
                play_style=sheet.play_style,
                judged_at=entry.judged_at.isoformat(),
            )
        ]
    return PreferenceSet.model_validate(payload)


def _shown(value, suffix=""):
    return "미상" if value is None else f"{value}{suffix}"


def _card_html(label, card, side, policy):
    feet = card.left_foot, card.right_foot
    weak = "미상" if None in feet else min(feet)
    skills = "미상" if card.skills is None else ", ".join(card.skills) or "없음"
    body_text = " · ".join(
        (_shown(card.height, "cm"), _shown(card.weight, "kg"), _shown(card.physical))
    )
    rows = [
        ("카드", f"{card.name} · 시즌 {card.season_id} · spid {card.spid}"),
        ("강화", f"{side.reinforce_level}강"),
        ("등록 포지션", ", ".join(p.value for p in card.positions)),
        ("키·몸무게·체형", body_text),
        (
            "왼발·오른발 (약발)",
            f"{_shown(feet[0])} · {_shown(feet[1])} (약발 {weak})",
        ),
        ("개인기", _shown(card.dribble_skill)),
        ("특성", skills),
        ("관측", f"{card.batch_id} · {card.observed_at.isoformat()}"),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows
    )
    for group in sorted(policy.groups, key=lambda g: list(_GROUP_LABELS).index(g.name)):
        values = " · ".join(
            f"{html.escape(s.name)} {_shown(card.stats.get(s.name))}"
            for s in sorted(group.stats, key=lambda s: s.name)
        )
        body += f"<tr><th>{_GROUP_LABELS[group.name]}</th><td>{values}</td></tr>"
    return f'<div class="card"><h3>{label}</h3><table>{body}</table></div>'


def render_form(pset, *, cards, policy):
    """로컬 판단 양식(HTML). OVR·점수·방식 선택·가격은 넣지 않는다."""
    by_key = {(c.spid, c.batch_id, c.version): c for c in cards}
    blocks = []
    for pair in pset.pairs:
        sides = []
        for label, side in (("A", pair.a), ("B", pair.b)):
            key = side.card
            sides.append(
                _card_html(
                    label, by_key[(key.spid, key.batch_id, key.version)], side, policy
                )
            )
        radios = "".join(
            f'<label><input type="radio" name="v-{pair.pair_id}" value="{v}"> '
            f"{text}</label>"
            for v, text in _VERDICT_LABELS
        )
        blocks.append(
            f'<section class="pair" data-pair="{pair.pair_id}">'
            f"<h2>{pair.pair_id} · 슬롯 {pair.slot.value}</h2>"
            f'<div class="sides">{"".join(sides)}</div>'
            f"<fieldset><legend>판단 (능력 기준)</legend>{radios}</fieldset>"
            f'<textarea name="r-{pair.pair_id}" rows="2" '
            f'placeholder="판단 근거"></textarea></section>'
        )
    meta = json.dumps(
        dict(
            schema_version=SHEET_SCHEMA,
            set_id=pset.set_id,
            set_version=pset.version,
            pairs=[p.pair_id for p in pset.pairs],
        )
    )
    return _FORM.format(
        set_id=html.escape(pset.set_id),
        reference=html.escape(pset.material.reference_time.isoformat()),
        count=len(pset.pairs),
        blocks="".join(blocks),
        meta=meta,
    )


_FORM = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MF-2 판단 양식</title>
<style>
:root {{ --bg:#fff; --fg:#1c1c1c; --line:#d0d0d0; --muted:#666; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161616; --fg:#e8e8e8; --line:#3a3a3a; --muted:#aaa; }}
}}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 sans-serif;
  margin:0 auto; max-width:1100px; padding:16px; }}
.pair {{ border-top:2px solid var(--line); padding:12px 0; }}
.sides {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ border-bottom:1px solid var(--line); padding:3px 6px; text-align:left;
  vertical-align:top; }}
th {{ white-space:nowrap; color:var(--muted); font-weight:normal; }}
fieldset {{ border:1px solid var(--line); margin:8px 0; }}
label {{ margin-right:16px; }}
textarea, input[type=text] {{ width:100%; box-sizing:border-box; }}
.top {{ position:sticky; top:0; background:var(--bg); padding:8px 0; }}
</style></head><body>
<h1>MF-2 비교 판단 양식</h1>
<p>세트 {set_id} · 기준 시각 {reference} · {count}쌍. 같은 슬롯에서 두 카드의 능력만 보고
고르세요. OVR·점수·추천 방식·가격·급여는 일부러 표시하지 않습니다. 화면 순서(A/B)는
무작위로 정했습니다.</p>
<div class="top">
<label>판단자 ID(가명) <input type="text" id="judge" placeholder="예: judge-01"></label>
<label>플레이 성향 <input type="text" id="style" placeholder="예: 측면 돌파 위주"></label>
<p><span id="progress"></span> <button id="save" type="button">판단 파일 저장</button></p>
</div>
{blocks}
<script>
const META = {meta};
const KEY = "mf2-sheet-" + META.set_version;
const times = {{}};
function read() {{
  return META.pairs.map(id => {{
    const picked = document.querySelector(`input[name="v-${{id}}"]:checked`);
    const text = document.querySelector(`textarea[name="r-${{id}}"]`).value;
    return {{ pair_id: id, verdict: picked ? picked.value : null, rationale: text,
      judged_at: picked ? (times[id] || null) : null }};
  }});
}}
function sheet() {{
  return {{ schema_version: META.schema_version, set_id: META.set_id,
    set_version: META.set_version, judge_id: document.getElementById("judge").value.trim(),
    play_style: document.getElementById("style").value.trim(), entries: read() }};
}}
function update() {{
  const done = read().filter(e => e.verdict && e.rationale.trim()).length;
  document.getElementById("progress").textContent = `${{done}} / ${{META.pairs.length}} 완료`;
  try {{ localStorage.setItem(KEY, JSON.stringify({{ s: sheet(), t: times }})); }} catch (e) {{}}
}}
document.addEventListener("input", event => {{
  const name = event.target.name || "";
  if (name.startsWith("v-") || name.startsWith("r-")) {{
    times[name.slice(2)] = new Date().toISOString();
  }}
  update();
}});
try {{
  const saved = JSON.parse(localStorage.getItem(KEY) || "null");
  if (saved) {{
    Object.assign(times, saved.t || {{}});
    document.getElementById("judge").value = saved.s.judge_id || "";
    document.getElementById("style").value = saved.s.play_style || "";
    for (const e of saved.s.entries) {{
      if (e.verdict) {{
        const box = document.querySelector(`input[name="v-${{e.pair_id}}"][value="${{e.verdict}}"]`);
        if (box) box.checked = true;
      }}
      const area = document.querySelector(`textarea[name="r-${{e.pair_id}}"]`);
      if (area) area.value = e.rationale || "";
    }}
  }}
}} catch (e) {{}}
document.getElementById("save").addEventListener("click", () => {{
  const blob = new Blob([JSON.stringify(sheet(), null, 2)], {{ type: "application/json" }});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "judgments.json";
  link.click();
}});
update();
</script></body></html>
"""


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    with path.open("x", encoding="utf-8") as handle:
        if isinstance(value, str):
            handle.write(value)
        else:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")


def _prepare(args, parser):
    if args.output_dir.exists():
        parser.error("기존 준비 폴더를 덮어쓰지 않습니다.")
    spec = SelectionSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
    policy = ScoringPolicy.model_validate_json(args.policy.read_text(encoding="utf-8"))
    source = args.material_dir / "player_card.jsonl"
    hashes = {"player_card.jsonl": _sha256(source)}
    report = args.material_dir / "report.json"
    if report.exists():
        recorded = json.loads(report.read_text(encoding="utf-8"))
        expected = recorded.get("processed_hashes", {}).get("player_card.jsonl")
        if expected != hashes["player_card.jsonl"]:
            parser.error("가공 파일 해시가 완료 보고서와 다릅니다.")
        if recorded.get("run_id") != spec.player_card_batch:
            parser.error("완료 보고서의 실행 ID가 명시한 배치와 다릅니다.")
        hashes["report.json"] = _sha256(report)
    cards = [
        PlayerCard.model_validate(json.loads(line))
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pset, manifest = select_pairs(spec, policy=policy, cards=cards)
    form = render_form(pset, cards=cards, policy=policy)
    manifest["source_hashes"] = hashes
    manifest["form_sha256"] = hashlib.sha256(form.encode()).hexdigest()
    args.output_dir.mkdir(parents=True)
    _write(args.output_dir / "selection-spec.json", spec.model_dump(mode="json"))
    _write(args.output_dir / "set.pre.json", pset.model_dump(mode="json"))
    _write(args.output_dir / "judgments.template.json", judgment_template(pset))
    _write(args.output_dir / "form.html", form)
    _write(args.output_dir / "manifest.json", manifest)
    return dict(
        set_version_before_judgments=pset.version,
        pairs=manifest["pairs"],
        shortfall=manifest["shortfall"],
        persons=manifest["persons"],
    )


def _import(args, parser):
    if args.output.exists():
        parser.error("기존 판단 세트를 덮어쓰지 않습니다.")
    pset = PreferenceSet.model_validate_json(args.set.read_text(encoding="utf-8"))
    sheet = JudgmentSheet.model_validate_json(
        args.judgments.read_text(encoding="utf-8")
    )
    judged = import_judgments(pset, sheet)
    _write(args.output, judged.model_dump(mode="json"))
    return dict(
        set_version_before_judgments=pset.version,
        set_version_after_judgments=judged.version,
        judgments=sum(len(p.judgments) for p in judged.pairs),
        pending_pairs=sum(not p.judgments for p in judged.pairs),
        judgments_sha256=_sha256(args.judgments),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="MF-2 판단 수집 준비 (네트워크·DB·모델 호출 없음)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="쌍 추출·판단 양식 생성")
    prepare.add_argument("--material-dir", required=True, type=Path)
    prepare.add_argument("--policy", required=True, type=Path)
    prepare.add_argument("--spec", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    imported = commands.add_parser("import", help="판단 파일을 판단 전 세트에 붙이기")
    imported.add_argument("--set", required=True, type=Path)
    imported.add_argument("--judgments", required=True, type=Path)
    imported.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    run = _prepare if args.command == "prepare" else _import
    print(json.dumps(run(args, parser), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
