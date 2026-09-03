from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from unittest import TestCase

import numpy as np
from fastapi.testclient import TestClient

from backend.model_adapter import ModelConfig, NoduleModel
from backend.server import AppContext, create_app
from backend.slice_store import SliceStore
from backend.storage import Storage
from backend.workers import ModelWorkerPool


def synthetic_nodule_slices() -> list[dict]:
    """Slice payload with a bright, clearly lung-bound nodule baked in - the
    same shape the browser sends after parsing a DICOM/MHD upload."""
    volume = np.full((32, 64, 64), -750, dtype=np.int16)
    z_grid, y_grid, x_grid = np.ogrid[:32, :64, :64]
    nodule_mask = (z_grid - 16) ** 2 + (y_grid - 32) ** 2 + (x_grid - 32) ** 2 < 8 ** 2
    volume[nodule_mask] = 80
    return [
        {
            "name": f"slice-{index}",
            "rows": 64,
            "columns": 64,
            "width": 64,
            "height": 64,
            "pixelData": {
                "dtype": "Int16Array",
                "data": base64.b64encode(slice_pixels.tobytes()).decode("ascii"),
            },
        }
        for index, slice_pixels in enumerate(volume)
    ]


class ApiTest(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        storage = Storage(f"{self.tmp.name}/test.sqlite3")
        slice_store = SliceStore(f"{self.tmp.name}/slices")
        workers = ModelWorkerPool(storage, 1, slice_store=slice_store)
        self.context = AppContext(
            storage=storage,
            workers=workers,
            public_dir=self.tmp_path,
            slice_store=slice_store,
        )
        self.client = TestClient(create_app(context=self.context))
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
        self.client.close()
        self.context.workers.shutdown()
        self.context.storage.close()
        self.tmp.cleanup()

    @property
    def tmp_path(self) -> Path:
        return Path(self.tmp.name)

    def test_create_study_add_finding_and_run_analysis(self) -> None:
        created = self.request(
            "/api/studies",
            method="POST",
            body={"patient_name": "Иван Петров", "patient_id": "P-001", "birth_date": "1980-01-02"},
        )
        study_id = created["study"]["id"]
        self.assertEqual(created["study"]["birth_date"], "1980-01-02")
        self.assertFalse(created["study"]["has_slices"])

        finding = self.request(
            f"/api/studies/{study_id}/findings",
            method="POST",
            body={"title": "Ручная находка", "diameter_mm": 7.2, "confidence": 0.88},
        )
        self.assertEqual(finding["finding"]["source"], "manual")

        # The queued job must run against the real model, so it needs the
        # slices sent along with the analyze request first.
        job = self.request(
            f"/api/studies/{study_id}/analyze",
            method="POST",
            body={"slices": synthetic_nodule_slices()},
        )["job"]
        loaded_job = self.wait_for_job(job["id"])

        self.assertEqual(loaded_job["status"], "done")
        self.assertTrue(loaded_job["result"]["findings"], "real model should have detected the synthetic nodule")
        self.assertEqual(loaded_job["result"]["findings"][0]["source"], "model")
        self.assertIsNotNone(loaded_job["result"]["findings"][0]["slice_index"])
        self.assertEqual(loaded_job["result"]["model"]["name"], "Improved3DCNN")

        loaded = self.request(f"/api/studies/{study_id}")["study"]
        self.assertTrue(loaded["has_slices"])
        self.assertGreaterEqual(loaded["finding_count"], 2)  # manual finding + at least one model finding

        slices_result = self.request(f"/api/studies/{study_id}/slices")
        self.assertTrue(slices_result["slices"])

        # Re-analyzing without a body reuses the slices already stored from
        # the first upload, instead of requiring them to be resent.
        second_job = self.request(
            f"/api/studies/{study_id}/analyze",
            method="POST",
            body={},
        )["job"]
        second_loaded_job = self.wait_for_job(second_job["id"])
        self.assertEqual(second_loaded_job["status"], "done")

    def test_analyze_without_any_stored_slices_fails(self) -> None:
        created = self.request(
            "/api/studies",
            method="POST",
            body={"patient_name": "Без снимка"},
        )
        response = self.request_response(
            f"/api/studies/{created['study']['id']}/analyze",
            method="POST",
            body={},
        )
        self.assertEqual(response.status_code, 400)

    def wait_for_job(self, job_id: str) -> dict:
        for _ in range(50):
            loaded_job = self.request(f"/api/jobs/{job_id}")["job"]
            if loaded_job["status"] in ("done", "failed"):
                return loaded_job
            sleep(0.1)
        self.fail("analysis job did not finish")

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
        # No report-fastapi service is running in tests, so this must fall
        # back to the local template and say so honestly in the response.
        self.assertEqual(result["source"], "fallback")
        self.assertEqual(result["study"]["report_source"], "fallback")

    def test_patient_card_lifecycle_and_duplicate_detection(self) -> None:
        match_before = self.request(
            "/api/patients/match",
            method="POST",
            body={"full_name": "Иван Петров", "birth_date": "1980-01-02"},
        )
        self.assertIsNone(match_before["match"])

        created = self.request(
            "/api/patients",
            method="POST",
            body={"full_name": "Иван Петров", "birth_date": "1980-01-02"},
        )
        patient_id = created["patient"]["id"]
        self.assertEqual(created["patient"]["study_count"], 0)

        # Same ФИО+дата рождения (different case/whitespace) must now be
        # offered as a match instead of silently creating a duplicate card.
        match_after = self.request(
            "/api/patients/match",
            method="POST",
            body={"full_name": " иван ПЕТРОВ ", "birth_date": "1980-01-02"},
        )
        self.assertEqual(match_after["match"]["id"], patient_id)

        study_response = self.request(
            f"/api/patients/{patient_id}/studies",
            method="POST",
            body={"description": "КТ грудной клетки"},
        )
        self.assertEqual(study_response["study"]["patient_record_id"], patient_id)
        self.assertIn("user", study_response)

        loaded = self.request(f"/api/patients/{patient_id}")["patient"]
        self.assertEqual(loaded["study_count"], 1)
        self.assertEqual(loaded["studies"][0]["id"], study_response["study"]["id"])

        listed = self.request("/api/patients?query=иван")["patients"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], patient_id)

        self.assertEqual(self.request("/api/patients?query=нет такого")["patients"], [])

    def test_patient_studies_endpoint_charges_and_requires_balance(self) -> None:
        created = self.request(
            "/api/patients",
            method="POST",
            body={"full_name": "Пациент Без Денег"},
        )
        patient_id = created["patient"]["id"]

        before_balance = self.request("/api/auth/me")["user"]["balance"]
        response = self.request(f"/api/patients/{patient_id}/studies", method="POST", body={})
        after_balance = response["user"]["balance"]
        self.assertLess(after_balance, before_balance)

        # Drain the balance, then confirm the endpoint rejects further studies
        # with 402 instead of creating them for free.
        while self.request("/api/auth/me")["user"]["balance"] >= 80:
            self.request(f"/api/patients/{patient_id}/studies", method="POST", body={})
        response = self.request_response(f"/api/patients/{patient_id}/studies", method="POST", body={})
        self.assertEqual(response.status_code, 402)

    def test_patient_endpoints_404_for_unknown_or_other_users_patient(self) -> None:
        response = self.request_response("/api/patients/does-not-exist")
        self.assertEqual(response.status_code, 404)

        response = self.request_response(
            "/api/patients/does-not-exist/studies", method="POST", body={}
        )
        self.assertEqual(response.status_code, 404)

    def test_health(self) -> None:
        health = self.request("/api/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["workers"], 1)
        self.assertEqual(health["service"], "lung-prometheus")

    def test_cors_default_is_not_wildcard(self) -> None:
        from backend.server import cors_allowed_origins

        origins = cors_allowed_origins()
        self.assertNotIn("*", origins)
        self.assertTrue(origins)

    def test_cors_reads_env_override(self) -> None:
        import os

        from backend.server import cors_allowed_origins

        old = os.environ.get("CORS_ALLOWED_ORIGINS")
        try:
            os.environ["CORS_ALLOWED_ORIGINS"] = "https://example.com, https://other.example.com"
            self.assertEqual(
                cors_allowed_origins(),
                ["https://example.com", "https://other.example.com"],
            )
        finally:
            if old is None:
                os.environ.pop("CORS_ALLOWED_ORIGINS", None)
            else:
                os.environ["CORS_ALLOWED_ORIGINS"] = old

    def test_auth_register_top_up_and_balance_error(self) -> None:
        registered = self.request(
            "/api/auth/register",
            method="POST",
            body={"username": "doctor", "password": "secret"},
            auth=False,
        )
        admin_token = self.token
        self.token = registered["token"]

        response = self.request_response(
            "/api/studies",
            method="POST",
            body={"patient_name": "Петр Иванов"},
        )
        self.assertEqual(response.status_code, 402)
        payload = response.json()
        self.assertEqual(payload["error"], "недостаточно средств")

        top_up = self.request("/api/balance/top-up", method="POST", body={"amount": 80})
        self.assertEqual(top_up["user"]["balance"], 80)

        created = self.request("/api/studies", method="POST", body={"patient_name": "Петр Иванов"})
        self.assertEqual(created["user"]["balance"], 0)
        self.assertEqual(created["study"]["patient_name"], "Петр Иванов")
        self.token = admin_token

    def test_logout_invalidates_token(self) -> None:
        registered = self.request(
            "/api/auth/register",
            method="POST",
            body={"username": "nurse", "password": "secret"},
            auth=False,
        )
        admin_token = self.token
        self.token = registered["token"]

        me = self.request("/api/auth/me")
        self.assertEqual(me["user"]["username"], "nurse")

        logout_response = self.request("/api/auth/logout", method="POST")
        self.assertTrue(logout_response["ok"])

        rejected = self.request_response("/api/auth/me")
        self.assertEqual(rejected.status_code, 401)
        self.token = admin_token

    def test_login_rate_limited_after_repeated_failures(self) -> None:
        self.request(
            "/api/auth/register",
            method="POST",
            body={"username": "throttleme", "password": "correct-horse"},
            auth=False,
        )
        for _ in range(5):
            failed = self.request_response(
                "/api/auth/login",
                method="POST",
                body={"username": "throttleme", "password": "wrong"},
                auth=False,
            )
            self.assertEqual(failed.status_code, 401)

        blocked = self.request_response(
            "/api/auth/login",
            method="POST",
            body={"username": "throttleme", "password": "wrong"},
            auth=False,
        )
        self.assertEqual(blocked.status_code, 429)

        # Even the correct password is throttled once the window is exhausted.
        still_blocked = self.request_response(
            "/api/auth/login",
            method="POST",
            body={"username": "throttleme", "password": "correct-horse"},
            auth=False,
        )
        self.assertEqual(still_blocked.status_code, 429)

    def test_legacy_sha256_password_is_upgraded_to_bcrypt_on_login(self) -> None:
        from backend.storage import BCRYPT_HASH_PREFIX, UserORM, _hash_password_legacy_sha256

        user = self.context.storage.create_user("legacyuser", "irrelevant")
        legacy_hash = _hash_password_legacy_sha256("oldpass", "somesalt")
        with self.context.storage.session() as session, session.begin():
            orm_user = session.get(UserORM, user["id"])
            orm_user.password_hash = legacy_hash

        with self.context.storage.session() as session:
            self.assertTrue(session.get(UserORM, user["id"]).password_hash.startswith("sha256$"))

        logged_in = self.request(
            "/api/auth/login",
            method="POST",
            body={"username": "legacyuser", "password": "oldpass"},
            auth=False,
        )
        self.assertTrue(logged_in["token"])

        with self.context.storage.session() as session:
            self.assertTrue(session.get(UserORM, user["id"]).password_hash.startswith(BCRYPT_HASH_PREFIX))

    def test_viewer_analyze_without_medical_pixels_does_not_create_demo_detection(self) -> None:
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

        self.assertEqual(result["model"]["name"], "no-medical-pixels")
        self.assertEqual(result["detections"], [])

    def test_viewer_analyze_uses_improved_3dcnn_for_pixel_data(self) -> None:
        result = self.request(
            "/api/viewer/analyze", method="POST", body={"slices": synthetic_nodule_slices()}
        )

        self.assertEqual(result["model"]["name"], "Improved3DCNN")
        self.assertGreaterEqual(result["model"]["threshold"], 0.85)
        self.assertGreater(result["model"]["probability"], result["model"]["threshold"])
        self.assertTrue(result["detections"])
        self.assertEqual(result["detections"][0]["title"], "Подозрительный объект")

    def test_viewer_analyze_does_not_trigger_on_uniform_lung_volume(self) -> None:
        volume = np.full((32, 64, 64), -750, dtype=np.int16)
        slices = [
            {
                "name": f"normal-{index}",
                "rows": 64,
                "columns": 64,
                "width": 64,
                "height": 64,
                "pixelData": {
                    "dtype": "Int16Array",
                    "data": base64.b64encode(slice_pixels.tobytes()).decode("ascii"),
                },
            }
            for index, slice_pixels in enumerate(volume)
        ]

        result = self.request("/api/viewer/analyze", method="POST", body={"slices": slices})

        self.assertEqual(result["model"]["name"], "Improved3DCNN")
        self.assertEqual(result["model"]["probability"], 0.0)
        self.assertEqual(result["detections"], [])

    def test_candidate_generator_ignores_body_wall_without_lung(self) -> None:
        model = NoduleModel(ModelConfig(weights_path=None, patch_size=32))
        volume = np.full((32, 64, 64), -1000, dtype=np.float32)
        yy, xx = np.ogrid[:64, :64]
        body = ((yy - 34) / 25) ** 2 + ((xx - 32) / 28) ** 2 <= 1
        bowel_gas = ((yy - 31) / 4) ** 2 + ((xx - 35) / 6) ** 2 <= 1
        volume[:, body] = 30
        volume[:, bowel_gas] = -780

        self.assertEqual(model._generate_candidates(volume), [])

    def test_candidate_generator_keeps_nodule_inside_lung(self) -> None:
        model = NoduleModel(ModelConfig(weights_path=None, patch_size=32))
        volume = np.full((32, 64, 64), -1000, dtype=np.float32)
        yy, xx = np.ogrid[:64, :64]
        body = ((yy - 34) / 27) ** 2 + ((xx - 32) / 30) ** 2 <= 1
        right_lung = ((yy - 32) / 17) ** 2 + ((xx - 22) / 10) ** 2 <= 1
        left_lung = ((yy - 32) / 17) ** 2 + ((xx - 42) / 10) ** 2 <= 1
        volume[:, body] = 35
        volume[:, right_lung | left_lung] = -760

        z_grid, y_grid, x_grid = np.ogrid[:32, :64, :64]
        nodule = (z_grid - 16) ** 2 + (y_grid - 29) ** 2 + (x_grid - 20) ** 2 <= 5 ** 2
        volume[nodule] = 90

        candidates = model._generate_candidates(volume)

        self.assertTrue(candidates)
        self.assertTrue(
            any(
                abs(candidate["center"][0] - 16) <= 4
                and abs(candidate["center"][1] - 29) <= 12
                and abs(candidate["center"][2] - 20) <= 12
                for candidate in candidates
            )
        )

    def test_edge_connected_mask_matches_brute_force_flood_fill(self) -> None:
        # `_edge_connected_mask` used to be a hand-rolled Python BFS; it's now
        # backed by scipy.ndimage.label for speed on full-size CT slices (see
        # README's "Производительность" section). This locks in that the
        # vectorized version still gives pixel-identical results, including
        # the case that actually matters for detection quality: an air
        # pocket fully enclosed by tissue (like a lung) must NOT be marked as
        # "outside" air just because other air touches the border.
        def brute_force(mask: np.ndarray) -> np.ndarray:
            from collections import deque

            rows, columns = mask.shape
            visited = np.zeros_like(mask, dtype=bool)
            queue: deque[tuple[int, int]] = deque()

            def add(row: int, column: int) -> None:
                if mask[row, column] and not visited[row, column]:
                    visited[row, column] = True
                    queue.append((row, column))

            for row in range(rows):
                add(row, 0)
                add(row, columns - 1)
            for column in range(columns):
                add(0, column)
                add(rows - 1, column)
            while queue:
                row, column = queue.popleft()
                if row > 0:
                    add(row - 1, column)
                if row + 1 < rows:
                    add(row + 1, column)
                if column > 0:
                    add(row, column - 1)
                if column + 1 < columns:
                    add(row, column + 1)
            return visited

        rng = np.random.default_rng(1234)
        for _ in range(10):
            mask = rng.random((60, 70)) > 0.55
            expected = brute_force(mask)
            actual = NoduleModel._edge_connected_mask(mask)
            self.assertTrue(np.array_equal(expected, actual))

        # An air pocket fully enclosed by tissue - must stay "internal", not
        # get swept up as border-connected.
        enclosed = np.zeros((20, 20), dtype=bool)
        enclosed[5:15, 5:15] = True  # doesn't touch any edge
        result = NoduleModel._edge_connected_mask(enclosed)
        self.assertFalse(result.any())

        # Air that does touch the border must be marked "outside".
        touches_edge = np.zeros((20, 20), dtype=bool)
        touches_edge[0:10, 0:10] = True  # touches row 0 and column 0
        result = NoduleModel._edge_connected_mask(touches_edge)
        self.assertTrue(np.array_equal(result, touches_edge))

    def test_box_mean_grid_matches_per_point_computation(self) -> None:
        rng = np.random.default_rng(42)
        mask = rng.random((90, 110)) > 0.6
        y_values = range(10, 80, 7)
        x_values = range(5, 100, 11)

        grid = NoduleModel._box_mean_grid(mask, y_values, x_values, 12)

        for i, y in enumerate(y_values):
            for j, x in enumerate(x_values):
                y0, y1 = max(0, y - 12), min(mask.shape[0], y + 12)
                x0, x1 = max(0, x - 12), min(mask.shape[1], x + 12)
                expected = float(np.mean(mask[y0:y1, x0:x1]))
                self.assertAlmostEqual(expected, float(grid[i, j]), places=9)

    def request(self, path: str, method: str = "GET", body: dict | None = None, auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if auth and self.token:
            headers["X-Auth-Token"] = self.token
        response = self.client.request(method, path, json=body, headers=headers)
        response.raise_for_status()
        return response.json()

    def request_response(self, path: str, method: str = "GET", body: dict | None = None, auth: bool = True):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if auth and self.token:
            headers["X-Auth-Token"] = self.token
        return self.client.request(method, path, json=body, headers=headers)
