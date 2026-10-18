from datetime import timedelta

import pytest
from bson import BSON
from bson.codec_options import CodecOptions
from test_ability_parser import context
from test_schema import OBSERVED, card, common

from FRS.diagnostics.coverage import (
    CoverageError,
    build_history_coverage,
    salary_band,
)
from FRS.schema.models import CardPrice, CardSalary, TeamColor
from FRS.utils.crawling.ability_parser import ParseError
from FRS.utils.crawling.price_parser import parse_price


def price_html(value):
    return f'<div class="txt"><span>현재가</span><strong alt="{value}" title="{value}">가상 표시</strong></div>'


def test_price_keeps_large_integer_provenance_and_unknown_meaning():
    amount = 2**63 + 123
    ctx = context(valid_until=OBSERVED + timedelta(hours=1))
    price = parse_price(price_html(f"{amount:,}"), ctx)
    assert price.price == amount
    assert price.spid == ctx.spid and price.reinforce_level == ctx.reinforce_level
    assert price.version == ctx.version and price.batch_id == ctx.batch_id
    assert price.status == price.price_type == "unknown"
    assert price.valid_until is None and price.delete_after is None
    assert not price.is_usable_at(OBSERVED)
    assert (
        CardPrice.from_document(
            BSON.encode(price.to_document()).decode(
                codec_options=CodecOptions(tz_aware=True)
            )
        )
        == price
    )


@pytest.mark.parametrize(
    "value", ["1.5", "12,34", "-1", "+2", "", "NaN", "1억", "0", str(10**34)]
)
def test_price_does_not_guess_unrecognized_or_unavailable_values(value):
    with pytest.raises(ParseError):
        parse_price(price_html(value), context())


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace('title="100"', 'title="101"'),
        lambda text: text.replace('title="100"', ""),
        lambda text: text.replace('alt="100"', ""),
        lambda text: text.replace("현재가", "최고가"),
        lambda text: text + text,
        lambda text: text.replace("<strong", "<b").replace("</strong>", "</b>"),
    ],
)
def test_price_structure_and_dual_attributes_must_agree(transform):
    with pytest.raises(ParseError):
        parse_price(transform(price_html(100)), context())


def test_price_ignores_other_prices_and_never_evaluates_scripts():
    html = (
        '<strong title="999999">999999</strong><script>throw Error("synthetic")</script>'
        + price_html(10**34 - 1)
    )
    assert parse_price(html, context()).price == 10**34 - 1


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, "<=20"),
        (20, "<=20"),
        (21, "21-25"),
        (25, "21-25"),
        (26, "26-30"),
        (30, "26-30"),
        (31, "31-35"),
        (35, "31-35"),
        (36, ">=36"),
        (42, ">=36"),
    ],
)
def test_salary_bands_have_closed_integer_boundaries(value, expected):
    assert salary_band(value) == expected


@pytest.mark.parametrize("value", [0, -1, True, 20.5, "20"])
def test_salary_bands_reject_invalid_values(value):
    with pytest.raises(CoverageError):
        salary_band(value)


def salary(**changes):
    return CardSalary(**common(**{"reinforce_level": 5, "salary": 20, **changes}))


def history(**changes):
    return TeamColor(**common(**{"type": "club", "name": "가상클럽", **changes}))


def build(cards=None, salaries=None, histories=None, **options):
    return build_history_coverage(
        [card(positions=["ST", "CF"])] if cards is None else cards,
        [salary()] if salaries is None else salaries,
        [history()] if histories is None else histories,
        **{
            "when": OBSERVED,
            "clubs": ("가상클럽",),
            "positions": ("ST", "CF", "GK"),
            **options,
        },
    )


def cell(report, position="ST", band="<=20"):
    return next(
        row
        for row in report["cells"]
        if row["position"] == position and row["salary_band"] == band
    )


def test_history_coverage_deduplicates_cards_and_retains_empty_cells():
    c = card(positions=["ST", "CF"])
    report = build(
        cards=[c, c], salaries=[salary(), salary()], histories=[history(), history()]
    )
    assert report["cell_count"] == 15
    assert cell(report)["observed_cards"] == cell(report, "CF")["observed_cards"] == 1
    assert cell(report)["observation_gap"] == 2
    assert cell(report, "GK")["observation_gap"] == 3
    assert report["history_candidate_cards"] == 1
    assert (
        report["coverage_status"] == "unverified" and not report["recommendation_ready"]
    )


def test_zero_observations_are_reported_for_entire_position_universe():
    report = build_history_coverage([], [], [], when=OBSERVED)
    assert report["cell_count"] == 28 * 5 * 3
    assert all(
        row["observed_cards"] == 0 and row["observation_gap"] == 3
        for row in report["cells"]
    )


