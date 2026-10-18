import socket

import pytest
import requests


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """기본 테스트에서 외부 요청과 실제 DB 접속을 금지한다."""

    def blocked(*args, **kwargs):
        raise AssertionError("단위 테스트에서는 네트워크를 사용할 수 없습니다.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
