"""수동 갱신 계획이 대상을 확대하거나 원본을 승격하지 않는지 검증한다."""

from datetime import timedelta

import pytest
from test_ability_parser import ability_html, context
from test_freshness import OBSERVED, price

from FRS.utils.crawling.ability_parser import parse_ability, reinforcement_delta
from FRS.utils.crawling.refresh_plan import RefreshPlanError, build_plan


@pytest.fixture
def inputs():
    profiles = []
    prices = []
    for spid in (123000001, 123000002):
        parsed = []
        for grade in range(5, 12):
            parsed.append(
                parse_ability(
                    ability_html(grade, spid),
                    context(
                        grade,
                        spid=spid,
                        batch_id="synthetic-profiles",
                        version=f"synthetic-{spid}-{grade}",
                    ),
                )
            )
            prices.append(price(spid=spid, reinforce_level=grade))
        profiles.append(parsed[0].card)
        for item in parsed:
            profiles.extend(
                (item.salary, reinforcement_delta(parsed[0].card, item.card))
            )
    return dict(
        target_ids=(123000001, 123000002),
        profiles=profiles,
        prices=prices,
        profile_batch="synthetic-profiles",
        price_batch="synthetic-prices",
        at=OBSERVED,
    )


def test_plan_counts_shared_ability_salary_requests_once_and_sends_none(inputs):
    before = [r.model_dump() for r in inputs["profiles"] + inputs["prices"]]
    result = build_plan(**inputs)
    assert result["target_cards"] == 2
    assert result["estimated_requests_if_both_jobs_run"] == 15 + 2 * 7
    assert result["estimated_requests_due"] == 0
    assert result["network_requests_sent"] == 0 and not result["execution_authorized"]
    assert not result["auto_execution"] and not result["originals_modified"]
    assert result["blocked_jobs"] == [
        dict(resource="team_color", reason="verified_eligibility_source_unconfirmed")
    ]
    assert [r.model_dump() for r in inputs["profiles"] + inputs["prices"]] == before
    assert all(
        r.status == "unknown" and r.valid_until is None
        for r in inputs["profiles"] + inputs["prices"]
    )


def test_refresh_due_does_not_wait_for_expiration_and_uses_oldest_observation(inputs):
    duplicate = inputs["prices"][0].model_copy(
        update={"observed_at": OBSERVED + timedelta(hours=1)}
    )
    inputs["prices"].append(duplicate)
    result = build_plan(**{**inputs, "at": OBSERVED + timedelta(hours=12)})
    assert result["estimated_requests_due"] == 15
    assert (
        result["jobs"][0]["refresh_due"] and not result["jobs"][0]["expired_by_policy"]
    )
    assert result["jobs"][0]["oldest_observed_at"] == OBSERVED.isoformat()
    assert result["price_keys"] == 14
    result = build_plan(**{**inputs, "at": OBSERVED + timedelta(days=3)})
    assert result["estimated_requests_due"] == 29


@pytest.mark.parametrize(
    "case",
    [
        "future",
        "missing-price",
        "price-conflict",
        "profile-conflict",
        "wrong-profile-batch",
        "wrong-price-batch",
        "missing-grade",
        "wrong-version",
        "wrong-baseline",
        "unmanaged-target",
        "duplicate-target",
        "empty-target",
    ],
)
def test_invalid_evidence_does_not_silently_choose_latest_or_expand_scope(inputs, case):
    if case == "future":
        inputs["at"] = OBSERVED - timedelta(seconds=1)
    elif case == "missing-price":
        inputs["prices"].pop()
    elif case == "price-conflict":
        inputs["prices"].append(inputs["prices"][0].model_copy(update={"price": 9999}))
    elif case == "profile-conflict":
        inputs["profiles"].append(inputs["profiles"][0])
    elif case == "wrong-profile-batch":
        inputs["profile_batch"] = "other"
    elif case == "wrong-price-batch":
        inputs["price_batch"] = "other"
    elif case == "missing-grade":
        inputs["profiles"].pop()
    elif case == "wrong-version":
        inputs["profiles"][1] = inputs["profiles"][1].model_copy(
            update={"version": "changed"}
        )
    elif case == "wrong-baseline":
        inputs["profiles"][2] = inputs["profiles"][2].model_copy(
            update={"baseline_card_version": "changed"}
        )
    elif case == "unmanaged-target":
        inputs["target_ids"] = (123000001,)
    elif case == "duplicate-target":
        inputs["target_ids"] = (123000001, 123000001)
    else:
        inputs["target_ids"] = ()
    with pytest.raises(RefreshPlanError):
        build_plan(**inputs)


def test_unknown_price_is_retained_as_unknown(inputs):
    inputs["prices"][0] = inputs["prices"][0].model_copy(update={"price": None})
    result = build_plan(**inputs)
    assert result["unknown_price_values"] == 1
    assert result["network_requests_sent"] == 0
