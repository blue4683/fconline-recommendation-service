"""실데이터 수집기의 분할·대조·중단 규칙을 합성 응답으로 검증한다. 네트워크 없음."""

import json
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from test_ability_parser import ability_html
from test_player_list_parser import synthetic_row

from FRS.utils.crawling.realdata_catalog import (
    ABILITY_URL,
    BANDS,
    LIST_CAP,
    MIXED_ELIGIBILITY_BASIS,
    CannotSplit,
    CatalogStopped,
    Cell,
    ClubSpec,
    NationSpec,
    RealDataRun,
    build_form,
    initial_cells,
    main,
    split_cell,
    validate_template,
)

OBSERVED = datetime(2026, 10, 21, tzinfo=timezone.utc)
CLUB = ClubSpec("가상클럽", 13, 9)
NATION = NationSpec("가상국가", 27)
CLUB_CARD, NATION_CARD = 123000001, 123000002
# 공식 목록의 결과 없음 화면 구조만 남긴 합성 응답이다.
EMPTY_LIST_HTML = (
    '<div class="tr empty">\n  <span class="txt">\n    검색 결과가 없습니다.\n'
    "  </span>\n</div>\n<script></script>"
).encode()
SKILLS = ("sprintspeed", "acceleration", "strength", "stamina")


def template(**changes):
    form = {
        "n4LeagueId": "0",
        "n4TeamId": "",
        "n1History": "0",
        "teamcolorid": "0",
        "n4SalaryMin": "4",
        "n4SalaryMax": "99",
        "n4PageNo": "1",
        "n1Strong": "1",
        "strPosition": "",
        **{f"strSkill{i}": s for i, s in enumerate(SKILLS, 1)},
    }
    form.update(changes)
    return form


def rows_html(spids, salary, grade=5):
    return "".join(
        synthetic_row(spid, grade).replace(">24</span>", f">{salary}</span>", 1)
        for spid in spids
    )


class Server:
    """칸마다 응답할 카드 수를 정해 두는 합성 서버."""

    def __init__(self, sizes=None, parity_salary=None):
        self.sizes = sizes or {}
        self.parity_salary = parity_salary
        self.calls = []
        self.next_id = count(123000001)
        self.ids = {}

    def cards(self, key, size):
        if key not in self.ids:
            self.ids[key] = [next(self.next_id) for _ in range(size)]
        return self.ids[key]

    def __call__(self, url, form, resource):
        self.calls.append((resource, form))
        if resource == "ability":
            html = ability_html(form["n1Strong"], form["spid"])
            return html.replace("<span>20</span>", "<span>21</span>").encode()
        low, high = int(form["n4SalaryMin"]), int(form["n4SalaryMax"])
        grade = int(form["n1Strong"])
        key = (low, high, form.get("strPosition", ""))
        size = self.sizes.get(key, 1 if (low, high) == (21, 25) else 0)
        if size == 0:
            return EMPTY_LIST_HTML
        salary = low
        if grade == 11 and self.parity_salary is not None:
            salary = self.parity_salary
        return rows_html(self.cards(key, size), salary, grade).encode()


@pytest.fixture
def make(tmp_path):
    def build(server, **options):
        ticks = count(0, 2)
        defaults = dict(
            run_id="real-data-synthetic",
            clubs=[CLUB],
            template=template(),
            max_attempts=200,
            allow_empty_cells=True,
            clock=lambda: OBSERVED + timedelta(seconds=next(ticks)),
        )
        defaults.update(options)
        return RealDataRun(tmp_path / "raw", tmp_path / "out", server, **defaults)

    return build


def test_split_halves_salary_then_positions_then_gives_up():
    cell = Cell(CLUB, 4, 20)
    low, high = split_cell(cell)
    assert (low.low, low.high, high.low, high.high) == (4, 12, 13, 20)
    single = Cell(CLUB, 7, 7)
    with pytest.raises(CannotSplit):
        split_cell(single)
    by_position = split_cell(single, ("ST", "GK"))
    assert [c.position for c in by_position] == ["ST", "GK"]
    with pytest.raises(CannotSplit):
        split_cell(by_position[0], ("ST",))


def test_initial_cells_cover_every_band_for_every_club():
    other = ClubSpec("다른클럽", 14, 10)
    cells = initial_cells([CLUB, other])
    assert len(cells) == 2 * len(BANDS)
    assert {(c.low, c.high) for c in cells} == set(BANDS)
    with pytest.raises(ValueError):
        initial_cells([CLUB, CLUB])