def test_exact_club_names_do_not_merge_affiliates_or_nationality():
    report = build(histories=[history(name="가상클럽 II"), history(type="nation")])
    assert report["history_candidate_cards"] == 0


def test_full_observation_target_still_does_not_confirm_eligibility():
    cards, salaries, histories = [], [], []
    for person in range(1, 4):
        spid = 123000000 + person
        cards.append(card(spid=spid, person_id=person, positions=["ST"]))
        salaries.append(salary(spid=spid))
        histories.append(history(spid=spid))
    report = build(cards, salaries, histories)
    assert cell(report)["observed_cards"] == 3 and cell(report)["observation_gap"] == 0
    assert not report["recommendation_ready"]


def test_different_batches_are_not_implicitly_joined():
    report = build(histories=[history(batch_id="different")])
    assert report["history_candidate_cards"] == 0
    report = build(salaries=[salary(batch_id="different")])
    assert report["excluded_cards"] == {"missing_salary": 1}


def test_stale_history_is_inventory_and_not_recommendation_coverage():
    report = build(
        cards=[card(status="stale")],
        histories=[history(status="unknown")],
        when=OBSERVED + timedelta(days=1),
    )
    assert cell(report)["observed_cards"] == 1 and not report["recommendation_ready"]


@pytest.mark.parametrize("kind", ["card", "salary", "history"])
def test_future_and_deleted_observations_do_not_count(kind):
    args = {"cards": [card()], "salaries": [salary()], "histories": [history()]}
    key = {"card": "cards", "salary": "salaries", "history": "histories"}[kind]
    record = args[key][0]
    args[key] = [
        record.model_copy(update={"observed_at": OBSERVED + timedelta(seconds=1)})
    ]
    assert build(**args)["history_candidate_cards"] == 0
    args[key] = [record.model_copy(update={"delete_after": OBSERVED})]
    assert build(**args)["history_candidate_cards"] == 0


def test_ambiguous_versions_fail_instead_of_selecting_latest():
    with pytest.raises(CoverageError, match="스냅샷"):
        build(cards=[card(), card(version="different")])
    with pytest.raises(CoverageError, match="스냅샷"):
        build(salaries=[salary(), salary(salary=21)])
    with pytest.raises(CoverageError, match="버전"):
        build(salaries=[salary(version="different")])


def test_grade_and_position_labels_are_not_collapsed():
    report = build(reinforce_level=6)
    assert report["history_candidate_cards"] == 0
    report = build(cards=[card(positions=["CM"])], positions=("CM", "LCM"))
    assert cell(report, "CM")["observed_cards"] == 1
    assert cell(report, "LCM")["observed_cards"] == 0


@pytest.mark.parametrize(
    "options",
    [
        {"when": OBSERVED.replace(tzinfo=None)},
        {"minimum": True},
        {"minimum": 0},
        {"reinforce_level": True},
        {"positions": ("NEW",)},
        {"clubs": ()},
    ],
)
def test_invalid_coverage_query_is_rejected(options):
    with pytest.raises(CoverageError):
        build(**options)


def inspection(tmp_path, *, form=None, html=None):
    import hashlib
    import json

    body = (html or price_html(100)).encode()
    (tmp_path / "price.body").write_bytes(body)
    entry = {
        "url": "https://fconline.nexon.com/datacenter/PlayerPriceGraph",
        "status": 200,
        "form": {"spid": 123000001, "n1strong": 5} if form is None else form,
        "path": "price.body",
        "sha256": hashlib.sha256(body).hexdigest(),
        "observed_at": "2026-01-01T00:00:00Z",
    }
    text = json.dumps({"status": "stopped", "attempts": 2, "responses": [entry]})
    (tmp_path / "manifest.json").write_text(text)
    return text


def test_saved_price_loading_preserves_stopped_state_and_provenance(tmp_path):
    from FRS.diagnostics.local_market_report import load_price_observations

    before = inspection(tmp_path)
    prices = load_price_observations(tmp_path)
    assert len(prices) == 1 and prices[0].price == 100
    assert prices[0].spid == 123000001 and prices[0].reinforce_level == 5
    assert prices[0].batch_id == tmp_path.name
    assert (tmp_path / "manifest.json").read_text() == before


@pytest.mark.parametrize("fault", ["hash", "form", "path"])
def test_saved_price_rejects_tampered_body_or_missing_request_identity(tmp_path, fault):
    import json

    from FRS.diagnostics.local_market_report import load_price_observations

    inspection(tmp_path)
    path = tmp_path / "manifest.json"
    state = json.loads(path.read_text())
    if fault == "hash":
        (tmp_path / "price.body").write_bytes(b"tampered")
    elif fault == "form":
        state["responses"][0]["form"] = {"spid": 123000001}
    else:
        state["responses"][0]["path"] = "../price.body"
    path.write_text(json.dumps(state))
    with pytest.raises(ParseError):
        load_price_observations(tmp_path)
