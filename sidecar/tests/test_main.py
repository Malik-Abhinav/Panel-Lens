from main import handle


def test_ping_returns_pong() -> None:
    result = handle({"type": "ping", "request_id": "test-1"})

    assert result == {
        "protocol_version": 1,
        "request_id": "test-1",
        "status": "ok",
        "type": "pong",
    }

