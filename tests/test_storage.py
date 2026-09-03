from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.storage import (
    BCRYPT_HASH_PREFIX,
    InsufficientBalanceError,
    Storage,
    _hash_password_legacy_sha256,
    hash_password,
    needs_rehash,
    verify_password,
)


class StorageTest(TestCase):
    def test_create_study_and_finding(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                study = storage.create_study("Иван Петров", "P-001", "КТ грудной клетки", "1980-01-02")
                finding = storage.create_finding(study["id"], "Узел", 8.5, 0.9)
                storage.save_report(study["id"], "Тестовое заключение", source="ollama")
                loaded = storage.get_study(study["id"])

                self.assertEqual(loaded["patient_name"], "Иван Петров")
                self.assertEqual(loaded["birth_date"], "1980-01-02")
                self.assertEqual(loaded["report"], "Тестовое заключение")
                self.assertEqual(loaded["report_source"], "ollama")
                self.assertEqual(loaded["finding_count"], 1)
                self.assertEqual(loaded["findings"][0]["id"], finding["id"])
            finally:
                storage.close()

    def test_job_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                study = storage.create_study("Анна Сергеева")
                job = storage.create_job(study["id"])
                storage.mark_job_running(job["id"])
                storage.mark_job_done(job["id"], {"message": "ok"})

                loaded = storage.get_job(job["id"])
                self.assertEqual(loaded["status"], "done")
                self.assertEqual(loaded["result"]["message"], "ok")
            finally:
                storage.close()

    def test_delete_study_removes_related_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                study = storage.create_study("Пациент")
                storage.create_finding(study["id"], "Узел", 5, 0.8)
                storage.create_job(study["id"])
                storage.delete_study(study["id"])

                self.assertEqual(storage.stats()["studies"], 0)
                self.assertEqual(storage.stats()["findings"], 0)
            finally:
                storage.close()

    def test_finding_geometry_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                study = storage.create_study("Пациент")
                finding = storage.create_finding(
                    study["id"],
                    "Узел",
                    9.1,
                    0.77,
                    source="model",
                    slice_index=5,
                    x=10.0,
                    y=12.5,
                    width=20.0,
                    height=18.0,
                    segment_label="S3 правого легкого",
                    model_name="Improved3DCNN",
                    threshold=0.85,
                )
                self.assertEqual(finding["slice_index"], 5)
                self.assertEqual(finding["segment_label"], "S3 правого легкого")

                loaded = storage.get_study(study["id"])
                self.assertEqual(loaded["findings"][0]["model_name"], "Improved3DCNN")
                self.assertEqual(loaded["findings"][0]["width"], 20.0)
            finally:
                storage.close()

    def test_user_balance_and_password_hash(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
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
            finally:
                storage.close()

    def test_hash_password_uses_bcrypt_and_does_not_need_rehash(self) -> None:
        stored_hash = hash_password("secret")
        self.assertTrue(stored_hash.startswith(BCRYPT_HASH_PREFIX))
        self.assertFalse(needs_rehash(stored_hash))

    def test_legacy_sha256_hash_still_verifies_and_flags_for_rehash(self) -> None:
        legacy_hash = _hash_password_legacy_sha256("secret", "somesalt")
        self.assertTrue(legacy_hash.startswith("sha256$"))
        self.assertTrue(verify_password("secret", legacy_hash))
        self.assertFalse(verify_password("wrong", legacy_hash))
        self.assertTrue(needs_rehash(legacy_hash))

    def test_logout_rotates_auth_token(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                user = storage.create_user("doctor", "secret")
                old_token = user["token"]

                storage.logout(user["id"])

                with self.assertRaises(PermissionError):
                    storage.get_user_by_token(old_token)

                # Logging back in should still work and issue a fresh token.
                logged_in = storage.authenticate_user("doctor", "secret")
                self.assertTrue(logged_in["token"])
                self.assertNotEqual(logged_in["token"], old_token)
            finally:
                storage.close()

    def test_create_patient_and_add_study_to_card(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                user = storage.create_user("doctor", "secret")
                storage.top_up_balance(user["id"], 160)

                patient = storage.create_patient("Иван Петров", "1980-01-02", user_id=user["id"])
                self.assertEqual(patient["study_count"], 0)

                study = storage.create_study_for_patient(patient["id"], "КТ грудной клетки", user_id=user["id"])
                self.assertEqual(study["patient_record_id"], patient["id"])
                self.assertEqual(storage.get_user(user["id"])["balance"], 80)

                loaded = storage.get_patient(patient["id"], user["id"])
                self.assertEqual(loaded["study_count"], 1)
                self.assertEqual(loaded["studies"][0]["id"], study["id"])

                with self.assertRaises(KeyError):
                    storage.get_patient(patient["id"], user_id="someone-else")
            finally:
                storage.close()

    def test_find_matching_patient_is_case_and_whitespace_insensitive(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                user = storage.create_user("doctor", "secret")
                patient = storage.create_patient("Иван Петров", "1980-01-02", user_id=user["id"])

                match = storage.find_matching_patient(" иван ПЕТРОВ ".strip(), "1980-01-02", user["id"])
                self.assertIsNotNone(match)
                self.assertEqual(match["id"], patient["id"])

                self.assertIsNone(storage.find_matching_patient("Иван Петров", "1990-01-01", user["id"]))
                self.assertIsNone(storage.find_matching_patient("Другой Пациент", "1980-01-02", user["id"]))
                # Without a birth date there isn't enough to safely call it a
                # duplicate - should not match.
                self.assertIsNone(storage.find_matching_patient("Иван Петров", "", user["id"]))
            finally:
                storage.close()

    def test_list_patients_search_is_unicode_case_insensitive(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                user = storage.create_user("doctor", "secret")
                storage.create_patient("Иван Петров", "1980-01-02", user_id=user["id"])
                storage.create_patient("Мария Сидорова", "1990-05-05", user_id=user["id"])

                hits = storage.list_patients(user["id"], query="иван")
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["full_name"], "Иван Петров")

                self.assertEqual(storage.list_patients(user["id"], query="нет такого"), [])
                self.assertEqual(len(storage.list_patients(user["id"])), 2)
            finally:
                storage.close()

    def test_backfill_groups_legacy_studies_into_patient_cards(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                user = storage.create_user("doctor", "secret")
                storage.create_study("Иван Петров", birth_date="1980-01-02", user_id=user["id"])
                storage.create_study("иван петров", birth_date="1980-01-02", user_id=user["id"])
                storage.create_study("Мария Сидорова", birth_date="1990-05-05", user_id=user["id"])

                # Studies created via the legacy flow have no card yet until
                # the backfill runs (it also runs once automatically on
                # Storage startup/migrate(), this call just re-triggers it).
                storage._backfill_patients()

                patients = storage.list_patients(user["id"])
                self.assertEqual(len(patients), 2)
                by_name = {p["full_name"]: p for p in patients}
                self.assertEqual(by_name["Иван Петров"]["study_count"], 2)
                self.assertEqual(by_name["Мария Сидорова"]["study_count"], 1)

                # Re-running the backfill must be a no-op (idempotent) - it
                # should not create duplicate cards for already-linked studies.
                storage._backfill_patients()
                self.assertEqual(len(storage.list_patients(user["id"])), 2)
            finally:
                storage.close()

    def test_delete_patient_unlinks_studies_instead_of_deleting_them(self) -> None:
        with TemporaryDirectory() as tmp:
            storage = Storage(f"{tmp}/test.sqlite3")
            try:
                user = storage.create_user("doctor", "secret")
                storage.top_up_balance(user["id"], 80)
                patient = storage.create_patient("Иван Петров", "1980-01-02", user_id=user["id"])
                study = storage.create_study_for_patient(patient["id"], user_id=user["id"])

                storage.delete_patient(patient["id"], user["id"])

                with self.assertRaises(KeyError):
                    storage.get_patient(patient["id"], user["id"])
                # The study itself must survive - only the card link is gone.
                loaded_study = storage.get_study(study["id"], user["id"])
                self.assertIsNone(loaded_study["patient_record_id"])
            finally:
                storage.close()
