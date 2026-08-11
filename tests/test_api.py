from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import TestCase

from backend.server import AppContext, build_handler
from backend.storage import Storage
from backend.workers import ModelWorkerPool


class ApiTest(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        storage = Storage(f"{self.tmp.name}/test.sqlite3")
        workers = ModelWorkerPool(storage, 1)
        self.context = AppContext(storage=storage, workers=workers, public_dir=self.tmp_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.context))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.token = ""
        login = self.request(
            "/api/auth/login",
            method="POST",
            body={"username": "admin", "password": "admin"},
            auth=False,
        )
        self.token = login["token"]
        self.request("/api/balance/top-up", method="POST", body={"amount": 500})

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.context.workers.shutdown()
        self.tmp.cleanup()

    @property
    def tmp_path(self):
        from pathlib import Path

        return Path(self.tmp.name)

    def test_create_study_add_finding_and_run_analysis(self) -> None:
        created = self.request(
            "/api/studies",
            method="POST",
            body={"patient_name": "Иван Петров", "patient_id": "P-001", "birth_date": "1980-01-02"},
        )
        study_id = created["study"]["id"]
        self.assertEqual(created["study"]["birth_date"], "1980-01-02")

        finding = self.request(
            f"/api/studies/{study_id}/findings",
            method="POST",
            body={"title": "Ручная находка", "diameter_mm": 7.2, "confidence": 0.88},
        )
        self.assertEqual(finding["finding"]["source"], "manual")

        job = self.request(f"/api/studies/{study_id}/analyze", method="POST")["job"]
        for _ in range(20):
            loaded_job = self.request(f"/api/jobs/{job['id']}")["job"]
            if loaded_job["status"] == "done":
                break
            sleep(0.1)
        else:
            self.fail("analysis job did not finish")

        loaded = self.request(f"/api/studies/{study_id}")["study"]
        self.assertEqual(loaded["finding_count"], 2)

    def test_generate_report_fallback(self) -> None:
        created = self.request(
            "/api/studies",
            method="POST",
            body={"patient_name": "Иван Петров", "birth_date": "1980-01-02"},
        )
        result = self.request(
            "/api/reports/generate",
            method="POST",
            body={
                "study_id": created["study"]["id"],
                "patient_name": "Иван Петров",
                "birth_date": "1980-01-02",
                "slices_count": 3,
                "detections": [{"title": "Подозрительный объект", "sliceIndex": 1, "confidence": 0.9}],
            },
        )
        self.assertIn("Иван Петров", result["report"])
        self.assertEqual(result["study"]["status"], "reported")

    def test_health(self) -> None:
        health = self.request("/api/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["workers"], 1)
        self.assertEqual(health["service"], "lung-prometheus")

    def test_auth_register_top_up_and_balance_error(self) -> None:
        registered = self.request(
            "/api/auth/register",
            method="POST",
            body={"username": "doctor", "password": "secret"},
            auth=False,
        )
        admin_token = self.token
        self.token = registered["token"]

        with self.assertRaises(HTTPError) as error_context:
            self.request("/api/studies", method="POST", body={"patient_name": "Петр Иванов"})

        self.assertEqual(error_context.exception.code, 402)
        payload = json.loads(error_context.exception.read().decode("utf-8"))
        error_context.exception.close()
        self.assertEqual(payload["error"], "недостаточно средств")

        top_up = self.request("/api/balance/top-up", method="POST", body={"amount": 80})
        self.assertEqual(top_up["user"]["balance"], 80)

        created = self.request("/api/studies", method="POST", body={"patient_name": "Петр Иванов"})
        self.assertEqual(created["user"]["balance"], 0)
        self.assertEqual(created["study"]["patient_name"], "Петр Иванов")
        self.token = admin_token

    def test_viewer_analyze_returns_box_inside_image(self) -> None:
        result = self.request(
            "/api/viewer/analyze",
            method="POST",
            body={
                "slices": [
                    {"name": "slice-1.png", "width": 512, "height": 512},
                    {"name": "slice-2.png", "width": 512, "height": 512},
                ]
            },
        )

        detection = result["detections"][0]
        self.assertGreaterEqual(detection["sliceIndex"], 0)
        self.assertLess(detection["sliceIndex"], 2)
        self.assertGreater(detection["width"], 0)
        self.assertGreater(detection["height"], 0)
        self.assertLessEqual(detection["x"] + detection["width"], 512)
        self.assertLessEqual(detection["y"] + detection["height"], 512)

    def request(self, path: str, method: str = "GET", body: dict | None = None, auth: bool = True) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if auth and self.token:
            headers["X-Auth-Token"] = self.token
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