def test_template_must_be_the_club_career_form():
    validate_template(template())
    for bad in (
        template(teamcolorid="1001"),
        template(n1History="1"),
        template(strSkill4="agility"),
    ):
        with pytest.raises(ValueError):
            validate_template(bad)
    with pytest.raises(ValueError):
        validate_template({k: v for k, v in template().items() if k != "n4TeamId"})


def test_build_form_overrides_only_search_keys_and_needs_a_known_position_field():
    cell = Cell(CLUB, 21, 25)
    form = build_form(template(), cell, level=11)
    assert form["n4LeagueId"] == "13" and form["n4TeamId"] == "9"
    assert form["n1Strong"] == "11" and form["n1History"] == "0"
    assert form["strPosition"] == ""
    with pytest.raises(ValueError):
        build_form(template(), Cell(CLUB, 5, 5, "ST"))
    assert (
        build_form(template(), Cell(CLUB, 5, 5, "ST"), position_field="strPosition")[
            "strPosition"
        ]
        == "ST"
    )


def test_complete_run_collects_cards_with_club_career_basis(make, tmp_path):
    server = Server()
    run = make(server)
    report = run.execute()
    assert report["status"] == "completed", report
    assert report["eligibility_basis"] == "club_career_search"
    assert report["catalog_cards"] == 1
    assert report["cards_by_club"] == {"가상클럽": 1}
    # 목록 5 + 급여 대조 1 + 능력치 1
    assert report["attempts"] == 7
    resources = [r for r, _ in server.calls]
    assert resources == ["player_list"] * 5 + ["player_list", "ability"]
    out = tmp_path / "out"
    prices = (out / "card_price.jsonl").read_text().splitlines()
    salaries = (out / "card_salary.jsonl").read_text().splitlines()
    stats = (out / "reinforce_stats.jsonl").read_text().splitlines()
    assert (len(prices), len(salaries), len(stats)) == (7, 7, 6)
    # 5강 기준 누적 보너스 표(6,8,11,15,17,19,21)의 6~11강 차이
    deltas = [json.loads(s)["stats_delta"]["speed"] for s in stats]
    assert deltas == [2, 5, 9, 11, 13, 15]
    assert json.loads((out / "team_color.jsonl").read_text())["name"] == "가상클럽"
    manifest = json.loads((tmp_path / "raw/manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["retries"] == 0


def test_only_an_exactly_full_list_is_split(make):
    full = {(21, 25, ""): LIST_CAP}
    server = Server(full)
    run = make(server, max_attempts=2000)
    # 200장 칸은 나뉘고 하위 칸은 1장씩만 돌려주도록 합성한다.
    original = server.__call__

    def respond(url, form, resource):
        low, high = int(form["n4SalaryMin"]), int(form["n4SalaryMax"])
        if resource == "player_list" and (low, high) != (21, 25):
            server.sizes[(low, high, "")] = 1 if 21 <= low <= 25 else 0
        return original(url, form, resource)

    run.fetch_body = respond
    run.collect_catalog()
    assert [s["low"] for s in run.split_log] == [21]
    assert run.truncated == []
    ranges = {(c.low, c.high) for c, rows in run.accepted if rows}
    assert ranges == {(21, 23), (24, 25)}  # 200장 칸 자체는 버리고 하위 칸만 쓴다
    # 정확히 200장이 아닌 칸은 나누지 않는다.
    assert all(s["low"] == 21 for s in run.split_log)


def test_unsplittable_full_cell_is_flagged_not_dropped(make):
    server = Server({(7, 7, ""): LIST_CAP})
    run = make(server, max_attempts=100)
    cell = Cell(CLUB, 7, 7)
    form = build_form(run.template, cell)
    rows, entry = run.list_rows(form, 5)
    assert len(rows) == LIST_CAP
    run.accepted.clear()
    run.truncated.append(dict(cell.describe(), reason="test"))
    run.accept(cell, rows, entry)
    assert len(run.rows) == LIST_CAP


def test_duplicate_cards_across_cells_are_counted_once(make):
    run = make(Server())
    cell = Cell(CLUB, 21, 25)
    rows, entry = run.list_rows(build_form(run.template, cell), 5)
    run.accept(cell, rows, entry)
    run.accept(Cell(CLUB, 21, 25, "ST"), rows, entry)
    assert len(run.rows) == len(run.memberships) == 1
    assert len(run.prices) == 7


OTHER = ClubSpec("가상클럽2", 13, 5)
SHARED = 123000002


class TwoClubServer:
    """두 클럽이 카드 하나(SHARED)를 함께 보이는 합성 서버. 급여 21~25 칸에만 카드가 있다."""

    def __init__(self, cheap_on_second=False):
        self.cheap_on_second = cheap_on_second
        self.calls = []

    def __call__(self, url, form, resource):
        self.calls.append((resource, form))
        if resource == "ability":
            html = ability_html(form["n1Strong"], form["spid"])
            return html.replace("<span>20</span>", "<span>21</span>").encode()
        if (int(form["n4SalaryMin"]), int(form["n4SalaryMax"])) != (21, 25):
            return EMPTY_LIST_HTML
        grade = int(form["n1Strong"])
        spids = {"9": [123000001, SHARED], "5": [SHARED, 123000003]}[form["n4TeamId"]]
        html = ""
        for spid in spids:
            row = rows_html([spid], 21, grade)
            if self.cheap_on_second and form["n4TeamId"] == "5" and spid == SHARED:
                row = row.replace("9,999,999,999,999,999,999", "1,000")
            html += row
        return html.encode()


def test_cards_in_two_clubs_keep_one_membership_per_club(make, tmp_path):
    report = make(TwoClubServer(), clubs=[CLUB, OTHER]).execute()
    assert report["status"] == "completed", report
    assert report["catalog_cards"] == 3 and report["profile_cards"] == 3
    assert report["cards_by_club"] == {CLUB.name: 2, OTHER.name: 2}
    assert report["price_conflicts"] == 0
    colors = [
        json.loads(line)
        for line in (tmp_path / "out/team_color.jsonl").read_text().splitlines()
    ]
    assert sorted((c["name"], c["spid"]) for c in colors) == sorted(
        [
            (CLUB.name, 123000001),
            (CLUB.name, SHARED),
            (OTHER.name, SHARED),
            (OTHER.name, 123000003),
        ]
    )


def test_a_price_seen_with_two_values_becomes_unknown_without_stopping(make, tmp_path):
    report = make(TwoClubServer(cheap_on_second=True), clubs=[CLUB, OTHER]).execute()
    assert report["status"] == "completed", report
    assert report["price_conflicts"] == 7  # 공유 카드의 5~11강 가격
    prices = [
        json.loads(line)
        for line in (tmp_path / "out/card_price.jsonl").read_text().splitlines()
    ]
    shared = [p for p in prices if p["spid"] == SHARED]
    assert len(shared) == 7
    assert all(p["status"] == "unknown" and p["price"] is None for p in shared)
    assert all(p["status"] == "ok" for p in prices if p["spid"] != SHARED)
    manifest = json.loads((tmp_path / "raw/manifest.json").read_text())
    assert {tuple(c["prices"]) for c in manifest["price_conflicts"]} == {
        (9999999999999999999, 1000)
    }


def test_catalog_only_measures_cards_without_ability_requests(make, tmp_path):
    server = TwoClubServer()
    report = make(server, clubs=[CLUB, OTHER], catalog_only=True).execute()
    assert report["status"] == "catalog_measured"
    assert report["catalog_cards"] == 3 and report["profile_cards"] == 0
    assert report["cards_by_club"] == {CLUB.name: 2, OTHER.name: 2}
    # 목록 5칸 × 2클럽 + 급여 대조 2, 능력치 0
    assert [r for r, _ in server.calls] == ["player_list"] * 12
    assert report["attempts"] == 12
    assert list((tmp_path / "out").glob("*.jsonl")) == []


def test_only_the_confirmed_no_result_page_counts_as_an_empty_cell(make, tmp_path):
    report = make(Server(), allow_empty_cells=False).execute()
    assert report["status"] == "completed" and report["empty_cells"] == 4

    class OtherEmpty(Server):
        def __init__(self, body):
            super().__init__()
            self.body = body

        def __call__(self, url, form, resource):
            answer = super().__call__(url, form, resource)
            return self.body if answer == EMPTY_LIST_HTML else answer

    for index, body in enumerate(
        (b"<html>empty</html>", EMPTY_LIST_HTML + EMPTY_LIST_HTML)
    ):
        run = RealDataRun(
            tmp_path / f"raw{index}",
            tmp_path / f"out{index}",
            OtherEmpty(body),
            run_id="real-data-synthetic",
            clubs=[CLUB],
            template=template(),
            max_attempts=200,
            clock=lambda: OBSERVED,
        )
        report = run.execute()
        assert report["status"] == "stopped"
        assert report["stop_reason"]["kind"] == "empty_list_unverified"


def test_salary_mismatch_between_grades_stops_the_run(make):
    run = make(Server(parity_salary=23))
    report = run.execute()
    assert report["status"] == "stopped"
    assert report["stop_reason"]["kind"] in {"parity", "salary_range"}
    assert report["profile_cards"] == 0


def test_budget_is_reserved_before_the_request_and_never_retried(make):
    server = Server()
    run = make(server, max_attempts=3)
    report = run.execute()
    assert report["status"] == "stopped"
    assert report["stop_reason"]["kind"] == "budget_exhausted"
    assert report["attempts"] == 3 and len(server.calls) == 3


def test_transport_failure_stops_without_retry(make):
    calls = []

    def blocked(url, form, resource):
        calls.append(resource)
        raise CatalogStopped("blocked", "403")

    report = make(blocked).execute()
    assert report["status"] == "stopped" and report["stop_reason"]["kind"] == "blocked"
    assert len(calls) == 1


def test_structure_change_stops_instead_of_guessing(make):
    server = Server()
    original = server.__call__

    def broken(url, form, resource):
        body = original(url, form, resource)
        return body.replace(b'class="pay"', b'class="renamed"')

    report = make(broken).execute()
    assert report["status"] == "stopped" and report["stop_reason"]["kind"] == "parsing"


def test_detail_salary_conflict_stops(make):
    server = Server()
    original = server.__call__

    def respond(url, form, resource):
        body = original(url, form, resource)
        if resource == "ability":
            body = body.replace(b"<span>21</span>", b"<span>19</span>")
        return body

    report = make(respond).execute()
    assert report["status"] in {"stopped"}
    assert report["stop_reason"]["kind"] in {"parsing", "conflict"}


def test_finished_run_cannot_request_again_and_run_dirs_are_not_reused(make, tmp_path):
    run = make(Server())
    run.execute()
    with pytest.raises(CatalogStopped):
        run.request(ABILITY_URL, {}, "ability")
    with pytest.raises(FileExistsError):
        make(Server())


@pytest.mark.parametrize("run_id", ["synthetic", "real-data-", "real-data-A", "x" * 90])
def test_run_id_must_be_explicit_real_data_id(make, run_id):
    with pytest.raises(CatalogStopped):
        make(Server(), run_id=run_id)


def test_cli_defaults_to_plan_without_creating_anything(tmp_path, capsys):
    assert main(["--club", "가상클럽:13:9", "--root", str(tmp_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["initial_list_requests"] == len(BANDS)
    assert plan["retries"] == 0
    assert not (tmp_path / "data").exists()


def test_cli_run_requires_explicit_scope(tmp_path):
    with pytest.raises(SystemExit):
        main(["--club", "가상클럽:13:9", "--run", "--root", str(tmp_path)])
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("attempts", [0, 8001, "700", 1.5])
def test_attempt_cap_must_be_an_integer_within_the_code_limit(make, attempts):
    with pytest.raises(CatalogStopped, match="1~8000"):
        make(Server(), max_attempts=attempts)


def test_a_three_club_sized_cap_is_accepted(make):
    assert make(Server(), max_attempts=5800).max_attempts == 5800


def flag_html(nation_id):
    return (
        '<div class="nation"><img src="https://example.invalid/live/externalAssets/'
        f'common/countries/smallflags/{nation_id}.png" alt=""></div>'
    )


class NationServer:
    """클럽 칸과 국적 칸이 서로 다른 카드를 돌려준다. 상세 국기 번호를 정할 수 있다."""

    def __init__(self, flag=27):
        self.flag, self.calls = flag, []

    def __call__(self, url, form, resource):
        self.calls.append((resource, form))
        if resource == "ability":
            html = ability_html(form["n1Strong"], form["spid"])
            html = html.replace("<span>20</span>", "<span>21</span>")
            flag = self.flag if form["spid"] == NATION_CARD else 21
            return (flag_html(flag) + html).encode()
        low, high = int(form["n4SalaryMin"]), int(form["n4SalaryMax"])
        if (low, high) != (21, 25):
            return EMPTY_LIST_HTML
        spid = NATION_CARD if form["n4NationId"] == "27" else CLUB_CARD
        return rows_html([spid], low, int(form["n1Strong"])).encode()


def test_nation_cells_search_by_nationality_and_keep_a_nation_membership(
    make, tmp_path
):
    server = NationServer()
    run = make(server, clubs=[CLUB, NATION], template=template(n4NationId="0"))
    report = run.execute()
    assert report["status"] == "completed", report
    assert report["eligibility_basis"] == MIXED_ELIGIBILITY_BASIS
    assert report["cards_by_club"] == {"가상클럽": 1, "가상국가": 1}
    lists = [form for resource, form in server.calls if resource == "player_list"]
    club_forms = [f for f in lists if f["n4TeamId"] == "9"]
    nation_forms = [f for f in lists if f["n4NationId"] == "27"]
    assert len(club_forms) + len(nation_forms) == len(lists) == 12
    assert all(f["n4LeagueId"] == "13" and f["n4NationId"] == "0" for f in club_forms)
    assert all(f["n4LeagueId"] == f["n4TeamId"] == "0" for f in nation_forms)
    colors = [
        json.loads(line)
        for line in (tmp_path / "out/team_color.jsonl").read_text().splitlines()
    ]
    assert {(c["name"], c["type"], c["spid"]) for c in colors} == {
        ("가상클럽", "club", CLUB_CARD),
        ("가상국가", "nation", NATION_CARD),
    }
    manifest = json.loads((tmp_path / "raw/manifest.json").read_text())
    assert manifest["nations"] == ["가상국가"]
    assert manifest["eligibility_basis"] == MIXED_ELIGIBILITY_BASIS


def test_a_nation_card_with_another_flag_stops_the_run(make):
    run = make(
        NationServer(flag=21), clubs=[CLUB, NATION], template=template(n4NationId="0")
    )
    report = run.execute()
    assert report["status"] == "stopped"
    assert report["stop_reason"]["kind"] == "nationality"


def test_nation_search_needs_a_clean_nation_key_in_the_saved_form(make):
    with pytest.raises(ValueError, match="n4NationId"):
        make(NationServer(), clubs=[NATION])
    with pytest.raises(ValueError, match="n4NationId=0"):
        validate_template(template(n4NationId="27"))
    with pytest.raises(ValueError):
        NationSpec("가상국가", 0)


def test_cli_accepts_nations_and_needs_at_least_one_team(tmp_path, capsys):
    argv = [
        "--club",
        "가상클럽:13:9",
        "--nation",
        "가상국가:27",
        "--root",
        str(tmp_path),
    ]
    assert main(argv) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["eligibility_basis"] == MIXED_ELIGIBILITY_BASIS
    assert plan["initial_list_requests"] == 2 * len(BANDS)
    for bad in ([], ["--nation", "가상국가"], ["--nation", "가상국가:x"]):
        with pytest.raises(SystemExit):
            main([*bad, "--root", str(tmp_path)])
    assert not (tmp_path / "data").exists()


def test_generated_observations_are_searchable_at_all_supported_grades(make, tmp_path):
    from FRS.core.candidate_search import (
        BatchSelection,
        CandidateQuery,
        candidate_search,
    )
    from FRS.schema.models import DOCUMENT_MODELS

    report = make(Server()).execute()
    assert report["status"] == "completed"
    names = {
        "cards": "player_card",
        "salaries": "card_salary",
        "prices": "card_price",
        "reinforcements": "reinforce_stats",
        "team_colors": "team_color",
    }
    data = {
        key: [
            DOCUMENT_MODELS[name].model_validate(json.loads(line))
            for line in (tmp_path / "out" / f"{name}.jsonl").read_text().splitlines()
        ]
        for key, name in names.items()
    }
    batches = BatchSelection(**dict.fromkeys(names.values(), report["run_id"]))
    for grade in range(5, 12):
        result = candidate_search(
            CandidateQuery(
                position=data["cards"][0].positions[0],
                salary_cap=310,
                price_cap=10**30,
                team_color=CLUB.name,
                reinforce_level=grade,
                when=OBSERVED + timedelta(hours=1),
                batches=batches,
            ),
            **data,
            supported_clubs=(CLUB.name,),
        )
        assert len(result.candidates) == 1, (grade, result.exclusion_counts)
