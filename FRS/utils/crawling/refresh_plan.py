"""최근 보강 259개 카드의 수동 갱신 계획. HTTP·DB·스케줄러를 실행하지 않는다."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from FRS.core.freshness import Resource, specification, utc, window
from FRS.diagnostics.player_list_report import saved_rows
from FRS.schema.models import CardPrice, CardSalary, PlayerCard, ReinforceStats
from FRS.schema.scope import SUPPORTED_REINFORCEMENTS
from FRS.utils.crawling.enrich_cards import (
    RUN_ID,
    SOURCE_ID,
    CardEnrichment,
    source_selection,
)
from FRS.utils.crawling.pilot import ROOT, PilotStopped
from FRS.utils.crawling.verify_enrichment import verify


class RefreshPlanError(ValueError):
    pass


def _profile_records(records, targets, batch_id):
    groups = {PlayerCard: {}, CardSalary: {}, ReinforceStats: {}}
    for record in records:
        kind = type(record)
        if (
            kind not in groups
            or record.batch_id != batch_id
            or record.spid not in targets
        ):
            raise RefreshPlanError("프로필 관측의 종류·배치·관리 대상이 다릅니다.")
        grade = (
            record.base_reinforce_level
            if kind is PlayerCard
            else record.reinforce_level
        )
        key = record.spid, grade
        if key in groups[kind]:
            raise RefreshPlanError("프로필 관측이 중복/충돌합니다.")
        groups[kind][key] = record
    for spid in targets:
        base = groups[PlayerCard].get((spid, 5))
        if base is None:
            raise RefreshPlanError("기준 5강 카드가 없습니다.")
        for grade in SUPPORTED_REINFORCEMENTS:
            salary = groups[CardSalary].get((spid, grade))
            delta = groups[ReinforceStats].get((spid, grade))
            if (
                salary is None
                or delta is None
                or (
                    delta.baseline_card_version != base.version
                    or delta.baseline_reinforce_level != 5
                    or salary.version != delta.version
                    or (grade == 5 and delta.version != base.version)
                )
            ):
                raise RefreshPlanError("급여·강화 관측의 누락/버전 연결 오류입니다.")
    expected = len(targets)
    if [len(groups[k]) for k in (PlayerCard, CardSalary, ReinforceStats)] != [
        expected,
        expected * 7,
        expected * 7,
    ]:
        raise RefreshPlanError("프로필 관측의 강화 범위/개수가 다릅니다.")
    return records


def _price_records(prices, targets, batch_id):
    values = {}
    for record in prices:
        if (
            not isinstance(record, CardPrice)
            or record.batch_id != batch_id
            or record.spid not in targets
        ):
            raise RefreshPlanError("가격 관측의 종류·배치·관리 대상이 다릅니다.")
        if record.reinforce_level not in SUPPORTED_REINFORCEMENTS:
            raise RefreshPlanError("가격 강화 범위가 다릅니다.")
        key = record.spid, record.reinforce_level
        value = record.price, record.price_type
        if key in values and values[key] != value:
            raise RefreshPlanError("같은 가격 키의 관측 값이 충돌합니다.")
        values[key] = value
    if set(values) != {
        (spid, grade) for spid in targets for grade in SUPPORTED_REINFORCEMENTS
    }:
        raise RefreshPlanError("관리 대상의 강화별 가격 관측이 없습니다.")
    return values


def build_plan(*, target_ids, profiles, prices, profile_batch, price_batch, at):
    at = utc(at)
    ids = tuple(target_ids)
    if (
        not ids
        or len(ids) != len(set(ids))
        or len(ids) > 259
        or any(
            type(spid) is not int or not 1_000_000 <= spid <= 999_999_999
            for spid in ids
        )
    ):
        raise RefreshPlanError("첫 관리 대상 범위를 벗어났습니다.")
    targets = set(ids)
    profiles, prices = list(profiles), list(prices)
    _profile_records(profiles, targets, profile_batch)
    price_values = _price_records(prices, targets, price_batch)
    if any(utc(record.observed_at) > at for record in (*profiles, *prices)):
        raise RefreshPlanError("미래 관측이 있어 갱신 시점을 판단하지 않습니다.")
    jobs = []
    for name, resources, records, count in (
        (
            "catalog_prices_and_memberships",
            [Resource.PRICE, Resource.TEAM_COLOR],
            prices,
            15,
        ),
        (
            "abilities_and_salaries",
            [Resource.ABILITY, Resource.SALARY],
            profiles,
            len(ids) * 7,
        ),
    ):
        # 계획 시점만 보수적으로 정한다. 가격/최신 관측을 선택하거나 병합하지 않는다.
        oldest = min(record.observed_at for record in records)
        timing = window(resources[0], oldest, at=at)
        jobs.append(
            dict(
                name=name,
                resources=[r.value for r in resources],
                oldest_observed_at=utc(oldest).isoformat(),
                refresh_due_at=timing.refresh_due_at.isoformat(),
                policy_expires_at=timing.policy_expires_at.isoformat(),
                refresh_due=timing.refresh_due,
                expired_by_policy=timing.expired_by_policy,
                estimated_requests=count,
            )
        )
    return dict(
        mode="plan_only",
        as_of=at.isoformat(),
        policy=specification(),
        profile_batch=profile_batch,
        price_batch=price_batch,
        target_cards=len(ids),
        reinforcement_levels=list(SUPPORTED_REINFORCEMENTS),
        jobs=jobs,
        estimated_requests_if_both_jobs_run=sum(j["estimated_requests"] for j in jobs),
        estimated_requests_due=sum(
            j["estimated_requests"] for j in jobs if j["refresh_due"]
        ),
        blocked_jobs=[],
        price_keys=len(price_values),
        unknown_price_values=sum(v[0] is None for v in price_values.values()),
        source_status_counts=dict(Counter(str(r.status) for r in (*profiles, *prices))),
        execution_authorized=False,
        network_requests_sent=0,
        auto_execution=False,
        originals_modified=False,
    )


def load_plan(*, at, profile_batch=RUN_ID, catalog_batch=SOURCE_ID):
    from FRS.utils.crawling.refresh_run import validate_run_id
    from FRS.utils.crawling.verify_refresh import verify as verify_refresh

    raw = ROOT / "data/raw" / RUN_ID
    processed = ROOT / "data/processed" / RUN_ID
    source = ROOT / "data/raw" / SOURCE_ID
    selection = source_selection(source)
    state = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
    if (
        len(state["selected_ids"]) != 259
        or state.get("selection") != selection
        or state["status"] != "completed"
    ):
        raise RefreshPlanError("확정한 완료 배치의 259개 관리 대상이 아닙니다.")
    targets = state["selected_ids"]
    cache = {}

    def refreshed(batch):
        if batch.startswith("stage-1-f-derived-"):
            from FRS.utils.crawling.derive_refresh import verify as verify_derived

            if batch not in cache:
                cache[batch] = verify_derived(ROOT, batch, expected_targets=targets)
            return cache[batch]
        validate_run_id(batch)
        if batch not in cache:
            cache[batch] = verify_refresh(
                ROOT / "data/raw" / batch,
                ROOT / "data/processed" / batch,
                expected_targets=targets,
            )
        return cache[batch]

    if profile_batch == RUN_ID:
        verify(raw, processed, expected_selection=selection)
        reader = object.__new__(CardEnrichment)
        reader.state, reader.processed_path = state, processed
        data = {key: [] for key in ("player_card", "card_salary", "reinforce_stats")}
        for spid in targets:
            rows = reader.load_card(spid)
            for key in data:
                data[key].extend(rows[key])
    else:
        data = refreshed(profile_batch)
        if not all(
            key in data for key in ("player_card", "card_salary", "reinforce_stats")
        ):
            raise RefreshPlanError("능력치·급여가 없는 갱신 배치입니다.")
    profiles = [
        r
        for key in ("player_card", "card_salary", "reinforce_stats")
        for r in data[key]
    ]
    memberships = []
    if catalog_batch == SOURCE_ID:
        prices = [
            p
            for _, rows in saved_rows(source)
            for row in rows
            if row.spid in targets
            for p in row.prices
        ]
    else:
        data = refreshed(catalog_batch)
        if "card_price" not in data or "team_color" not in data:
            raise RefreshPlanError("가격·공식 자격 관측이 없는 갱신 배치입니다.")
        prices, memberships = data["card_price"], data["team_color"]
    result = build_plan(
        target_ids=targets,
        profiles=profiles,
        prices=prices,
        profile_batch=profile_batch,
        price_batch=catalog_batch,
        at=at,
    )
    result["membership_baseline"] = catalog_batch if memberships else None
    result["membership_observations"] = len(memberships)
    result["membership_initialization_needed"] = not memberships
    result["input_manifest_hashes"] = {
        batch: hashlib.sha256(
            (ROOT / "data/raw" / batch / "manifest.json").read_bytes()
        ).hexdigest()
        for batch in sorted({RUN_ID, SOURCE_ID, profile_batch, catalog_batch})
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan", action="store_true", help="계획을 표시합니다(기본값). 외부 요청 없음."
    )
    parser.add_argument(
        "--at", type=datetime.fromisoformat, help="시간대를 포함한 기준 시각"
    )
    parser.add_argument("--profile-batch-id", default=RUN_ID)
    parser.add_argument("--catalog-batch-id", default=SOURCE_ID)
    args = parser.parse_args()
    try:
        result = load_plan(
            at=args.at or datetime.now(timezone.utc),
            profile_batch=args.profile_batch_id,
            catalog_batch=args.catalog_batch_id,
        )
    except (ValueError, TypeError, OSError, PilotStopped) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
