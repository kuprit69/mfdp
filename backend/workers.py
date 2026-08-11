from __future__ import annotations

import hashlib
from queue import Queue
from threading import Thread
from time import sleep

try:
    from .storage import Storage
except ImportError:  # pragma: no cover - used when running backend/server.py directly.
    from storage import Storage


class ModelWorkerPool:
    def __init__(self, storage: Storage, worker_count: int = 1) -> None:
        self.storage = storage
        self.worker_count = max(1, int(worker_count))
        self.queue: Queue[str | None] = Queue()
        self.threads = [
            Thread(target=self._work, name=f"model-worker-{index + 1}", daemon=True)
            for index in range(self.worker_count)
        ]
        for thread in self.threads:
            thread.start()

    def enqueue(self, job_id: str) -> None:
        self.queue.put(job_id)

    def shutdown(self) -> None:
        for _ in self.threads:
            self.queue.put(None)
        for thread in self.threads:
            thread.join(timeout=1)

    def _work(self) -> None:
        while True:
            job_id = self.queue.get()
            if job_id is None:
                self.queue.task_done()
                return

            try:
                self.storage.mark_job_running(job_id)
                job = self.storage.get_job(job_id)
                study = self.storage.get_study(job["study_id"])
                result = self._simple_model(study)
                finding = self.storage.create_finding(
                    study_id=study["id"],
                    title=result["title"],
                    diameter_mm=result["diameter_mm"],
                    confidence=result["confidence"],
                    source="model",
                )
                self.storage.mark_job_done(job_id, {"finding": finding})
            except Exception as exc:  # noqa: BLE001 - job errors should be visible in API.
                self.storage.mark_job_failed(job_id, str(exc))
            finally:
                self.queue.task_done()

    @staticmethod
    def _simple_model(study: dict) -> dict:
        # This is a deterministic demo model for the MVP. A real CNN can replace this function.
        sleep(0.2)
        seed_text = "|".join(
            [
                study["id"],
                study["patient_name"],
                study.get("patient_id", ""),
                study.get("description", ""),
            ]
        )
        digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
        diameter = round(4 + digest[0] / 255 * 18, 1)
        confidence = round(0.7 + digest[1] / 255 * 0.25, 2)
        return {
            "title": "Вероятный легочный узел",
            "diameter_mm": diameter,
            "confidence": confidence,
        }
