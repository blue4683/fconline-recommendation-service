"""진단의 배치 검증 경계는 합성 문서와 검증기 대역으로 확인한다."""

import hashlib
import json

import pytest
from test_model_input_contract import complete

from FRS.core.position_recommend.data_contract import InputContractError
from FRS.diagnostics import model_input_report as diagnostic


@pytest.fixture
def saved(tmp_path, monkeypatch):
    batch = diagnostic.PILOT_ID
    raw = tmp_path / "data/raw" / batch
    processed = tmp_path / "data/processed" / batch
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)
    (raw / "manifest.json").write_text(
        json.dumps({"pilot_id": batch, "status": "completed"})
    )
    c = complete(batch_id=batch, status="unknown", valid_until=None)
    (processed / "player_card.jsonl").write_text(c.model_dump_json() + "\n")
    calls = []
    monkeypatch.setattr(diagnostic, "verify_pilot", lambda r, p: calls.append((r, p)))
    return tmp_path, raw, processed, c, calls


def test_report_verifies_explicit_completed_source_and_performs_no_training(saved):
    root, raw, processed, c, calls = saved
    data = diagnostic.load_verified_cards(root, batch_ids=[c.batch_id])
    result = diagnostic.report(data)
    assert calls == [(raw, processed)]
    assert result["cards"] == 1 and result["labeled_rows"] == 2
    assert result["classes"] == ["CF", "ST"]
    assert result["card_groups"] == result["persons"] == 1
    assert result["source_status_counts"] == {"unknown": 1}
    assert (
        result["source_hashes"][c.batch_id]["manifest_sha256"]
        == hashlib.sha256((raw / "manifest.json").read_bytes()).hexdigest()
    )
    assert result["network_requests_sent"] == 0
    assert not any(
        result[key]
        for key in (
            "training_executed",
            "model_loaded",
            "split_performed",
            "observations_modified",
        )
    )
    assert diagnostic.report(data) == result
    assert "spid" not in json.dumps(result)


@pytest.mark.parametrize(
    "ids", [[], ["../other"], [diagnostic.PILOT_ID, diagnostic.PILOT_ID]]
)
def test_unapproved_or_duplicate_batch_ids_are_rejected_without_io(tmp_path, ids):
    with pytest.raises(InputContractError):
        diagnostic.load_verified_cards(tmp_path, batch_ids=ids)


def test_incomplete_source_is_rejected_before_verifier(saved):
    root, raw, _, c, calls = saved
    (raw / "manifest.json").write_text(
        json.dumps({"pilot_id": c.batch_id, "status": "stopped"})
    )
    with pytest.raises(InputContractError, match="완료"):
        diagnostic.load_verified_cards(root, batch_ids=[c.batch_id])
    assert calls == []


def test_verifier_error_prevents_model_input(saved, monkeypatch):
    root, _, _, c, _ = saved

    def reject(*args):
        raise ValueError("원본 해시 불일치")

    monkeypatch.setattr(diagnostic, "verify_pilot", reject)
    with pytest.raises(ValueError, match="원본"):
        diagnostic.load_verified_cards(root, batch_ids=[c.batch_id])


def test_source_change_during_verification_is_rejected(saved, monkeypatch):
    root, raw, _, c, _ = saved

    def change(*args):
        (raw / "manifest.json").write_text(
            json.dumps({"pilot_id": c.batch_id, "status": "completed", "changed": True})
        )

    monkeypatch.setattr(diagnostic, "verify_pilot", change)
    with pytest.raises(InputContractError, match="바뀌"):
        diagnostic.load_verified_cards(root, batch_ids=[c.batch_id])


def test_wrong_batch_in_processed_card_is_rejected(saved):
    root, _, processed, c, _ = saved
    other = complete(batch_id="another")
    (processed / "player_card.jsonl").write_text(other.model_dump_json() + "\n")
    with pytest.raises(InputContractError, match="배치"):
        diagnostic.load_verified_cards(root, batch_ids=[c.batch_id])
