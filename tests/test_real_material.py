"""실데이터 공급기의 명시 선택·해시 검증을 합성 실행 결과로 확인한다."""

import json
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from test_realdata_catalog import CLUB, Server, template

from FRS.agent.real_material import ObservedMaterial, RealMaterialError
from FRS.core.candidate_search.search import BatchSelection
from FRS.utils.crawling.realdata_catalog import RealDataRun

OBSERVED = datetime(2026, 10, 21, tzinfo=timezone.utc)
RUN_ID = "real-data-synthetic"


@pytest.fixture
def finished(tmp_path):
    ticks = count(0, 2)
    run = RealDataRun(
        tmp_path / "raw" / RUN_ID,
        tmp_path / "processed" / RUN_ID,
        Server(),
        run_id=RUN_ID,
        clubs=[CLUB],
        template=template(),
        max_attempts=50,
        allow_empty_cells=True,
        clock=lambda: OBSERVED + timedelta(seconds=next(ticks)),
    )
    assert run.execute()["status"] == "completed"
    return tmp_path / "processed"


def selection(run_id=RUN_ID):
    return BatchSelection(
        player_card=run_id,
        card_salary=run_id,
        card_price=run_id,
        reinforce_stats=run_id,
        team_color=run_id,
    )


def test_loads_the_explicit_run_in_the_synthetic_provider_shape(finished):
    material = ObservedMaterial(finished, RUN_ID)
    data = material.load(selection())
    assert set(data) == {"cards", "salaries", "prices", "reinforcements", "team_colors"}
    assert (len(data["cards"]), len(data["salaries"]), len(data["prices"])) == (1, 7, 7)
    assert len(data["reinforcements"]) == 6 and len(data["team_colors"]) == 1
    assert material.data_mode == "observed"


def test_batches_must_match_the_explicit_run(finished):
    material = ObservedMaterial(finished, RUN_ID)
    with pytest.raises(RealMaterialError):
        material.load(selection("real-data-other"))
    with pytest.raises(TypeError):
        material.load({"player_card": RUN_ID})


def test_tampered_or_unfinished_material_is_rejected(finished):
    path = finished / RUN_ID / "card_price.jsonl"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(RealMaterialError):
        ObservedMaterial(finished, RUN_ID)


def test_stopped_run_and_bad_ids_are_rejected(finished):
    report = finished / RUN_ID / "report.json"
    value = json.loads(report.read_text())
    report.write_text(json.dumps({**value, "status": "stopped"}))
    with pytest.raises(RealMaterialError):
        ObservedMaterial(finished, RUN_ID)
    for bad in ("latest", "../real-data-x", "synthetic"):
        with pytest.raises(RealMaterialError):
            ObservedMaterial(finished, bad)
