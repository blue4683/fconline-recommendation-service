import importlib
from unittest.mock import Mock

import pytest
import requests

http = importlib.import_module("FRS.utils.crawling.get_response")
ITEM = ("https://example.invalid/player?id=synthetic", "fake-1", "가상선수_0001")


@pytest.fixture
def transport(monkeypatch):
    get = Mock()
    sleeps = Mock()
    monkeypatch.setattr(http.requests, "get", get)
    monkeypatch.setattr(http.time, "sleep", sleeps)
    return get, sleeps


def response(status=200, content=b"synthetic"):
    return Mock(status_code=status, content=content)


def test_default_identification_and_request_contract(transport):
    get, sleeps = transport
    page = response()
    get.return_value = page
    assert http.fetch_url(ITEM) == (page, ITEM[1], ITEM[2])
    get.assert_called_once_with(ITEM[0], timeout=(5.0, 20.0), allow_redirects=False)
    sleeps.assert_called_once_with(1.0)


@pytest.mark.parametrize("status", [403, 429])
def test_block_stops_all_remaining_chunks_without_retry(transport, status):
    get, _ = transport
    get.return_value = response(status)
    with pytest.raises(http.CollectionStopped) as caught:
        http.get_response([[ITEM, ITEM], [ITEM]])
    assert caught.value.failure["kind"] == "blocked"
    assert caught.value.failure["status_code"] == status
    assert get.call_count == 1


@pytest.mark.parametrize(
    ("error", "kind"),
    [(requests.Timeout(), "timeout"), (requests.ConnectionError(), "connection_error")],
)
def test_network_failure_stops_without_retry(transport, error, kind):
    get, _ = transport
    get.side_effect = error
    with pytest.raises(http.CollectionStopped) as caught:
        http.get_response([[ITEM, ITEM]])
    assert caught.value.failure["kind"] == kind
    assert get.call_count == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_temporary_http_error_has_bounded_retry(transport, status):
    get, sleeps = transport
    get.return_value = response(status)
    page, failure = http.fetch_url(ITEM)
    assert page is None
    assert failure == {
        "id": ITEM[1],
        "name": ITEM[2],
        "kind": "http_error",
        "status_code": status,
    }
    assert get.call_count == 2
    assert [call.args[0] for call in sleeps.call_args_list] == [1.0, 2.0, 1.0]


def test_success_after_retry(transport):
    get, _ = transport
    page = response()
    get.side_effect = [response(503), page]
    assert http.fetch_url(ITEM)[0] is page


def test_not_found_is_recorded_and_next_item_runs(transport):
    get, _ = transport
    page = response()
    get.side_effect = [response(404), page]
    pages, failures, elapsed = http.get_response([[ITEM], [ITEM]])
    assert pages == [(page, ITEM[1], ITEM[2])]
    assert failures[0]["kind"] == "not_found"
    assert elapsed >= 0
    assert get.call_count == 2


@pytest.mark.parametrize(
    "status,content", [(200, b"  "), (302, b"redirect"), (500, b"error")]
)
def test_unexpected_response_stops(transport, status, content):
    get, _ = transport
    get.return_value = response(status, content)
    with pytest.raises(http.CollectionStopped, match="unexpected_response"):
        http.fetch_url(ITEM)
    assert get.call_count == 1


def test_slow_response_stops_before_following_request(transport, monkeypatch):
    get, _ = transport
    get.return_value = response()
    monkeypatch.setattr(http.time, "monotonic", Mock(side_effect=[0.0, 31.0]))
    with pytest.raises(http.CollectionStopped, match="slow_response"):
        http.get_response_one([ITEM, ITEM])
    assert get.call_count == 1


@pytest.mark.parametrize(
    "settings",
    [
        {"read_timeout": 0},
        {"request_interval": -1},
        {"retry_backoff": float("inf")},
        {"max_response_seconds": float("nan")},
        {"max_retries": -1},
        {"max_retries": 0.5},
        {"max_retries": True},
    ],
)
def test_invalid_policy_is_rejected(settings):
    with pytest.raises(ValueError):
        http.RequestPolicy(**settings)


def test_empty_batch_makes_no_requests(transport):
    get, _ = transport
    pages, failed, _ = http.get_response([])
    assert pages == failed == []
    get.assert_not_called()
