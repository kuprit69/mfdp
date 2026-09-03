from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Study:
    patient_name: str
    patient_id: str = ""
    birth_date: str = ""
    description: str = ""
    report: str = ""
    status: str = "new"
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Patient:
    full_name: str
    birth_date: str = ""
    patient_id: str = ""
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Finding:
    study_id: str
    title: str
    diameter_mm: float
    confidence: float
    source: str = "manual"
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisJob:
    study_id: str
    status: str = "queued"
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
