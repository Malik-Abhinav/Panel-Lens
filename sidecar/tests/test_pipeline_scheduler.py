import threading
import time

import pytest

from pipeline_scheduler import InvalidJobTransition
from pipeline_scheduler import JobPriority
from pipeline_scheduler import JobState
from pipeline_scheduler import PipelineJob
from pipeline_scheduler import PipelineScheduler
from pipeline_scheduler import StageState
from performance_instrumentation import recorder


def make_job(
    name: str,
    priority: JobPriority,
    seen_ocr: list[str] | None = None,
    seen_translation: list[str] | None = None,
    ocr_work=None,
    translation_work=None,
    publish_work=None,
) -> PipelineJob:
    def ocr(job: PipelineJob):
        if seen_ocr is not None:
            seen_ocr.append(job.image_id)
        return ocr_work(job) if ocr_work else f"ocr-{name}"

    def translate(job: PipelineJob, result):
        if seen_translation is not None:
            seen_translation.append(job.image_id)
        return translation_work(job, result) if translation_work else f"translated-{result}"

    return PipelineJob(
        request_id=f"request-{name}",
        image_id=name,
        priority=priority,
        image_bytes=name.encode(),
        ocr_work=ocr,
        translation_work=translate,
        publish_work=publish_work,
    )


def wait_all(jobs: list[PipelineJob]) -> None:
    for job in jobs:
        job.wait(2)


def test_priority_order_is_p0_then_p1_then_p2_then_p3() -> None:
    gate = threading.Event()
    started = threading.Event()
    seen: list[str] = []
    scheduler = PipelineScheduler()
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        seen,
        ocr_work=lambda _: (started.set(), gate.wait(2))[1],
    )
    scheduler.submit(blocker)
    assert started.wait(1)
    jobs = [
        make_job("p3", JobPriority.P3, seen),
        make_job("p1", JobPriority.P1, seen),
        make_job("p2", JobPriority.P2, seen),
        make_job("p0", JobPriority.P0, seen),
    ]
    for job in jobs:
        scheduler.submit(job)
    gate.set()
    wait_all([blocker, *jobs])
    scheduler.shutdown()
    assert seen[1:] == ["p0", "p1", "p2", "p3"]


def test_same_priority_is_fifo() -> None:
    gate = threading.Event()
    started = threading.Event()
    seen: list[str] = []
    scheduler = PipelineScheduler()
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        seen,
        ocr_work=lambda _: (started.set(), gate.wait(2))[1],
    )
    scheduler.submit(blocker)
    assert started.wait(1)
    jobs = [make_job(name, JobPriority.P1, seen) for name in ("a", "b", "c")]
    for job in jobs:
        scheduler.submit(job)
    gate.set()
    wait_all([blocker, *jobs])
    scheduler.shutdown()
    assert seen[1:] == ["a", "b", "c"]


def test_reprioritization_moves_queued_p2_ahead() -> None:
    gate = threading.Event()
    started = threading.Event()
    seen: list[str] = []
    scheduler = PipelineScheduler()
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        seen,
        ocr_work=lambda _: (started.set(), gate.wait(2))[1],
    )
    scheduler.submit(blocker)
    assert started.wait(1)
    p1 = make_job("p1", JobPriority.P1, seen)
    p2 = make_job("p2", JobPriority.P2, seen)
    scheduler.submit(p1)
    scheduler.submit(p2)
    assert scheduler.reprioritize(p2.job_id, JobPriority.P0)
    gate.set()
    wait_all([blocker, p1, p2])
    scheduler.shutdown()
    assert seen[1:] == ["p2", "p1"]


def test_cancelled_queued_job_never_starts() -> None:
    gate = threading.Event()
    started = threading.Event()
    seen: list[str] = []
    scheduler = PipelineScheduler()
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        seen,
        ocr_work=lambda _: (started.set(), gate.wait(2))[1],
    )
    cancelled = make_job("cancelled", JobPriority.P1, seen)
    scheduler.submit(blocker)
    assert started.wait(1)
    scheduler.submit(cancelled)
    assert scheduler.cancel(cancelled.job_id)
    gate.set()
    blocker.wait(2)
    cancelled.wait(2)
    scheduler.shutdown()
    assert cancelled.state == JobState.CANCELLED
    assert "cancelled" not in seen


