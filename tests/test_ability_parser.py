from datetime import datetime, timedelta, timezone

import pytest

from FRS.utils.crawling.ability_parser import (
    ParseContext,
    ParseError,
    keyword_map,
    parse_ability,
    parse_club_history,
    reinforcement_delta,
    unverified_colors,
)


def context(grade=5, **changes):
    return ParseContext(
        **{
            "batch_id": "synthetic-batch",
            "source": "synthetic",
            "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "delete_after": None,
            "spid": 123000001,
            "reinforce_level": grade,
            "version": f"synthetic-v{grade}",
            **changes,
        }
    )


def ability_html(grade=5, spid=123000001, position="ST"):
    stats = "".join(
        f'<li class="ab"><span class="txt">{label}</span>'
        f'<span class="value">{100 + grade}<span class="diff">+9</span></span></li>'
        for label in keyword_map()
    )
    return f"""
    <div class="info_name"><span class="name">가상선수_0001</span></div>
    <div class="info_ab"><div class="position"><span class="txt">{position}</span>
    <span class="txt">+0</span><span class="skillData_{spid} value">105</span></div></div>
    <a class="ability en_level{grade}"></a>
    <div class="info_etc"><span class="height">180cm</span>
    <span class="weight">75kg</span><span class="physical">보통</span>
    <span class="skill">★★★★★★</span><span class="foot">L4 – <strong>R5</strong></span></div>
    <span class="etc nation"><span class="txt">가상국가</span></span>
    <div class="skill_wrap"><span><img alt="가상특성"/></span></div>
    <div class="pay"><span>20</span></div><div class="pay_side"><span>20</span></div>
    <div class="ovr_set"><span class="position value st">{100 + grade}</span></div>
    <div class="content_bottom"><ul class="data_wrap_playerinfo">{stats}</ul></div>
    """


def test_complete_values_exclude_diff_and_keep_unknown_freshness():
    parsed = parse_ability(ability_html(), context())
    assert len(parsed.card.stats) == 34
    assert set(parsed.card.stats.values()) == {105}
    assert parsed.card.positions == ("ST",)
    assert parsed.card.position_ovr == {"ST": 105}
    assert parsed.card.name == "가상선수_0001"
    assert (parsed.card.height, parsed.card.weight) == (180, 75)
    assert (parsed.card.left_foot, parsed.card.right_foot) == (4, 5)
    assert parsed.card.dribble_skill == 6
    assert parsed.card.skills == ("가상특성",)
    assert parsed.salary.salary == 20
    assert parsed.card.status == parsed.salary.status == "unknown"
    assert parsed.card.valid_until is None
    assert not parsed.card.is_usable_at(context().observed_at)


@pytest.mark.parametrize(
    "old,new",
    [
        ("skillData_123000001", "skillData_123000002"),
        ("en_level5", "en_level6"),
        ('class="name"', 'class="renamed"'),
        ('class="txt">ST', 'class="txt">NEW'),
        ('class="txt">ST', 'class="txt">+1'),
        ('class="txt">속력', 'class="txt">새항목'),
        ('class="value">105', 'class="value">--'),
        ('class="position value st"', 'class="position value new"'),
        ('class="data_wrap_playerinfo"', 'class="renamed"'),
        ('class="pay_side"><span>20', 'class="pay_side"><span>21'),
        ("L4 –", "unknown"),
    ],
)
def test_changed_structure_is_rejected(old, new):
    with pytest.raises(ParseError):
        parse_ability(ability_html().replace(old, new), context())


def test_missing_traits_are_unknown_not_confirmed_empty():
    html = ability_html().replace('class="skill_wrap"', 'class="renamed"')
    assert parse_ability(html, context()).card.skills is None


def test_clubs_and_nation_are_separate_unverified_records():
    clubs = parse_club_history("""<div class="data_detail_club"><div class="data_table">
        <div class="td club">가상클럽_A</div><div class="td club">가상클럽_A</div>
        <div class="td club">가상클럽_B</div></div></div>""")
    records = unverified_colors(parse_ability(ability_html(), context()), clubs)
    assert [(r.type, r.name) for r in records] == [
        ("club", "가상클럽_A"),
        ("club", "가상클럽_B"),
        ("nation", "가상국가"),
    ]
    assert all(r.status == "unknown" for r in records)
    with pytest.raises(ParseError):
        parse_club_history("<html></html>")


def test_reinforcement_delta_links_observed_baseline():
    base = parse_ability(ability_html(), context()).card
    enhanced = parse_ability(ability_html(11), context(11)).card
    delta = reinforcement_delta(base, enhanced)
    assert set(delta.stats_delta.values()) == {6}
    assert delta.position_ovr == {"ST": 111}
    assert delta.baseline_reinforce_level == 5
    assert delta.baseline_card_version == base.version
    assert delta.status == "unknown"
    other = enhanced.model_copy(update={"batch_id": "different"})
    with pytest.raises(ParseError):
        reinforcement_delta(base, other)


def test_delta_does_not_upgrade_unknown_or_expired_baseline():
    observed = context().observed_at
    base = parse_ability(
        ability_html(), context(valid_until=observed + timedelta(hours=1))
    ).card
    enhanced = parse_ability(
        ability_html(11), context(11, valid_until=observed + timedelta(hours=2))
    ).card
    assert reinforcement_delta(base, enhanced).valid_until == base.valid_until
    base = base.model_copy(update={"status": "unknown"})
    assert reinforcement_delta(base, enhanced).status == "unknown"
    enhanced = enhanced.model_copy(
        update={"observed_at": observed + timedelta(minutes=90)}
    )
    delta = reinforcement_delta(base, enhanced)
    assert delta.status == "stale" and delta.valid_until is None
