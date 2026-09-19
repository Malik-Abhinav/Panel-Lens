from performance_instrumentation import PerformanceRecorder
from performance_instrumentation import bind_request
import performance_instrumentation

from main import handle


def test_events_include_correlation_fields_and_monotonic_timestamps() -> None:
    events: list[dict[str, object]] = []
    ticks = iter([100, 200])
    recorder = PerformanceRecorder(True, events.append, lambda: next(ticks))

    with bind_request("request-1", "image-1", "prefetch", "warm"):
        recorder.emit("queue_enter", queue_depth=2)
        recorder.emit("queue_exit", duration_ms=0.1)

    assert [event["stage"] for event in events] == ["queue_enter", "queue_exit"]
    assert all(event["request_id"] == "request-1" for event in events)
    assert all(event["image_id"] == "image-1" for event in events)
    assert all(event["priority"] == "prefetch" for event in events)
    assert all(event["model_state"] == "warm" for event in events)
    assert events[1]["timestamp_ns"] > events[0]["timestamp_ns"]


def test_disabled_instrumentation_generates_no_events() -> None:
    events: list[dict[str, object]] = []
    recorder = PerformanceRecorder(False, events.append)

    with bind_request("request-1", "image-1"):
        assert recorder.emit("ocr_start") is None

    assert events == []


def test_span_uses_monotonic_duration() -> None:
    events: list[dict[str, object]] = []
    ticks = iter([1_000_000, 2_000_000, 6_000_000, 7_000_000])
    recorder = PerformanceRecorder(True, events.append, lambda: next(ticks))

    with bind_request("request-2", "image-2"):
        with recorder.span("decode"):
            pass

    assert events[-1]["stage"] == "decode_end"
    assert events[-1]["duration_ms"] == 5.0


def test_pipeline_events_preserve_request_and_image_ids() -> None:
    events: list[dict[str, object]] = []
    original_enabled = performance_instrumentation.recorder.enabled
    original_sink = performance_instrumentation.recorder._sink
    performance_instrumentation.recorder.enabled = True
    performance_instrumentation.recorder._sink = events.append
    try:
        result = handle(
            {
                "type": "translate",
                "request_id": "pipeline-request",
                "image_id": "pipeline-image",
                "priority": "prefetch",
                "image_base64": "aW1hZ2U=",
            },
            ocr_handler=lambda _: [],
            bubble_handler=lambda _, regions: (regions, 0),
            translation_handler=lambda regions, *_: regions,
        )
    finally:
        performance_instrumentation.recorder.enabled = original_enabled
        performance_instrumentation.recorder._sink = original_sink

    assert result["image_id"] == "pipeline-image"
    assert {event["request_id"] for event in events} == {"pipeline-request"}
    assert {event["image_id"] for event in events} == {"pipeline-image"}
    assert "ocr_start" in {event["stage"] for event in events}