def test_stale_completed_result_cannot_be_published() -> None:
    accepted: list[str] = []
    scheduler = PipelineScheduler()

    def reject_stale(current: PipelineJob, _result) -> bool:
        is_current = False
        if is_current:
            accepted.append(current.image_id)
        return is_current

    job = make_job(
        "stale",
        JobPriority.P0,
        publish_work=reject_stale,
    )
    scheduler.submit(job)
    job.wait(2)
    scheduler.shutdown()
    assert job.state == JobState.CANCELLED
    assert accepted == []


def test_ocr_completion_enqueues_translation() -> None:
    translated: list[str] = []
    scheduler = PipelineScheduler()
    job = make_job("a", JobPriority.P0, seen_translation=translated)
    scheduler.submit(job)
    assert job.wait(2) == "translated-ocr-a"
    scheduler.shutdown()
    assert translated == ["a"]
    assert job.state == JobState.COMPLETE
    assert job.ocr_state == StageState.COMPLETE
    assert job.translation_state == StageState.COMPLETE


def test_ocr_b_overlaps_translation_a() -> None:
    translation_a_started = threading.Event()
    release_translation_a = threading.Event()
    ocr_b_started = threading.Event()
    scheduler = PipelineScheduler()

    a = make_job(
        "a",
        JobPriority.P0,
        translation_work=lambda *_: (
            translation_a_started.set(),
            release_translation_a.wait(2),
        )[-1],
    )
    b = make_job(
        "b",
        JobPriority.P0,
        ocr_work=lambda _: ocr_b_started.set() or "ocr-b",
    )
    scheduler.submit(a)
    assert translation_a_started.wait(1)
    scheduler.submit(b)
    assert ocr_b_started.wait(1)
    release_translation_a.set()
    wait_all([a, b])
    scheduler.shutdown()


def test_translation_worker_never_runs_ollama_concurrently() -> None:
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def translate(*_):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return "translated"

    scheduler = PipelineScheduler()
    jobs = [
        make_job(name, JobPriority.P0, translation_work=translate)
        for name in "abc"
    ]
    for job in jobs:
        scheduler.submit(job)
    wait_all(jobs)
    scheduler.shutdown()
    assert maximum_active == 1


def test_cancellation_after_ocr_starts_preserves_ocr_result() -> None:
    ocr_started = threading.Event()
    release_ocr = threading.Event()
    translated: list[str] = []
    scheduler = PipelineScheduler()
    job = make_job(
        "cancel-running",
        JobPriority.P0,
        ocr_work=lambda _: (ocr_started.set(), release_ocr.wait(2), "kept-ocr")[-1],
        translation_work=lambda *_: translated.append("unexpected"),
    )
    scheduler.submit(job)
    assert ocr_started.wait(1)
    assert scheduler.cancel(job.job_id)
    release_ocr.set()
    job.wait(2)
    scheduler.shutdown()
    assert job.state == JobState.CANCELLED
    assert job.ocr_result == "kept-ocr"
    assert translated == []


def test_bounded_ocr_queue_applies_backpressure() -> None:
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    third_submitted = threading.Event()
    scheduler = PipelineScheduler(capacity=1)
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        ocr_work=lambda _: (blocker_started.set(), release_blocker.wait(2))[-1],
    )
    queued = make_job("queued", JobPriority.P1)
    third = make_job("third", JobPriority.P1)
    scheduler.submit(blocker)
    assert blocker_started.wait(1)
    scheduler.submit(queued)

    submitter = threading.Thread(
        target=lambda: (scheduler.submit(third), third_submitted.set())
    )
    submitter.start()
    assert not third_submitted.wait(0.05)
    release_blocker.set()
    assert third_submitted.wait(1)
    submitter.join()
    wait_all([blocker, queued, third])
    scheduler.shutdown()


