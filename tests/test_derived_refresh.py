"""파생 배치는 원래 관측 시각·원본을 보존하고 오프라인으로 재검증한다."""

import hashlib
import json
from datetime import timedelta

import pytest
import test_refresh_execution
from test_refresh_execution import OBSERVED

from FRS.utils.crawling import derive_refresh as derived
from FRS.utils.crawling.pilot import PilotStopped

BATCH = "stage-1-f-derived-synthetic"


@pytest.fixture
def refresh_run(tmp_path, monkeypatch):
    return test_refresh_execution.run.__wrapped__(tmp_path, monkeypatch)


def evidence(refresh_run):
    refresh_run.execute()
    catalogs, profiles = [], []
    for entry in refresh_run.state["responses"]:
        pair = (refresh_run.raw_path / entry["path"]).read_bytes(), entry
        (catalogs if entry["resource"] == "player_list" else profiles).append(pair)
    return [123000001], catalogs, profiles, {"synthetic-source": "fixed-hash"}


def test_derivation_preserves_source_time_and_never_extends_expiration(refresh_run):
    targets, catalogs, profiles, _ = evidence(refresh_run)
    before = json.dumps(refresh_run.state, sort_keys=True)
    records = derived.build_records(
        batch_id=BATCH,
        targets=targets,
        catalogs=catalogs,
        profiles=profiles,
        at=OBSERVED + timedelta(days=8),
    )
    assert {key: len(values) for key, values in records.items()} == {
        "card_price": 7,
        "team_color": 1,
        "player_card": 1,
        "card_salary": 7,
        "reinforce_stats": 7,
    }
    for key, values in records.items():
        for record in values:
            assert record.batch_id == BATCH
            assert record.status == ("unknown" if key == "reinforce_stats" else "stale")
            period = timedelta(hours=24) if key == "card_price" else timedelta(days=7)
            expected = record.observed_at + period
            if key == "reinforce_stats":
                expected = min(expected, records["player_card"][0].valid_until)
            assert record.valid_until == expected
            assert record.observed_at < OBSERVED + timedelta(hours=1)
    assert json.dumps(refresh_run.state, sort_keys=True) == before


@pytest.mark.parametrize("change", ["hash", "missing", "form", "target"])
def test_derivation_rejects_unverified_or_incomplete_sources(refresh_run, change):
    targets, catalogs, profiles, _ = evidence(refresh_run)
    if change == "hash":
        body, entry = profiles[0]
        profiles[0] = body + b"changed", entry
    elif change == "missing":
        profiles.pop()
    elif change == "form":
        profiles[0][1]["form"]["n1Strong"] = "11"
    else:
        targets.append(123000002)
    with pytest.raises((PilotStopped, ValueError)):
        derived.build_records(
            batch_id=BATCH,
            targets=targets,
            catalogs=catalogs,
            profiles=profiles,
            at=OBSERVED + timedelta(hours=1),
        )


def test_saved_derived_batch_rechecks_sources_and_rejects_rehashed_tampering(
    refresh_run,
    tmp_path,
    monkeypatch,
):
    sources = evidence(refresh_run)
    monkeypatch.setattr(derived, "source_records", lambda root: sources)
    state = derived.derive(tmp_path, BATCH, at=OBSERVED + timedelta(hours=1))
    assert state["source_mode"] == "derived" and state["network_requests_sent"] == 0
    assert state["auto_activation"] is False
    result = derived.verify(tmp_path, BATCH, expected_targets=sources[0])
    assert result["card_price"][0].status == "ok"
    with pytest.raises(FileExistsError):
        derived.derive(tmp_path, BATCH, at=OBSERVED + timedelta(hours=2))
    path = tmp_path / "data/processed" / BATCH / "card_salary.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["salary"] += 1
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    state["processed_hashes"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "data/raw" / BATCH / "manifest.json"
    manifest.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="파생"):
        derived.verify(tmp_path, BATCH, expected_targets=sources[0])


@pytest.mark.parametrize("batch", ["../invalid", "stage-1-f-derived-../bad", "other"])
def test_invalid_derived_id_is_rejected_before_loading_sources(
    tmp_path, monkeypatch, batch
):
    def forbidden(root):
        pytest.fail("잘못된 ID로 원본을 읽으면 안 됩니다.")

    monkeypatch.setattr(derived, "source_records", forbidden)
    with pytest.raises(PilotStopped):
        derived.derive(tmp_path, batch, at=OBSERVED)
