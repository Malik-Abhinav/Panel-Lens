import base64
from http import HTTPStatus

from http_server import SESSION_TOKEN, origin_allowed, request_authorized, route_request


def test_health_reuses_sidecar_ping() -> None:
    messages: list[dict[str, object]] = []

    def pipeline(message: dict[str, object]) -> dict[str, object]:
        messages.append(message)
        return {"status": "ok", "type": "pong"}

    status, result = route_request("GET", "/v1/health", pipeline_handler=pipeline)

    assert status == HTTPStatus.OK
    assert result["type"] == "pong"
    assert messages == [{"type": "ping", "request_id": "http-health"}]


def test_translate_maps_extension_request_to_existing_pipeline() -> None:
    messages: list[dict[str, object]] = []

    def pipeline(message: dict[str, object]) -> dict[str, object]:
        messages.append(message)
        return {"status": "ok", "type": "translation", "regions": [], "cache_hit": True}

    image = base64.b64encode(b"image").decode("ascii")
    status, result = route_request(
        "POST",
        "/v1/images/translate",
        {
            "request_id": "request-1",
            "session_id": "tab:chapter",
            "image_id": "panel-1",
            "image_base64": image,
            "priority": "prefetch",
            "series": "Example",
        },
        pipeline,
    )

    assert status == HTTPStatus.OK
    assert result["image_id"] == "panel-1"
    assert result["cached"] is True
    assert messages == [
        {
            "type": "translate",
            "request_id": "request-1",
            "image_id": "panel-1",
            "priority": "prefetch",
            "image_base64": image,
            "series": "Example",
            "context": [],
        }
    ]


def test_translate_rejects_missing_fields_and_invalid_priority() -> None:
    status, result = route_request("POST", "/v1/images/translate", {})
    assert status == HTTPStatus.BAD_REQUEST
    assert result["error"]["code"] == "invalid_request"

    status, result = route_request(
        "POST",
        "/v1/images/translate",
        {
            "request_id": "request-1",
            "session_id": "session",
            "image_id": "panel",
            "image_base64": "",
            "priority": "entire-chapter",
        },
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert result["error"]["code"] == "invalid_priority"


def test_unknown_endpoint_is_rejected() -> None:
    status, result = route_request("GET", "/anything")
    assert status == HTTPStatus.NOT_FOUND
    assert result["error"]["code"] == "not_found"


def test_local_transport_requires_extension_origin_and_session_token() -> None:
    assert origin_allowed("chrome-extension://abcdefghijklmnopabcdefghijklmnop")
    assert not origin_allowed("https://hostile.example")
    assert request_authorized("chrome-extension://abcdefghijklmnopabcdefghijklmnop", SESSION_TOKEN)
    assert not request_authorized("chrome-extension://abcdefghijklmnopabcdefghijklmnop", "wrong")
    assert not request_authorized("https://hostile.example", SESSION_TOKEN)


def test_request_control_routes_cancel_and_reprioritize() -> None:
    actions = []
    status, response = route_request(
        "POST", "/v1/requests/request-1/reprioritize", {"priority": "P0"},
        reprioritize_handler=lambda request_id, priority: actions.append((request_id, priority)) or True,
    )
    assert status == HTTPStatus.OK
    assert response["changed"] is True
    assert actions == [("request-1", "P0")]

    status, response = route_request(
        "POST", "/v1/requests/request-1/cancel", {},
        cancel_handler=lambda request_id: actions.append((request_id, "cancel")) or True,
    )
    assert status == HTTPStatus.OK
    assert response["changed"] is True


def test_batch_endpoint_maps_three_images_to_batch_handler() -> None:
    messages: list[dict[str, object]] = []

    def batch(message: dict[str, object]) -> dict[str, object]:
        messages.append(message)
        return {"status": "ok", "type": "translation_batch", "images": []}

    images = [
        {"image_id": f"image-{index}", "image_base64": "aW1hZ2U="}
        for index in range(3)
    ]
    status, result = route_request(
        "POST",
        "/v1/images/translate-batch",
        {
            "request_id": "batch-request",
            "priority": "visible",
            "session_id": "chapter",
            "images": images,
            "series": "Example",
            "context": [{"korean": "이전", "english": "Previous"}],
            "max_image_width": 1400,
        },
        batch_handler=batch,
    )

    assert status == HTTPStatus.OK
    assert result["type"] == "translation_batch"
    assert messages == [
        {
            "request_id": "batch-request",
            "priority": "visible",
            "images": images,
            "series": "Example",
            "context": [{"korean": "이전", "english": "Previous"}],
            "max_image_width": 1400,
        }
    ]
