from __future__ import annotations

import json
import sqlite3
import hashlib
import hmac
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from threading import Lock

try:
    from .domain import AnalysisJob, Finding, Study, new_id, now_iso
except ImportError:  # pragma: no cover - used when running backend/server.py directly.
    from domain import AnalysisJob, Finding, Study, new_id, now_iso


REQUEST_PRICE_KOPEKS = 8_000


class InsufficientBalanceError(ValueError):
    pass


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt, digest = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "sha256":
        return False
    expected = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(expected, digest)


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def migrate(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    balance_kopeks INTEGER NOT NULL DEFAULT 0,
                    auth_token TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS studies (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT '',
                    patient_name TEXT NOT NULL,
                    patient_id TEXT NOT NULL DEFAULT '',
                    birth_date TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    report TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    study_id TEXT NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    diameter_mm REAL NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id TEXT PRIMARY KEY,
                    study_id TEXT NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )
            self._add_column_if_missing(db, "studies", "user_id", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(db, "studies", "birth_date", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(db, "studies", "report", "TEXT NOT NULL DEFAULT ''")
            admin_id = self._ensure_admin_user(db)
            db.execute("UPDATE studies SET user_id = ? WHERE user_id = ''", (admin_id,))

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip()
        if not username or not password:
            raise ValueError("логин и пароль обязательны")

        user_id = new_id()
        token = secrets.token_urlsafe(32)
        created_at = now_iso()
        with self._lock, self.connect() as db:
            try:
                db.execute(
                    """
                    INSERT INTO users (id, username, password_hash, balance_kopeks, auth_token, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, username, hash_password(password), 0, token, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("пользователь уже существует") from exc
        return self.get_user_by_token(token, include_token=True)

    def authenticate_user(self, username: str, password: str) -> dict:
        username = username.strip()
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
                raise PermissionError("неверный логин или пароль")
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE users SET auth_token = ? WHERE id = ?", (token, row["id"]))
        return self.get_user_by_token(token, include_token=True)

    def get_user(self, user_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise PermissionError("требуется вход")
        return self._user_from_row(row)

    def get_user_by_token(self, token: str, include_token: bool = False) -> dict:
        if not token:
            raise PermissionError("требуется вход")
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE auth_token = ?", (token,)).fetchone()
            if row is None:
                raise PermissionError("требуется вход")
        return self._user_from_row(row, include_token=include_token)

    def top_up_balance(self, user_id: str, amount: object) -> dict:
        kopeks = self._amount_to_kopeks(amount)
        if kopeks <= 0:
            raise ValueError("сумма должна быть больше 0")
        with self._lock, self.connect() as db:
            result = db.execute(
                "UPDATE users SET balance_kopeks = balance_kopeks + ? WHERE id = ?",
                (kopeks, user_id),
            )
            if result.rowcount == 0:
                raise PermissionError("требуется вход")
        return self.get_user(user_id)

    def create_study(
        self,
        patient_name: str,
        patient_id: str = "",
        description: str = "",
        birth_date: str = "",
        user_id: str = "",
        charge_request: bool = False,
    ) -> dict:
        patient_name = patient_name.strip()
        if not patient_name:
            raise ValueError("patient_name is required")

        study = Study(
            patient_name=patient_name,
            patient_id=patient_id.strip(),
            birth_date=birth_date.strip(),
            description=description.strip(),
        )
        with self._lock, self.connect() as db:
            if user_id and not self._user_exists(db, user_id):
                raise PermissionError("требуется вход")
            if charge_request:
                self._charge_request_locked(db, user_id)
            db.execute(
                """
                INSERT INTO studies (id, user_id, patient_name, patient_id, birth_date, description, report, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    study.id,
                    user_id,
                    study.patient_name,
                    study.patient_id,
                    study.birth_date,
                    study.description,
                    study.report,
                    study.status,
                    study.created_at,
                ),
            )
        return self.get_study(study.id, user_id or None)

    def list_studies(self, user_id: str | None = None) -> list[dict]:
        where = "WHERE s.user_id = ?" if user_id is not None else ""
        params = (user_id,) if user_id is not None else ()
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT
                    s.*,
                    COUNT(f.id) AS finding_count,
                    COALESCE(MAX(f.diameter_mm), 0) AS max_diameter_mm
                FROM studies s
                LEFT JOIN findings f ON f.study_id = s.id
                {where}
                GROUP BY s.id
                ORDER BY s.created_at DESC
                """,
                params,
            ).fetchall()
        return [self._study_from_row(row) for row in rows]

    def get_study(self, study_id: str, user_id: str | None = None) -> dict:
        user_filter = "AND s.user_id = ?" if user_id is not None else ""
        params = (study_id, user_id) if user_id is not None else (study_id,)
        with self.connect() as db:
            row = db.execute(
                f"""
                SELECT
                    s.*,
                    COUNT(f.id) AS finding_count,
                    COALESCE(MAX(f.diameter_mm), 0) AS max_diameter_mm
                FROM studies s
                LEFT JOIN findings f ON f.study_id = s.id
                WHERE s.id = ?
                {user_filter}
                GROUP BY s.id
                """,
                params,
            ).fetchone()
            if row is None:
                raise KeyError("study not found")

            findings = db.execute(
                "SELECT * FROM findings WHERE study_id = ? ORDER BY created_at DESC",
                (study_id,),
            ).fetchall()
            jobs = db.execute(
                "SELECT * FROM analysis_jobs WHERE study_id = ? ORDER BY created_at DESC",
                (study_id,),
            ).fetchall()

        study = self._study_from_row(row)
        study["findings"] = [self._finding_from_row(item) for item in findings]
        study["jobs"] = [self._job_from_row(item) for item in jobs]
        return study

    def delete_study(self, study_id: str, user_id: str | None = None) -> None:
        user_filter = "AND user_id = ?" if user_id is not None else ""
        params = (study_id, user_id) if user_id is not None else (study_id,)
        with self._lock, self.connect() as db:
            result = db.execute(f"DELETE FROM studies WHERE id = ? {user_filter}", params)
            if result.rowcount == 0:
                raise KeyError("study not found")

    def create_finding(
        self,
        study_id: str,
        title: str,
        diameter_mm: float,
        confidence: float,
        source: str = "manual",
        user_id: str | None = None,
    ) -> dict:
        if not title.strip():
            raise ValueError("title is required")
        diameter_mm = float(diameter_mm)
        confidence = float(confidence)
        if diameter_mm <= 0:
            raise ValueError("diameter_mm must be positive")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")

        finding = Finding(
            study_id=study_id,
            title=title.strip(),
            diameter_mm=diameter_mm,
            confidence=confidence,
            source=source.strip() or "manual",
        )

        with self._lock, self.connect() as db:
            if not self._study_exists(db, study_id, user_id):
                raise KeyError("study not found")
            db.execute(
                """
                INSERT INTO findings (id, study_id, title, diameter_mm, confidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding.id,
                    finding.study_id,
                    finding.title,
                    finding.diameter_mm,
                    finding.confidence,
                    finding.source,
                    finding.created_at,
                ),
            )
            db.execute("UPDATE studies SET status = ? WHERE id = ?", ("reviewed", study_id))

        return finding.to_dict()

    def create_job(self, study_id: str, user_id: str | None = None) -> dict:
        job = AnalysisJob(study_id=study_id)
        with self._lock, self.connect() as db:
            if not self._study_exists(db, study_id, user_id):
                raise KeyError("study not found")
            db.execute(
                """
                INSERT INTO analysis_jobs (id, study_id, status, result_json, error, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job.id, job.study_id, job.status, None, None, job.created_at, None),
            )
            db.execute("UPDATE studies SET status = ? WHERE id = ?", ("queued", study_id))
        return job.to_dict()

    def get_job(self, job_id: str, user_id: str | None = None) -> dict:
        with self.connect() as db:
            if user_id is None:
                row = db.execute("SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT j.*
                    FROM analysis_jobs j
                    JOIN studies s ON s.id = j.study_id
                    WHERE j.id = ? AND s.user_id = ?
                    """,
                    (job_id, user_id),
                ).fetchone()
            if row is None:
                raise KeyError("job not found")
        return self._job_from_row(row)

    def mark_job_running(self, job_id: str) -> None:
        self._update_job(job_id, status="running")

    def mark_job_done(self, job_id: str, result: dict) -> None:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT study_id FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job not found")
            db.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, result_json = ?, error = NULL, finished_at = ?
                WHERE id = ?
                """,
                ("done", json.dumps(result, ensure_ascii=False), now_iso(), job_id),
            )
            db.execute("UPDATE studies SET status = ? WHERE id = ?", ("analyzed", row["study_id"]))

    def mark_job_failed(self, job_id: str, error: str) -> None:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT study_id FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job not found")
            db.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                ("failed", error, now_iso(), job_id),
            )
            db.execute("UPDATE studies SET status = ? WHERE id = ?", ("error", row["study_id"]))

    def save_report(self, study_id: str, report: str, user_id: str | None = None) -> dict:
        report = report.strip()
        if not report:
            raise ValueError("report is required")
        user_filter = "AND user_id = ?" if user_id is not None else ""
        params = (report, "reported", study_id, user_id) if user_id is not None else (report, "reported", study_id)
        with self._lock, self.connect() as db:
            result = db.execute(
                f"UPDATE studies SET report = ?, status = ? WHERE id = ? {user_filter}",
                params,
            )
            if result.rowcount == 0:
                raise KeyError("study not found")
        return self.get_study(study_id, user_id)

    def stats(self) -> dict:
        with self.connect() as db:
            studies = db.execute("SELECT COUNT(*) FROM studies").fetchone()[0]
            findings = db.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
            queued = db.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        return {"studies": studies, "findings": findings, "active_jobs": queued}

    def _update_job(self, job_id: str, **fields: str) -> None:
        if not fields:
            return
        names = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        with self._lock, self.connect() as db:
            result = db.execute(f"UPDATE analysis_jobs SET {names} WHERE id = ?", [*values, job_id])
            if result.rowcount == 0:
                raise KeyError("job not found")

    def _charge_request_locked(self, db: sqlite3.Connection, user_id: str) -> None:
        row = db.execute("SELECT balance_kopeks FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise PermissionError("требуется вход")
        if row["balance_kopeks"] < REQUEST_PRICE_KOPEKS:
            raise InsufficientBalanceError("недостаточно средств")
        db.execute(
            "UPDATE users SET balance_kopeks = balance_kopeks - ? WHERE id = ?",
            (REQUEST_PRICE_KOPEKS, user_id),
        )

    @staticmethod
    def _study_exists(db: sqlite3.Connection, study_id: str, user_id: str | None = None) -> bool:
        if user_id is None:
            return db.execute("SELECT 1 FROM studies WHERE id = ?", (study_id,)).fetchone() is not None
        return db.execute(
            "SELECT 1 FROM studies WHERE id = ? AND user_id = ?",
            (study_id, user_id),
        ).fetchone() is not None

    @staticmethod
    def _user_exists(db: sqlite3.Connection, user_id: str) -> bool:
        return db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None

    @staticmethod
    def _add_column_if_missing(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _ensure_admin_user(self, db: sqlite3.Connection) -> str:
        row = db.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
        if row is not None:
            return str(row["id"])

        admin_id = new_id()
        db.execute(
            """
            INSERT INTO users (id, username, password_hash, balance_kopeks, auth_token, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (admin_id, "admin", hash_password("admin"), 0, secrets.token_urlsafe(32), now_iso()),
        )
        return admin_id

    @staticmethod
    def _amount_to_kopeks(amount: object) -> int:
        try:
            value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("некорректная сумма") from exc
        return int(value * 100)

    @staticmethod
    def _kopeks_to_rubles(kopeks: int) -> float | int:
        value = Decimal(int(kopeks)) / Decimal(100)
        if value == value.to_integral():
            return int(value)
        return float(value)

    @classmethod
    def _user_from_row(cls, row: sqlite3.Row, include_token: bool = False) -> dict:
        user = {
            "id": row["id"],
            "username": row["username"],
            "balance": cls._kopeks_to_rubles(row["balance_kopeks"]),
            "request_price": cls._kopeks_to_rubles(REQUEST_PRICE_KOPEKS),
            "created_at": row["created_at"],
        }
        if include_token:
            user["token"] = row["auth_token"]
        return user

    @staticmethod
    def _study_from_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "patient_name": row["patient_name"],
            "patient_id": row["patient_id"],
            "birth_date": row["birth_date"],
            "description": row["description"],
            "report": row["report"],
            "status": row["status"],
            "created_at": row["created_at"],
            "finding_count": row["finding_count"],
            "max_diameter_mm": row["max_diameter_mm"],
        }

    @staticmethod
    def _finding_from_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "study_id": row["study_id"],
            "title": row["title"],
            "diameter_mm": row["diameter_mm"],
            "confidence": row["confidence"],
            "source": row["source"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "id": row["id"],
            "study_id": row["study_id"],
            "status": row["status"],
            "result": result,
            "error": row["error"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
        }
