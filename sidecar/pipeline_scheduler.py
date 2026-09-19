"""Bounded, priority-aware two-stage scheduler for PanelLens ML work."""

from __future__ import annotations

import hashlib
import heapq
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable

from performance_instrumentation import bind_request, emit


class JobPriority(IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3

    @classmethod
    def parse(cls, value: Any) -> "JobPriority":
        if isinstance(value, cls):
            return value
        aliases = {"visible": cls.P0, "prefetch": cls.P1}
        if str(value).casefold() in aliases:
            return aliases[str(value).casefold()]
        try:
            return cls[str(value).upper()]
        except (KeyError, ValueError) as error:
            raise ValueError(f"Invalid job priority: {value!r}") from error


class JobState(str, Enum):
    QUEUED = "queued"
    OCR_RUNNING = "ocr_running"
    OCR_COMPLETE = "ocr_complete"
    TRANSLATION_QUEUED = "translation_queued"
    TRANSLATION_RUNNING = "translation_running"
    COMPLETE = "complete"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CancellationState(str, Enum):
    ACTIVE = "active"
    REQUESTED = "requested"
    CANCELLED = "cancelled"


class StageState(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


_ALLOWED_TRANSITIONS = {
    JobState.QUEUED: {
        JobState.OCR_RUNNING,
        JobState.CANCEL_REQUESTED,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.OCR_RUNNING: {
        JobState.OCR_COMPLETE,
        JobState.CANCEL_REQUESTED,
        JobState.FAILED,
    },
    JobState.OCR_COMPLETE: {
        JobState.TRANSLATION_QUEUED,
        JobState.CANCEL_REQUESTED,
        JobState.CANCELLED,
    },
    JobState.TRANSLATION_QUEUED: {
        JobState.TRANSLATION_RUNNING,
        JobState.CANCEL_REQUESTED,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.TRANSLATION_RUNNING: {
        JobState.COMPLETE,
        JobState.CANCEL_REQUESTED,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.CANCEL_REQUESTED: {
        JobState.OCR_COMPLETE,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.COMPLETE: set(),
    JobState.CANCELLED: set(),
    JobState.FAILED: set(),
}


class InvalidJobTransition(RuntimeError):
    pass


@dataclass
class PipelineJob:
    request_id: str
    image_id: str
    priority: JobPriority
    image_bytes: bytes
    ocr_work: Callable[["PipelineJob"], Any]
    translation_work: Callable[["PipelineJob", Any], Any]
    publish_work: Callable[["PipelineJob", Any], bool] | None = None
    model_state: str = "unknown"
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at_ns: int = field(default_factory=time.monotonic_ns)
    state: JobState = JobState.QUEUED
    cancellation_state: CancellationState = CancellationState.ACTIVE
    ocr_state: StageState = StageState.QUEUED
    translation_state: StageState = StageState.PENDING
    content_identity: str = ""
    ocr_result: Any = None
    translation_result: Any = None
    error: BaseException | None = None
    failed_stage: str | None = None
    ocr_duration_ms: float = 0
    translation_duration_ms: float = 0
    queue_wait_ms: float = 0
    queue_wait_by_stage_ms: dict[str, float] = field(default_factory=dict)
    completed_at_ns: int | None = None
    _queue_entered_ns: dict[str, int] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        if not self.content_identity:
            self.content_identity = hashlib.sha256(self.image_bytes).hexdigest()

    def transition(self, target: JobState) -> None:
        with self._lock:
            previous = self.state
            if target not in _ALLOWED_TRANSITIONS[self.state]:
                raise InvalidJobTransition(f"{self.state.value} -> {target.value}")
            self.state = target
        emit(
            "job_state_transition",
            request_id=self.request_id,
            image_id=self.image_id,
            priority=self.priority.name,
            job_id=self.job_id,
            previous_state=previous.value,
            state=target.value,
        )

    def request_cancellation(self) -> bool:
        with self._lock:
            if self.state in {JobState.COMPLETE, JobState.CANCELLED, JobState.FAILED}:
                return False
            self.cancellation_state = CancellationState.REQUESTED
            if self.state != JobState.CANCEL_REQUESTED:
                self.transition(JobState.CANCEL_REQUESTED)
            return True

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self.cancellation_state == CancellationState.REQUESTED

    def wait(self, timeout: float | None = None) -> Any:
        if not self._done.wait(timeout):
            raise TimeoutError(f"Pipeline job {self.job_id} did not finish")
        if self.error is not None:
            raise self.error
        return self.translation_result

    def finish(self) -> None:
        self.completed_at_ns = time.monotonic_ns()
        self._done.set()


class _PriorityQueue:
    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = capacity
        self._condition = threading.Condition()
        self._heap: list[tuple[int, int, int, str]] = []
        self._jobs: dict[str, PipelineJob] = {}
        self._generation: dict[str, int] = {}
        self._sequence = 0
        self._closed = False

    def put(self, job: PipelineJob) -> None:
        with self._condition:
            self._condition.wait_for(
                lambda: len(self._jobs) < self.capacity or self._closed
            )
            if self._closed:
                raise RuntimeError(f"{self.name} queue is closed")
            self._sequence += 1
            generation = self._generation.get(job.job_id, 0) + 1
            self._generation[job.job_id] = generation
            self._jobs[job.job_id] = job
            heapq.heappush(
                self._heap,
                (int(job.priority), self._sequence, generation, job.job_id),
            )
            job._queue_entered_ns[self.name] = time.monotonic_ns()
            emit(
                "queue_enter",
                request_id=job.request_id,
                image_id=job.image_id,
                priority=job.priority.name,
                job_id=job.job_id,
                queue=self.name,
                queue_depth=len(self._jobs),
                state=job.state.value,
            )
            self._condition.notify_all()

    def get(self) -> PipelineJob | None:
        with self._condition:
            while True:
                while self._heap:
                    _, _, generation, job_id = heapq.heappop(self._heap)
                    if generation != self._generation.get(job_id):
                        continue
                    job = self._jobs.pop(job_id, None)
                    if job is None:
                        continue
                    entered = job._queue_entered_ns.pop(self.name, job.created_at_ns)
                    wait_ms = (time.monotonic_ns() - entered) / 1_000_000
                    job.queue_wait_ms += wait_ms
                    job.queue_wait_by_stage_ms[self.name] = (
                        job.queue_wait_by_stage_ms.get(self.name, 0.0) + wait_ms
                    )
                    emit(
                        "queue_exit",
                        request_id=job.request_id,
                        image_id=job.image_id,
                        priority=job.priority.name,
                        job_id=job.job_id,
                        queue=self.name,
                        queue_depth=len(self._jobs),
                        queue_wait_ms=round(wait_ms, 3),
                        worker_id=threading.current_thread().name,
                        state=job.state.value,
                    )
                    self._condition.notify_all()
                    return job
                if self._closed:
                    return None
                self._condition.wait()

    def reprioritize(self, job_id: str, priority: JobPriority) -> bool:
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.priority = priority
            self._sequence += 1
            generation = self._generation.get(job_id, 0) + 1
            self._generation[job_id] = generation
            heapq.heappush(
                self._heap,
                (int(priority), self._sequence, generation, job_id),
            )
            self._condition.notify_all()
            return True

    def remove(self, job_id: str) -> PipelineJob | None:
        with self._condition:
            job = self._jobs.pop(job_id, None)
            if job is not None:
                self._generation[job_id] = self._generation.get(job_id, 0) + 1
                self._condition.notify_all()
            return job

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class PipelineScheduler:
    """Exactly one OCR worker and one translation worker by default."""

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._ocr_queue = _PriorityQueue("ocr", capacity)
        self._translation_queue = _PriorityQueue("translation", capacity)
        self._result_queue = _PriorityQueue("result", capacity)
        self._jobs: dict[str, PipelineJob] = {}
        self._lock = threading.RLock()
        self._shutdown = False
        self._workers = [
            threading.Thread(target=self._ocr_loop, name="ocr-0", daemon=True),
            threading.Thread(
                target=self._translation_loop,
                name="translation-0",
                daemon=True,
            ),
            threading.Thread(target=self._result_loop, name="result-0", daemon=True),
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, job: PipelineJob) -> PipelineJob:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("scheduler is shut down")
            if job.job_id in self._jobs:
                raise ValueError(f"Duplicate job_id {job.job_id}")
            self._jobs[job.job_id] = job
        try:
            self._ocr_queue.put(job)
        except BaseException:
            with self._lock:
                self._jobs.pop(job.job_id, None)
            raise
        return job

    def reprioritize(self, job_id: str, new_priority: Any) -> bool:
        priority = JobPriority.parse(new_priority)
        changed = any(
            queue.reprioritize(job_id, priority)
            for queue in (self._ocr_queue, self._translation_queue, self._result_queue)
        )
        if changed:
            with self._lock:
                job = self._jobs.get(job_id)
            if job is not None:
                emit(
                    "reprioritized",
                    request_id=job.request_id,
                    image_id=job.image_id,
                    priority=job.priority.name,
                    job_id=job.job_id,
                    state=job.state.value,
                )
        return changed

    def cancel(self, job_id: str, reason: str = "obsolete") -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or not job.request_cancellation():
            return False
        emit(
            "cancel_requested",
            request_id=job.request_id,
            image_id=job.image_id,
            priority=job.priority.name,
            job_id=job.job_id,
            state=job.state.value,
            reason=reason,
        )
        removed = None
        for queue in (self._ocr_queue, self._translation_queue, self._result_queue):
            removed = queue.remove(job_id) or removed
        if removed is not None:
            self._mark_cancelled(job, "queued")
        return True

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._ocr_queue.close()
        if not wait:
            self._translation_queue.close()
            self._result_queue.close()
            return

        # Drain in pipeline order. Closing every queue at once can make OCR fail
        # while forwarding a completed item to an already-closed translation queue.
        self._workers[0].join()
        self._translation_queue.close()
        self._workers[1].join()
        self._result_queue.close()
        self._workers[2].join()

    @property
    def workers_alive(self) -> bool:
        return any(worker.is_alive() for worker in self._workers)

    def _ocr_loop(self) -> None:
        while (job := self._ocr_queue.get()) is not None:
            if job.cancellation_requested:
                self._mark_cancelled(job, "before_ocr")
                continue
            with bind_request(
                job.request_id, job.image_id, job.priority.name, job.model_state
            ):
                try:
                    job.transition(JobState.OCR_RUNNING)
                    job.ocr_state = StageState.RUNNING
                    started = time.monotonic_ns()
                    emit(
                        "ocr_start",
                        job_id=job.job_id,
                        worker_id="ocr-0",
                        state=job.state.value,
                    )
                    job.ocr_result = job.ocr_work(job)
                    job.ocr_state = StageState.COMPLETE
                    job.ocr_duration_ms = (time.monotonic_ns() - started) / 1_000_000
                    if job.cancellation_requested:
                        job.transition(JobState.OCR_COMPLETE)
                        emit(
                            "ocr_end",
                            job_id=job.job_id,
                            duration_ms=round(job.ocr_duration_ms, 3),
                            worker_id="ocr-0",
                            state=job.state.value,
                        )
                        self._mark_cancelled(job, "after_ocr")
                        continue
                    job.transition(JobState.OCR_COMPLETE)
                    emit(
                        "ocr_end",
                        job_id=job.job_id,
                        duration_ms=round(job.ocr_duration_ms, 3),
                        worker_id="ocr-0",
                        state=job.state.value,
                    )
                    job.transition(JobState.TRANSLATION_QUEUED)
                    job.translation_state = StageState.QUEUED
                    self._translation_queue.put(job)
                except BaseException as error:
                    job.ocr_state = StageState.FAILED
                    self._fail(job, error, "ocr")

    def _translation_loop(self) -> None:
        while (job := self._translation_queue.get()) is not None:
            if job.cancellation_requested:
                self._mark_cancelled(job, "before_translation")
                continue
            with bind_request(
                job.request_id, job.image_id, job.priority.name, job.model_state
            ):
                try:
                    job.transition(JobState.TRANSLATION_RUNNING)
                    job.translation_state = StageState.RUNNING
                    started = time.monotonic_ns()
                    emit(
                        "translation_start",
                        job_id=job.job_id,
                        worker_id="translation-0",
                        state=job.state.value,
                    )
                    job.translation_result = job.translation_work(job, job.ocr_result)
                    job.translation_state = StageState.COMPLETE
                    job.translation_duration_ms = (
                        time.monotonic_ns() - started
                    ) / 1_000_000
                    emit(
                        "translation_end",
                        job_id=job.job_id,
                        duration_ms=round(job.translation_duration_ms, 3),
                        worker_id="translation-0",
                        state=job.state.value,
                    )
                    if job.cancellation_requested:
                        emit(
                            "result_discarded",
                            job_id=job.job_id,
                            reason="after_translation",
                        )
                        self._mark_cancelled(job, "after_translation")
                        continue
                    self._result_queue.put(job)
                except BaseException as error:
                    job.translation_state = StageState.FAILED
                    self._fail(job, error, "translation")

    def _result_loop(self) -> None:
        while (job := self._result_queue.get()) is not None:
            with bind_request(
                job.request_id, job.image_id, job.priority.name, job.model_state
            ):
                if job.cancellation_requested:
                    emit(
                        "result_discarded",
                        job_id=job.job_id,
                        reason="before_publication",
                    )
                    self._mark_cancelled(job, "before_publication")
                    continue
                try:
                    publish = job.publish_work
                    if publish is not None and not publish(job, job.translation_result):
                        emit(
                            "result_discarded",
                            job_id=job.job_id,
                            reason="publisher_rejected",
                        )
                        job.request_cancellation()
                        self._mark_cancelled(job, "publisher_rejected")
                        continue
                    job.transition(JobState.COMPLETE)
                    self._finish(job)
                except BaseException as error:
                    self._fail(job, error, "result")

    def _mark_cancelled(self, job: PipelineJob, boundary: str) -> None:
        with job._lock:
            job.cancellation_state = CancellationState.CANCELLED
            if job.ocr_state in {StageState.PENDING, StageState.QUEUED}:
                job.ocr_state = StageState.CANCELLED
            if job.translation_state in {
                StageState.PENDING,
                StageState.QUEUED,
                StageState.RUNNING,
            }:
                job.translation_state = StageState.CANCELLED
            if job.state != JobState.CANCELLED:
                job.transition(JobState.CANCELLED)
        emit(
            "cancelled",
            request_id=job.request_id,
            image_id=job.image_id,
            priority=job.priority.name,
            job_id=job.job_id,
            state=job.state.value,
            boundary=boundary,
        )
        self._finish(job)

    def _fail(self, job: PipelineJob, error: BaseException, stage: str) -> None:
        with job._lock:
            job.error = error
            job.failed_stage = stage
            if job.state not in {JobState.FAILED, JobState.CANCELLED, JobState.COMPLETE}:
                job.transition(JobState.FAILED)
        emit(
            "pipeline_failed",
            request_id=job.request_id,
            image_id=job.image_id,
            priority=job.priority.name,
            job_id=job.job_id,
            state=job.state.value,
            failed_stage=stage,
            error_type=type(error).__name__,
        )
        self._finish(job)

    def _finish(self, job: PipelineJob) -> None:
        job.finish()
        with self._lock:
            self._jobs.pop(job.job_id, None)
