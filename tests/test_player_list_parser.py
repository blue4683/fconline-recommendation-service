"""선수 목록 파서의 합성 입력과 구조 변경 실패를 검증한다."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from FRS.schema.models import CardPrice, DataStatus
from FRS.utils.crawling.ability_parser import ParseError
from FRS.utils.crawling.player_list_parser import PlayerListContext, parse_player_list


@pytest.fixture
def context():
    return PlayerListContext(
        batch_id="synthetic-list",
        source="synthetic",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        delete_after=None,
        version="synthetic-list-v1",
        reinforce_level=5,
        skill_fields=("sprintspeed", "acceleration", "strength", "stamina"),
    )


def synthetic_row(spid=123000001, grade=5):
    prices = "".join(
        f'<span class="span_bp{level}" title="9,999,999,999,999,999,999" '
        'alt="9,999,999,999,999,999,999">축약값 무시</span>'
        for level in range(5, 12)
    )
    stats = "".join(
        f'<div class="td td_ar"><span class="skillData_{spid}" '
        f'data-type="{skill}">{value}</span></div>'
        for skill, value in zip(
            ("sprintspeed", "acceleration", "strength", "stamina"),
            (90, 91, 92, 93),
            strict=True,
        )
    )
    return f'''<div id="area_playerunit_{spid}"><div class="tr">
      <div class="td default">
        <div class="info_top"><div class="name">가상선수_목록</div>
          <input name="Strong{spid}" value="{grade}/9"></div>
        <div class="info_middle"><span class="position"><span class="txt">CM</span>
          <span class="skillData_{spid}">99</span></span></div>
        <div class="info_bottom"><div class="en_wrap">
          <a class="ability en_level{grade}">{grade}</a>
          <a class="selector_item en_level13">13</a></div></div>
      </div>
      <div class="td td_ar"><span class="pay">24</span></div>
      <div class="td td_ar"><span class="skillData_{spid}">99<em>(90)</em></span></div>
      {stats}<div class="td td_ar_bp bp_{spid}">{prices}</div>
    </div></div><div id="area_playersublist_{spid}"></div>'''


@pytest.mark.parametrize("grade", [5, 11])
def test_partial_observation_keeps_provenance_and_exact_bp(context, grade):
    context = context.model_copy(update={"reinforce_level": grade})
    (row,) = parse_player_list(synthetic_row(grade=grade), context)
    assert row.spid == 123000001
    assert row.salary == 24
    assert row.primary_position == "CM"
    assert row.displayed_ovr == 99 and row.base_ovr == 90
    assert row.displayed_stats == dict(
        speed=90, accelartion=91, struggle=92, stamina=93
    )
    assert row.reinforce_level == grade
    assert row.status == DataStatus.UNKNOWN and row.valid_until is None
    assert not hasattr(row, "positions")
    assert len(row.prices) == 7
    for price in row.prices:
        assert price.version == row.version == context.version
        assert price.batch_id == row.batch_id == context.batch_id
        assert price.observed_at == row.observed_at == context.observed_at
        assert price.source == row.source == context.source
        assert price.delete_after is None and price.valid_until is None
        assert price.status == DataStatus.UNKNOWN and price.price_type == "unknown"
        assert price.price == 9999999999999999999
        assert CardPrice.from_document(price.to_document()) == price


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('value="5/9"', 'value="6/11"'),
        ('class="ability en_level5"', 'class="ability en_level11"'),
        ('en_level5">5', 'en_level5">11'),
        ('name="Strong123000001"', 'name="Strong123000002"'),
        (">CM</span>", ">ZZ</span>"),
        (">99<em>", ">98<em>"),
        ("(90)", "(bad)"),
        (">24</span>", ">0</span>"),
        (">24</span>", ">24.5</span>"),
        ('data-type="strength"', 'data-type="acceleration"'),
        ('class="span_bp5"', 'class="span_bp55"'),
        ('alt="9,999,999,999,999,999,999"', 'alt="3,000"'),
        ('title="9,999,999,999,999,999,999"', 'title="1e20"'),
        ('title="9,999,999,999,999,999,999"', 'title="1,00"'),
        ('class="skillData_123000001"', 'class="skillData_123000002"'),
        ("area_playerunit_123000001", "area_playerunit_bad"),
        ('class="td td_ar"', 'class="td changed"'),
    ],
)
def test_changed_or_conflicting_structure_is_rejected(context, before, after):
    html = synthetic_row().replace(before, after)
    with pytest.raises(ParseError):
        parse_player_list(html, context)


@pytest.mark.parametrize("html", ["", "<h1>오류 페이지</h1>", "<div>결과 없음</div>"])
def test_unverified_empty_shape_is_rejected(context, html):
    with pytest.raises(ParseError):
        parse_player_list(html, context)


def test_duplicate_card_rejected(context):
    with pytest.raises(ParseError):
        parse_player_list(synthetic_row() * 2, context)


def test_multiple_cards_and_no_base_ovr(context):
    rows = parse_player_list(
        (synthetic_row() + synthetic_row(123000002)).replace("<em>(90)</em>", ""),
        context,
    )
    assert len(rows) == 2 and all(row.base_ovr is None for row in rows)


def test_zero_price_is_unknown_not_untradeable(context):
    html = synthetic_row().replace("9,999,999,999,999,999,999", "0")
    (row,) = parse_player_list(html, context)
    assert all(price.price is None for price in row.prices)
    assert all(price.status == DataStatus.UNKNOWN for price in row.prices)


@pytest.mark.parametrize(
    "skills",
    [
        ("sprintspeed", "sprintspeed", "strength", "stamina"),
        ("sprintspeed", "acceleration", "unknown", "stamina"),
        ("sprintspeed",),
    ],
)
def test_context_rejects_unverified_or_duplicate_skills(context, skills):
    with pytest.raises(ValidationError):
        PlayerListContext.model_validate(
            {**context.model_dump(), "skill_fields": skills}
        )


def test_multiple_listed_positions_do_not_become_full_card(context):
    extra = '<span class="position"><span class="txt">CDM</span><span class="skillData_123000001">98</span></span>'
    html = synthetic_row()
    html = html.replace(
        '</span></div>\n        <div class="info_bottom">',
        "</span>" + extra + '</div>\n        <div class="info_bottom">',
    )
    (row,) = parse_player_list(html, context)
    assert row.primary_position == "CM"
    assert row.listed_position_ovr == {"CM": 99, "CDM": 98}
    assert not hasattr(row, "positions")
    with pytest.raises(ParseError):
        parse_player_list(html.replace(">CDM</span>", ">CM</span>"), context)


def saved_run(tmp_path, html=None):
    import hashlib
    import json

    from FRS.diagnostics.player_list_report import LIST_URL

    body = (html or synthetic_row()).encode()
    (tmp_path / "list.body").write_bytes(body)
    entry = dict(
        method="POST",
        url=LIST_URL,
        status=200,
        path="list.body",
        sha256=hashlib.sha256(body).hexdigest(),
        observed_at="2026-01-01T00:00:00+00:00",
        label="synthetic-query",
        form=dict(
            n1Strong="5",
            strSkill1="sprintspeed",
            strSkill2="acceleration",
            strSkill3="strength",
            strSkill4="stamina",
        ),
    )
    manifest = dict(
        run_id="synthetic-diagnostic",
        status="completed",
        delete_after=None,
        responses=[entry, entry.copy()],
    )
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_saved_report_preserves_observations_without_inflating_distinct_keys(tmp_path):
    from FRS.diagnostics.player_list_report import saved_rows, summary

    saved_run(tmp_path)
    results = saved_rows(tmp_path)
    report = summary(results)
    assert report["row_observations"] == 2
    assert report["distinct_cards"] == 1
    assert report["distinct_card_price_keys"] == 7
    assert report["complete"] is False
    assert report["status"] == "unknown"
    assert results[0][1][0].observed_at.year == 2026


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "wrong"),
        ("path", "../outside.body"),
        ("method", "GET"),
        ("status", 302),
    ],
)
def test_saved_report_rejects_invalid_provenance(tmp_path, field, value):
    import json

    from FRS.diagnostics.player_list_report import saved_rows

    manifest = saved_run(tmp_path)
    manifest["responses"][0][field] = value
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        saved_rows(tmp_path)


@pytest.mark.parametrize("change", ["running", "no-list"])
def test_saved_report_rejects_unfinished_or_missing_evidence(tmp_path, change):
    import json

    from FRS.diagnostics.player_list_report import saved_rows

    manifest = saved_run(tmp_path)
    if change == "running":
        manifest["status"] = "inspecting"
    else:
        manifest["responses"] = []
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        saved_rows(tmp_path)
