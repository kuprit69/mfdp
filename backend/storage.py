from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from threading import Lock

import bcrypt
from sqlalchemy import Float, ForeignKey, Integer, String, Text, create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.event import listens_for
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

try:
    from .domain import Finding, Patient, Study, new_id, now_iso
except ImportError:  # pragma: no cover - used when running backend/server.py directly.
    from domain import Finding, Patient, Study, new_id, now_iso


REQUEST_PRICE_KOPEKS = 8_000
LEGACY_HASH_PREFIX = "sha256$"
BCRYPT_HASH_PREFIX = "bcrypt$"


class InsufficientBalanceError(ValueError):
    pass


def hash_password(password: str) -> str:
    """Hash a new password with bcrypt (a slow, salted KDF - the legacy
    sha256 scheme below is verify-only, kept so existing accounts keep
    working)."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return BCRYPT_HASH_PREFIX + hashed.decode("ascii")


def _hash_password_legacy_sha256(password: str, salt: str) -> str:
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{LEGACY_HASH_PREFIX}{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    if stored_hash.startswith(BCRYPT_HASH_PREFIX):
        encoded = stored_hash[len(BCRYPT_HASH_PREFIX):].encode("ascii")
        try:
            return bcrypt.checkpw(password.encode("utf-8"), encoded)
        except (ValueError, TypeError):
            return False

    # Accounts created before bcrypt was introduced still have a
    # "sha256$salt$digest" hash. Keep verifying those (see needs_rehash()
    # for how they get upgraded transparently on next successful login)
    # instead of locking existing users out.
    try:
        algorithm, salt, digest = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "sha256":
        return False
    expected = _hash_password_legacy_sha256(password, salt).split("$", 2)[2]
    return hmac.compare_digest(expected, digest)


def needs_rehash(stored_hash: str) -> bool:
    return not stored_hash.startswith(BCRYPT_HASH_PREFIX)


class Base(DeclarativeBase):
    pass


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(96), nullable=False)
    balance_kopeks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auth_token: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)

    studies: Mapped[list[StudyORM]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    patients: Mapped[list[PatientORM]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PatientORM(Base):
    """A patient card: groups studies by ФИО+дата рождения so a returning
    patient's history can be browsed/searched as one record instead of a
    flat list of unrelated studies."""

    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    birth_date: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # Free-text external ID/MRN, independent from this row's own primary key.
    patient_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    user: Mapped[UserORM] = relationship(back_populates="patients")
    studies: Mapped[list[StudyORM]] = relationship(
        back_populates="patient",
        passive_deletes=True,
    )


class StudyORM(Base):
    __tablename__ = "studies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    patient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    patient_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    birth_date: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    report: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "ollama" when the report LLM produced the text, "fallback" when the
    # report service (or this API, if the report service was unreachable)
    # fell back to the templated report instead. Empty when no report yet.
    report_source: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="new", index=True)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Links this study into a patient's card/history. Nullable: legacy studies
    # created before this column existed (or created without going through the
    # patient-card flow) simply have no card yet - see Storage._backfill_patients.
    patient_record_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("patients.id", ondelete="SET NULL"),
        index=True,
    )

    user: Mapped[UserORM] = relationship(back_populates="studies")
    patient: Mapped[PatientORM | None] = relationship(back_populates="studies")
    findings: Mapped[list[FindingORM]] = relationship(
        back_populates="study",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    jobs: Mapped[list[AnalysisJobORM]] = relationship(
        back_populates="study",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class FindingORM(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    study_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    diameter_mm: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    # Geometry of the detection on the original slice, so a study can later be
    # reopened with the same box drawn on the same slice. Only populated for
    # model-sourced findings; manual findings leave these columns null.
    slice_index: Mapped[int | None] = mapped_column(Integer)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    segment_label: Mapped[str | None] = mapped_column(String(120))
    model_name: Mapped[str | None] = mapped_column(String(80))
    threshold: Mapped[float | None] = mapped_column(Float)

    study: Mapped[StudyORM] = relationship(back_populates="findings")


class AnalysisJobORM(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    study_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    finished_at: Mapped[str | None] = mapped_column(String(40))

    study: Mapped[StudyORM] = relationship(back_populates="jobs")


@listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


class Storage:
    def __init__(self, database_url: str | Path) -> None:
        self.database_url = self._normalize_database_url(database_url)
        self.path = self.database_url
        self._lock = Lock()
        connect_args = {"check_same_thread": False} if self.database_url.startswith("sqlite") else {}
        self.engine = create_engine(self.database_url, future=True, connect_args=connect_args)
        self.SessionLocal = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self.migrate()

    @staticmethod
    def _normalize_database_url(database_url: str | Path) -> str:
        value = str(database_url)
        if "://" in value:
            return value
        path = Path(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+pysqlite:///{path}"

    def session(self) -> Session:
        return self.SessionLocal()

    def close(self) -> None:
        self.engine.dispose()

    def migrate(self) -> None:
        last_error: Exception | None = None
        for _ in range(20):
            try:
                Base.metadata.create_all(self.engine)
                self._ensure_finding_geometry_columns()
                self._ensure_study_report_source_column()
                self._ensure_study_patient_record_column()
                with self._lock, self.session() as session, session.begin():
                    self._ensure_admin_user(session)
                self._backfill_patients()
                return
            except Exception as exc:  # noqa: BLE001 - startup should wait for PostgreSQL readiness.
                last_error = exc
                time.sleep(1)
        if last_error is not None:
            raise last_error

    def _ensure_finding_geometry_columns(self) -> None:
        # create_all() only creates missing tables, not missing columns on an
        # already-existing "findings" table (e.g. a database from before the
        # detection-geometry columns were added). Add them in place so older
        # databases keep working without a manual migration step.
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(self.engine)
        existing_columns = {column["name"] for column in inspector.get_columns("findings")}
        additions = {
            "slice_index": "INTEGER",
            "x": "FLOAT",
            "y": "FLOAT",
            "width": "FLOAT",
            "height": "FLOAT",
            "segment_label": "VARCHAR(120)",
            "model_name": "VARCHAR(80)",
            "threshold": "FLOAT",
        }
        with self.engine.begin() as connection:
            for name, ddl_type in additions.items():
                if name not in existing_columns:
                    connection.exec_driver_sql(f"ALTER TABLE findings ADD COLUMN {name} {ddl_type}")

    def _ensure_study_report_source_column(self) -> None:
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(self.engine)
        existing_columns = {column["name"] for column in inspector.get_columns("studies")}
        if "report_source" not in existing_columns:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE studies ADD COLUMN report_source VARCHAR(20) NOT NULL DEFAULT ''"
                )

    def _ensure_study_patient_record_column(self) -> None:
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(self.engine)
        existing_columns = {column["name"] for column in inspector.get_columns("studies")}
        if "patient_record_id" not in existing_columns:
            with self.engine.begin() as connection:
                connection.exec_driver_sql("ALTER TABLE studies ADD COLUMN patient_record_id VARCHAR(36)")

    def _backfill_patients(self) -> None:
        """Group any study that isn't linked to a patient card yet (studies
        created before PatientORM existed, or by an older client) into
        synthetic Patient records by (user_id, ФИО, дата рождения), so old
        history shows up as patient cards too instead of disappearing."""
        with self._lock, self.session() as session, session.begin():
            orphan_studies = session.scalars(
                select(StudyORM).where(StudyORM.patient_record_id.is_(None))
            ).all()
            if not orphan_studies:
                return
            groups: dict[tuple[str, str, str], list[StudyORM]] = {}
            for study in orphan_studies:
                key = (study.user_id, study.patient_name.strip().lower(), study.birth_date.strip())
                groups.setdefault(key, []).append(study)
            for (user_id, _normalized_name, _normalized_birth), studies in groups.items():
                first = min(studies, key=lambda s: s.created_at)
                patient = PatientORM(
                    id=new_id(),
                    user_id=user_id,
                    full_name=first.patient_name.strip(),
                    birth_date=first.birth_date.strip(),
                    patient_id=first.patient_id.strip(),
                    created_at=first.created_at,
                )
                session.add(patient)
                session.flush()
                for study in studies:
                    study.patient_record_id = patient.id

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip()
        if not username or not password:
            raise ValueError("логин и пароль обязательны")

        user = UserORM(
            id=new_id(),
            username=username,
            password_hash=hash_password(password),
            balance_kopeks=0,
            auth_token=secrets.token_urlsafe(32),
            created_at=now_iso(),
        )
        with self._lock, self.session() as session, session.begin():
            if session.scalar(select(UserORM).where(UserORM.username == username)) is not None:
                raise ValueError("пользователь уже существует")
            session.add(user)
        return self.get_user_by_token(user.auth_token, include_token=True)

    def authenticate_user(self, username: str, password: str) -> dict:
        username = username.strip()
        with self._lock, self.session() as session, session.begin():
            user = session.scalar(select(UserORM).where(UserORM.username == username))
            if user is None or not verify_password(password, user.password_hash):
                raise PermissionError("неверный логин или пароль")
            if needs_rehash(user.password_hash):
                # Transparently upgrade legacy sha256 hashes to bcrypt now that
                # we know the plaintext password is correct.
                user.password_hash = hash_password(password)
            user.auth_token = secrets.token_urlsafe(32)
            token = user.auth_token
        return self.get_user_by_token(token, include_token=True)

    def logout(self, user_id: str) -> None:
        """Invalidate the user's current auth token server-side by rotating it."""
        with self._lock, self.session() as session, session.begin():
            user = session.get(UserORM, user_id)
            if user is None:
                return
            user.auth_token = secrets.token_urlsafe(32)

    def get_user(self, user_id: str) -> dict:
        with self.session() as session:
            user = session.get(UserORM, user_id)
            if user is None:
                raise PermissionError("требуется вход")
            return self._user_from_orm(user)

    def get_user_by_token(self, token: str, include_token: bool = False) -> dict:
        if not token:
            raise PermissionError("требуется вход")
        with self.session() as session:
            user = session.scalar(select(UserORM).where(UserORM.auth_token == token))
            if user is None:
                raise PermissionError("требуется вход")
            return self._user_from_orm(user, include_token=include_token)

    def top_up_balance(self, user_id: str, amount: object) -> dict:
        kopeks = self._amount_to_kopeks(amount)
        if kopeks <= 0:
            raise ValueError("сумма должна быть больше 0")
        with self._lock, self.session() as session, session.begin():
            user = session.get(UserORM, user_id)
            if user is None:
                raise PermissionError("требуется вход")
            user.balance_kopeks += kopeks
        return self.get_user(user_id)

    def create_study(
        self,
        patient_name: str,
        patient_id: str = "",
        description: str = "",
        birth_date: str = "",
        user_id: str = "",
        charge_request: bool = False,
        patient_record_id: str | None = None,
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
        with self._lock, self.session() as session, session.begin():
            if not user_id:
                user_id = self._ensure_admin_user(session)
            user = session.get(UserORM, user_id)
            if user is None:
                raise PermissionError("требуется вход")
            if patient_record_id:
                patient = session.get(PatientORM, patient_record_id)
                if patient is None or patient.user_id != user_id:
                    raise KeyError("patient not found")
            if charge_request:
                self._charge_request_locked(user)
            session.add(
                StudyORM(
                    id=study.id,
                    user_id=user_id,
                    patient_name=study.patient_name,
                    patient_id=study.patient_id,
                    birth_date=study.birth_date,
                    description=study.description,
                    report=study.report,
                    status=study.status,
                    created_at=study.created_at,
                    patient_record_id=patient_record_id or None,
                )
            )
        return self.get_study(study.id, user_id or None)

    def create_study_for_patient(
        self,
        patient_record_id: str,
        description: str = "",
        user_id: str | None = None,
    ) -> dict:
        """Create a new study directly inside an existing patient's card
        (charges the same request fee as a plain POST /api/studies)."""
        with self.session() as session:
            patient = session.get(PatientORM, patient_record_id)
            if patient is None or (user_id is not None and patient.user_id != user_id):
                raise KeyError("patient not found")
            resolved_user_id = patient.user_id
            full_name = patient.full_name
            external_patient_id = patient.patient_id
            birth_date = patient.birth_date
        return self.create_study(
            patient_name=full_name,
            patient_id=external_patient_id,
            birth_date=birth_date,
            description=description,
            user_id=resolved_user_id,
            charge_request=True,
            patient_record_id=patient_record_id,
        )

    def list_studies(self, user_id: str | None = None) -> list[dict]:
        with self.session() as session:
            counts = (
                select(
                    FindingORM.study_id.label("study_id"),
                    func.count(FindingORM.id).label("finding_count"),
                    func.coalesce(func.max(FindingORM.diameter_mm), 0).label("max_diameter_mm"),
                )
                .group_by(FindingORM.study_id)
                .subquery()
            )
            statement = (
                select(
                    StudyORM,
                    func.coalesce(counts.c.finding_count, 0),
                    func.coalesce(counts.c.max_diameter_mm, 0),
                )
                .outerjoin(counts, counts.c.study_id == StudyORM.id)
                .order_by(StudyORM.created_at.desc())
            )
            if user_id is not None:
                statement = statement.where(StudyORM.user_id == user_id)
            rows = session.execute(statement).all()
        return [self._study_from_orm(study, finding_count, max_diameter) for study, finding_count, max_diameter in rows]

    def get_study(self, study_id: str, user_id: str | None = None) -> dict:
        with self.session() as session:
            study = self._load_study(session, study_id, user_id)
            finding_count, max_diameter = self._finding_summary(session, study_id)
            findings = session.scalars(
                select(FindingORM).where(FindingORM.study_id == study_id).order_by(FindingORM.created_at.desc())
            ).all()
            jobs = session.scalars(
                select(AnalysisJobORM).where(AnalysisJobORM.study_id == study_id).order_by(AnalysisJobORM.created_at.desc())
            ).all()

            payload = self._study_from_orm(study, finding_count, max_diameter)
            payload["findings"] = [self._finding_from_orm(item) for item in findings]
            payload["jobs"] = [self._job_from_orm(item) for item in jobs]
            return payload

    def delete_study(self, study_id: str, user_id: str | None = None) -> None:
        with self._lock, self.session() as session, session.begin():
            study = self._load_study(session, study_id, user_id)
            session.delete(study)

    @staticmethod
    def _normalize_patient_key(full_name: str, birth_date: str) -> tuple[str, str]:
        return full_name.strip().lower(), birth_date.strip()

    def find_matching_patient(self, full_name: str, birth_date: str, user_id: str) -> dict | None:
        """Look for an existing patient card with the same ФИО + дата
        рождения for this user, so the client can offer "add to existing
        card" instead of silently creating a duplicate patient."""
        name_key, birth_key = self._normalize_patient_key(full_name, birth_date)
        if not name_key or not birth_key:
            return None
        with self.session() as session:
            candidates = session.scalars(
                select(PatientORM).where(PatientORM.user_id == user_id)
            ).all()
            for patient in candidates:
                if self._normalize_patient_key(patient.full_name, patient.birth_date) == (name_key, birth_key):
                    return self.get_patient(patient.id, user_id)
        return None

    def create_patient(
        self,
        full_name: str,
        birth_date: str = "",
        patient_id: str = "",
        user_id: str = "",
    ) -> dict:
        full_name = full_name.strip()
        if not full_name:
            raise ValueError("full_name is required")

        patient = Patient(
            full_name=full_name,
            birth_date=birth_date.strip(),
            patient_id=patient_id.strip(),
        )
        with self._lock, self.session() as session, session.begin():
            if not user_id:
                user_id = self._ensure_admin_user(session)
            user = session.get(UserORM, user_id)
            if user is None:
                raise PermissionError("требуется вход")
            session.add(
                PatientORM(
                    id=patient.id,
                    user_id=user_id,
                    full_name=patient.full_name,
                    birth_date=patient.birth_date,
                    patient_id=patient.patient_id,
                    created_at=patient.created_at,
                )
            )
        return self.get_patient(patient.id, user_id)

    def list_patients(self, user_id: str | None = None, query: str = "") -> list[dict]:
        with self.session() as session:
            counts = (
                select(
                    StudyORM.patient_record_id.label("patient_record_id"),
                    func.count(StudyORM.id).label("study_count"),
                    func.max(StudyORM.created_at).label("last_study_at"),
                )
                .where(StudyORM.patient_record_id.is_not(None))
                .group_by(StudyORM.patient_record_id)
                .subquery()
            )
            statement = (
                select(
                    PatientORM,
                    func.coalesce(counts.c.study_count, 0),
                    counts.c.last_study_at,
                )
                .outerjoin(counts, counts.c.patient_record_id == PatientORM.id)
            )
            if user_id is not None:
                statement = statement.where(PatientORM.user_id == user_id)
            statement = statement.order_by(func.coalesce(counts.c.last_study_at, PatientORM.created_at).desc())
            rows = session.execute(statement).all()

        # Filtered in Python rather than via SQL LOWER()/LIKE: SQLite's
        # built-in LOWER only folds ASCII, so a Cyrillic query (the normal
        # case here - ФИО, дата рождения) would silently match nothing.
        needle = query.strip().casefold()
        results = []
        for patient, study_count, last_study_at in rows:
            if needle and needle not in patient.full_name.casefold() and needle not in patient.patient_id.casefold() and needle not in patient.birth_date.casefold():
                continue
            results.append(self._patient_summary_from_orm(patient, study_count, last_study_at))
        return results

    def get_patient(self, patient_id: str, user_id: str | None = None) -> dict:
        with self.session() as session:
            patient = session.get(PatientORM, patient_id)
            if patient is None or (user_id is not None and patient.user_id != user_id):
                raise KeyError("patient not found")
            studies = session.scalars(
                select(StudyORM)
                .where(StudyORM.patient_record_id == patient_id)
                .order_by(StudyORM.created_at.desc())
            ).all()
            study_dicts = []
            for study in studies:
                finding_count, max_diameter = self._finding_summary(session, study.id)
                study_dicts.append(self._study_from_orm(study, finding_count, max_diameter))
            last_study_at = study_dicts[0]["created_at"] if study_dicts else None
            payload = self._patient_summary_from_orm(patient, len(study_dicts), last_study_at)
            payload["studies"] = study_dicts
            return payload

    def delete_patient(self, patient_id: str, user_id: str | None = None) -> None:
        with self._lock, self.session() as session, session.begin():
            patient = session.get(PatientORM, patient_id)
            if patient is None or (user_id is not None and patient.user_id != user_id):
                raise KeyError("patient not found")
            session.delete(patient)

    def create_finding(
        self,
        study_id: str,
        title: str,
        diameter_mm: float,
        confidence: float,
        source: str = "manual",
        user_id: str | None = None,
        slice_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        width: float | None = None,
        height: float | None = None,
        segment_label: str | None = None,
        model_name: str | None = None,
        threshold: float | None = None,
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
        with self._lock, self.session() as session, session.begin():
            study = self._load_study(session, study_id, user_id)
            session.add(
                FindingORM(
                    id=finding.id,
                    study_id=finding.study_id,
                    title=finding.title,
                    diameter_mm=finding.diameter_mm,
                    confidence=finding.confidence,
                    source=finding.source,
                    created_at=finding.created_at,
                    slice_index=slice_index,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    segment_label=segment_label,
                    model_name=model_name,
                    threshold=threshold,
                )
            )
            study.status = "reviewed"
        return {
            **finding.to_dict(),
            "slice_index": slice_index,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "segment_label": segment_label,
            "model_name": model_name,
            "threshold": threshold,
        }

    def create_job(self, study_id: str, user_id: str | None = None) -> dict:
        job = AnalysisJobORM(
            id=new_id(),
            study_id=study_id,
            status="queued",
            result_json=None,
            error=None,
            created_at=now_iso(),
            finished_at=None,
        )
        with self._lock, self.session() as session, session.begin():
            study = self._load_study(session, study_id, user_id)
            session.add(job)
            study.status = "queued"
        return self._job_from_orm(job)

    def get_job(self, job_id: str, user_id: str | None = None) -> dict:
        with self.session() as session:
            job = session.get(AnalysisJobORM, job_id)
            if job is None:
                raise KeyError("job not found")
            if user_id is not None and job.study.user_id != user_id:
                raise KeyError("job not found")
            return self._job_from_orm(job)

    def pending_job_ids(self) -> list[str]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(AnalysisJobORM.id)
                    .where(AnalysisJobORM.status.in_(("queued", "running")))
                    .order_by(AnalysisJobORM.created_at)
                )
            )

    def claim_job(self, job_id: str) -> dict | None:
        with self._lock, self.session() as session, session.begin():
            job = session.get(AnalysisJobORM, job_id)
            if job is None or job.status != "queued":
                return None
            job.status = "running"
            return self._job_from_orm(job)

    def recover_active_jobs(self) -> None:
        with self._lock, self.session() as session, session.begin():
            session.execute(
                update(AnalysisJobORM)
                .where(AnalysisJobORM.status == "running")
                .values(status="queued", error=None, finished_at=None)
            )

    def mark_job_running(self, job_id: str) -> None:
        self._update_job(job_id, status="running")

    def mark_job_done(self, job_id: str, result: dict) -> None:
        with self._lock, self.session() as session, session.begin():
            job = session.get(AnalysisJobORM, job_id)
            if job is None:
                raise KeyError("job not found")
            job.status = "done"
            job.result_json = json.dumps(result, ensure_ascii=False)
            job.error = None
            job.finished_at = now_iso()
            job.study.status = "analyzed"

    def mark_job_failed(self, job_id: str, error: str) -> None:
        with self._lock, self.session() as session, session.begin():
            job = session.get(AnalysisJobORM, job_id)
            if job is None:
                raise KeyError("job not found")
            job.status = "failed"
            job.error = error
            job.finished_at = now_iso()
            job.study.status = "error"

    def save_report(self, study_id: str, report: str, user_id: str | None = None, source: str = "") -> dict:
        report = report.strip()
        if not report:
            raise ValueError("report is required")
        with self._lock, self.session() as session, session.begin():
            study = self._load_study(session, study_id, user_id)
            study.report = report
            study.report_source = source
            study.status = "reported"
        return self.get_study(study_id, user_id)

    def stats(self) -> dict:
        with self.session() as session:
            studies = session.scalar(select(func.count(StudyORM.id))) or 0
            findings = session.scalar(select(func.count(FindingORM.id))) or 0
            queued = (
                session.scalar(
                    select(func.count(AnalysisJobORM.id)).where(AnalysisJobORM.status.in_(("queued", "running")))
                )
                or 0
            )
        return {"studies": studies, "findings": findings, "active_jobs": queued}

    def _update_job(self, job_id: str, **fields: str) -> None:
        if not fields:
            return
        with self._lock, self.session() as session, session.begin():
            job = session.get(AnalysisJobORM, job_id)
            if job is None:
                raise KeyError("job not found")
            for name, value in fields.items():
                setattr(job, name, value)

    @staticmethod
    def _charge_request_locked(user: UserORM | None) -> None:
        if user is None:
            raise PermissionError("требуется вход")
        if user.balance_kopeks < REQUEST_PRICE_KOPEKS:
            raise InsufficientBalanceError("недостаточно средств")
        user.balance_kopeks -= REQUEST_PRICE_KOPEKS

    @staticmethod
    def _load_study(session: Session, study_id: str, user_id: str | None = None) -> StudyORM:
        study = session.get(StudyORM, study_id)
        if study is None or (user_id is not None and study.user_id != user_id):
            raise KeyError("study not found")
        return study

    @staticmethod
    def _finding_summary(session: Session, study_id: str) -> tuple[int, float]:
        row = session.execute(
            select(
                func.count(FindingORM.id),
                func.coalesce(func.max(FindingORM.diameter_mm), 0),
            ).where(FindingORM.study_id == study_id)
        ).one()
        return int(row[0] or 0), float(row[1] or 0)

    @staticmethod
    def _ensure_admin_user(session: Session) -> str:
        admin = session.scalar(select(UserORM).where(UserORM.username == "admin"))
        if admin is not None:
            return admin.id

        admin = UserORM(
            id=new_id(),
            username="admin",
            password_hash=hash_password("admin"),
            balance_kopeks=0,
            auth_token=secrets.token_urlsafe(32),
            created_at=now_iso(),
        )
        session.add(admin)
        return admin.id

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
    def _user_from_orm(cls, user: UserORM, include_token: bool = False) -> dict:
        payload = {
            "id": user.id,
            "username": user.username,
            "balance": cls._kopeks_to_rubles(user.balance_kopeks),
            "request_price": cls._kopeks_to_rubles(REQUEST_PRICE_KOPEKS),
            "created_at": user.created_at,
        }
        if include_token:
            payload["token"] = user.auth_token
        return payload

    @staticmethod
    def _study_from_orm(study: StudyORM, finding_count: int = 0, max_diameter_mm: float = 0) -> dict:
        return {
            "id": study.id,
            "user_id": study.user_id,
            "patient_name": study.patient_name,
            "patient_id": study.patient_id,
            "birth_date": study.birth_date,
            "description": study.description,
            "report": study.report,
            "report_source": study.report_source,
            "status": study.status,
            "created_at": study.created_at,
            "patient_record_id": study.patient_record_id,
            "finding_count": int(finding_count or 0),
            "max_diameter_mm": float(max_diameter_mm or 0),
        }

    @staticmethod
    def _patient_summary_from_orm(patient: PatientORM, study_count: int = 0, last_study_at: str | None = None) -> dict:
        return {
            "id": patient.id,
            "user_id": patient.user_id,
            "full_name": patient.full_name,
            "birth_date": patient.birth_date,
            "patient_id": patient.patient_id,
            "created_at": patient.created_at,
            "study_count": int(study_count or 0),
            "last_study_at": last_study_at,
        }

    @staticmethod
    def _finding_from_orm(finding: FindingORM) -> dict:
        return {
            "id": finding.id,
            "study_id": finding.study_id,
            "title": finding.title,
            "diameter_mm": finding.diameter_mm,
            "confidence": finding.confidence,
            "source": finding.source,
            "created_at": finding.created_at,
            "slice_index": finding.slice_index,
            "x": finding.x,
            "y": finding.y,
            "width": finding.width,
            "height": finding.height,
            "segment_label": finding.segment_label,
            "model_name": finding.model_name,
            "threshold": finding.threshold,
        }

    @staticmethod
    def _job_from_orm(job: AnalysisJobORM) -> dict:
        result = json.loads(job.result_json) if job.result_json else None
        return {
            "id": job.id,
            "study_id": job.study_id,
            "status": job.status,
            "result": result,
            "error": job.error,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }
