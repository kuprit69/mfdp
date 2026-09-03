from __future__ import annotations

import logging
from queue import Empty, Queue
from threading import Event, Thread
from time import sleep
from typing import Callable

try:
    from .slice_store import SliceStore
    from .storage import Storage
except ImportError:  # pragma: no cover - used when backend/server.py is run directly.
    from slice_store import SliceStore
    from storage import Storage


logger = logging.getLogger(__name__)


class MessageQueue:
    def __init__(self, redis_url: str = "", queue_name: str = "lung-prometheus:analysis") -> None:
        self.queue_name = queue_name
        self.local: Queue[str | None] = Queue()
        self.redis_client = None
        if redis_url:
            for attempt in range(10):
                try:
                    import redis

                    self.redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
                    self.redis_client.ping()
                    break
                except Exception as exc:  # noqa: BLE001 - retry loop, logged below.
                    self.redis_client = None
                    # Expected during normal startup (e.g. docker-compose still
                    # bringing the Redis container up) - not worth more than
                    # a debug note per attempt.
                    logger.debug("redis connection attempt %s/10 failed: %s", attempt + 1, exc)
                    sleep(0.5)
            if self.redis_client is None:
                logger.warning(
                    "could not connect to redis at %s after 10 attempts - "
                    "falling back to the in-process queue (jobs won't survive a restart)",
                    redis_url,
                )

    @property
    def backend(self) -> str:
        return "redis" if self.redis_client is not None else "local"

    def enqueue(self, job_id: str) -> None:
        if self.redis_client is not None:
            self.redis_client.lpush(self.queue_name, job_id)
            return
        self.local.put(job_id)

    def dequeue(self, timeout: int = 1) -> str | None:
        if self.redis_client is not None:
            item = self.redis_client.brpop(self.queue_name, timeout=timeout)
            return str(item[1]) if item else None
        try:
            return self.local.get(timeout=timeout)
        except Empty:
            return None

    def wake_workers(self, count: int) -> None:
        if self.redis_client is None:
            for _ in range(count):
                self.local.put(None)


class ModelWorkerPool:
    """Runs queued analysis jobs against the real nodule-detection model.

    Each job's study must have slices persisted via ``SliceStore`` before it
    is enqueued (the API layer is responsible for that) - the worker loads
    them, runs ``NoduleModel.analyze()`` on the actual pixel data, and stores
    real detections as findings. There is no synthetic/fake fallback here:
    if slices aren't available the job fails with a clear error instead of
    inventing a result.
    """

    def __init__(
        self,
        storage: Storage,
        worker_count: int = 1,
        redis_url: str = "",
        slice_store: SliceStore | None = None,
        model_factory: Callable[[], object] | None = None,
    ) -> None:
        self.storage = storage
        self.worker_count = max(1, int(worker_count))
        self.queue = MessageQueue(redis_url=redis_url)
        self.stop_event = Event()
        self.slice_store = slice_store or SliceStore()
        # A fresh model instance per job keeps worker threads from sharing
        # mutable model/device state; the checkpoint is small so reloading it
        # is cheap next to running inference over a whole CT volume.
        self.model_factory = model_factory or self._default_model_factory
        self.storage.recover_active_jobs()
        for job_id in self.storage.pending_job_ids():
            self.queue.enqueue(job_id)

        self.threads = [
            Thread(target=self._work, name=f"model-worker-{index + 1}", daemon=True)
            for index in range(self.worker_count)
        ]
        for thread in self.threads:
            thread.start()
        logger.info(
            "model worker pool started: %s worker(s), queue backend=%s",
            self.worker_count,
            self.queue.backend,
        )

    @property
    def queue_backend(self) -> str:
        return self.queue.backend

    def enqueue(self, job_id: str) -> None:
        self.queue.enqueue(job_id)

    def shutdown(self) -> None:
        self.stop_event.set()
        self.queue.wake_workers(self.worker_count)
        for thread in self.threads:
            thread.join(timeout=1)

    @staticmethod
    def _default_model_factory() -> object:
        try:
            from .model_adapter import NoduleModel
        except ImportError:
            from model_adapter import NoduleModel
        return NoduleModel()

    def _work(self) -> None:
        while not self.stop_event.is_set():
            job_id = self.queue.dequeue(timeout=1)
            if job_id is None:
                continue

            try:
                job = self.storage.claim_job(job_id)
                if job is None:
                    continue
                study = self.storage.get_study(job["study_id"])
                self._run_analysis(job_id, study)
            except Exception as exc:  # noqa: BLE001 - job errors should be visible in API.
                logger.exception("analysis job %s failed", job_id)
                try:
                    self.storage.mark_job_failed(job_id, str(exc))
                except Exception:
                    logger.exception("failed to record failure for job %s", job_id)

    def _run_analysis(self, job_id: str, study: dict) -> None:
        slices = self.slice_store.load(study["id"])
        if not slices:
            raise ValueError(
                "Для этого исследования не сохранены срезы - повторите загрузку файла, "
                "чтобы модель могла проанализировать снимок."
            )

        model = self.model_factory()
        detections = model.analyze({"slices": slices})

        findings = []
        for detection in detections:
            segment = detection.get("segment") or {}
            finding = self.storage.create_finding(
                study_id=study["id"],
                title=detection.get("title") or "Подозрительный объект",
                diameter_mm=float(detection.get("diameterMm") or 6.0),
                confidence=float(detection.get("confidence") or detection.get("probability") or 0.0),
                source="model",
                slice_index=detection.get("sliceIndex"),
                x=detection.get("x"),
                y=detection.get("y"),
                width=detection.get("width"),
                height=detection.get("height"),
                segment_label=segment.get("label") if isinstance(segment, dict) else None,
                model_name=detection.get("modelName"),
                threshold=detection.get("threshold"),
            )
            findings.append(finding)

        self.storage.mark_job_done(
            job_id,
            {
                "findings": findings,
                "model": {
                    "name": getattr(model, "model_name", "3D CNN"),
                    "probability": getattr(model, "last_probability", None),
                    "threshold": getattr(model, "last_threshold", None),
                    "weights_loaded": getattr(model, "weights_loaded", False),
                },
            },
        )