@pytest.mark.parametrize("failed_stage", ["ocr", "translation"])
def test_failure_does_not_deadlock_following_jobs(failed_stage: str) -> None:
    scheduler = PipelineScheduler()

    def fail(*_):
        raise RuntimeError(failed_stage)

    failed = make_job(
        "failed",
        JobPriority.P0,
        ocr_work=fail if failed_stage == "ocr" else None,
        translation_work=fail if failed_stage == "translation" else None,
    )
    healthy = make_job("healthy", JobPriority.P1)
    scheduler.submit(failed)
    scheduler.submit(healthy)
    with pytest.raises(RuntimeError, match=failed_stage):
        failed.wait(2)
    assert healthy.wait(2) == "translated-ocr-healthy"
    scheduler.shutdown()
    assert failed.state == JobState.FAILED
    assert healthy.state == JobState.COMPLETE


def test_invalid_state_transition_is_rejected() -> None:
    job = make_job("state", JobPriority.P0)
    with pytest.raises(InvalidJobTransition):
        job.transition(JobState.COMPLETE)


def test_shutdown_workers_exit_cleanly() -> None:
    scheduler = PipelineScheduler()
    scheduler.shutdown()
    assert not scheduler.workers_alive


def test_shutdown_drains_active_pipeline_cleanly() -> None:
    scheduler = PipelineScheduler(capacity=2)
    jobs = [make_job(name, JobPriority.P0) for name in "ab"]
    for job in jobs:
        scheduler.submit(job)
    scheduler.shutdown()
    assert [job.state for job in jobs] == [JobState.COMPLETE, JobState.COMPLETE]
    assert not scheduler.workers_alive


def test_scheduler_emits_queue_wait_worker_and_state_metrics() -> None:
    events: list[dict] = []
    original_enabled, original_sink = recorder.enabled, recorder._sink
    recorder.enabled, recorder._sink = True, events.append
    try:
        scheduler = PipelineScheduler()
        job = make_job("metrics", JobPriority.P2)
        scheduler.submit(job)
        job.wait(2)
        scheduler.shutdown()
    finally:
        recorder.enabled, recorder._sink = original_enabled, original_sink

    exits = [event for event in events if event["stage"] == "queue_exit"]
    assert {event["queue"] for event in exits} == {"ocr", "translation", "result"}
    assert all(event["queue_wait_ms"] >= 0 for event in exits)
    assert all(event["worker_id"] for event in exits)
    assert all(event["request_id"] == "request-metrics" for event in exits)
    assert set(job.queue_wait_by_stage_ms) == {"ocr", "translation", "result"}


def test_synthetic_priority_workload_avoids_speculative_work() -> None:
    gate = threading.Event()
    started = threading.Event()
    seen: list[str] = []
    scheduler = PipelineScheduler()
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        seen,
        ocr_work=lambda _: (started.set(), gate.wait(2))[1],
    )
    scheduler.submit(blocker)
    assert started.wait(1)
    jobs = [
        make_job("A", JobPriority.P2, seen),
        make_job("B", JobPriority.P2, seen),
        make_job("C", JobPriority.P0, seen),
        make_job("D", JobPriority.P1, seen),
        make_job("E", JobPriority.P3, seen),
    ]
    for job in jobs:
        scheduler.submit(job)
    gate.set()
    wait_all([blocker, *jobs])
    scheduler.shutdown()
    assert seen[1:] == ["C", "D", "A", "B", "E"]


def test_rapid_scroll_reprioritizes_only_queued_work() -> None:
    gate = threading.Event()
    started = threading.Event()
    seen: list[str] = []
    scheduler = PipelineScheduler()
    blocker = make_job(
        "blocker",
        JobPriority.P0,
        seen,
        ocr_work=lambda _: (started.set(), gate.wait(2))[1],
    )
    scheduler.submit(blocker)
    assert started.wait(1)
    a, b, c = [make_job(name, JobPriority.P1, seen) for name in "ABC"]
    for job in (a, b, c):
        scheduler.submit(job)
    assert scheduler.reprioritize(a.job_id, JobPriority.P3)
    assert scheduler.reprioritize(b.job_id, JobPriority.P0)
    assert scheduler.reprioritize(c.job_id, JobPriority.P0)
    gate.set()
    wait_all([blocker, a, b, c])
    scheduler.shutdown()
    assert seen[1:] == ["B", "C", "A"]
