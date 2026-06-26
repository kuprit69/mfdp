from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.storage import InsufficientBalanceError, Storage, hash_password, verify_password


class StorageTest(TestCase):
    def test_create_study_and_finding(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")

            study = storage.create_study("Иван Петров", "P-001", "КТ грудной клетки", "1980-01-02")
            finding = storage.create_finding(study["id"], "Узел", 8.5, 0.9)
            storage.save_report(study["id"], "Тестовое заключение")
            loaded = storage.get_study(study["id"])

            self.assertEqual(loaded["patient_name"], "Иван Петров")
            self.assertEqual(loaded["birth_date"], "1980-01-02")
            self.assertEqual(loaded["report"], "Тестовое заключение")
            self.assertEqual(loaded["finding_count"], 1)
            self.assertEqual(loaded["findings"][0]["id"], finding["id"])

    def test_job_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")

            study = storage.create_study("Анна Сергеева")
            job = storage.create_job(study["id"])
            storage.mark_job_running(job["id"])
            storage.mark_job_done(job["id"], {"message": "ok"})

            loaded = storage.get_job(job["id"])
            self.assertEqual(loaded["status"], "done")
            self.assertEqual(loaded["result"]["message"], "ok")

    def test_delete_study_removes_related_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")

            study = storage.create_study("Пациент")
            storage.create_finding(study["id"], "Узел", 5, 0.8)
            storage.create_job(study["id"])
            storage.delete_study(study["id"])

            self.assertEqual(storage.stats()["studies"], 0)
            self.assertEqual(storage.stats()["findings"], 0)

    def test_user_balance_and_password_hash(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")

            user = storage.create_user("doctor", "secret")
            self.assertTrue(user["token"])
            self.assertEqual(user["balance"], 0)

            loaded = storage.get_user_by_token(user["token"])
            self.assertEqual(loaded["username"], "doctor")
            storage.top_up_balance(loaded["id"], 80)
            study = storage.create_study("Пациент", user_id=loaded["id"], charge_request=True)

            self.assertEqual(study["patient_name"], "Пациент")
            self.assertEqual(storage.get_user(loaded["id"])["balance"], 0)
            with self.assertRaises(InsufficientBalanceError):
                storage.create_study("Пациент 2", user_id=loaded["id"], charge_request=True)
            stored_hash = hash_password("secret")
            self.assertTrue(verify_password("secret", stored_hash))
            self.assertFalse(verify_password("wrong", stored_hash))
