from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI
from pydantic import BaseModel, Field


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

app = FastAPI(title="LungPrometheus Report Service")


class Detection(BaseModel):
    title: str = "Подозрительный объект"
    sliceIndex: int = 0
    confidence: float = 0.0
    width: float = 0.0
    height: float = 0.0


class ReportRequest(BaseModel):
    study_id: str | None = None
    patient_name: str = Field(default="")
    birth_date: str = Field(default="")
    slices_count: int = 0
    detections: list[Detection] = Field(default_factory=list)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "report-fastapi", "ollama_model": OLLAMA_MODEL}


@app.post("/api/reports/generate")
def generate_report(payload: ReportRequest) -> dict:
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    prompt = build_prompt(data)
    try:
        report = ask_ollama(prompt)
        source = "ollama"
    except Exception:
        report = fallback_report(data)
        source = "fallback"
    return {"ok": True, "report": report, "source": source}


def ask_ollama(prompt: str) -> str:
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        OLLAMA_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError("Ollama is not available") from exc

    text = str(result.get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response")
    return text


def build_prompt(data: dict) -> str:
    findings = data.get("detections") or []
    finding_lines = []
    for item in findings:
        confidence = round(float(item.get("confidence") or 0) * 100)
        finding_lines.append(
            f"- {item.get('title', 'Подозрительный объект')}: "
            f"срез {int(item.get('sliceIndex') or 0) + 1}, уверенность {confidence}%"
        )
    findings_text = "\n".join(finding_lines) if finding_lines else "- Патологические объекты не обнаружены."

    return (
        "Сформируй короткое медицинское заключение на русском языке для учебного MVP. "
        "Не ставь окончательный диагноз, используй осторожные формулировки и рекомендацию "
        "проверки врачом-рентгенологом.\n\n"
        f"Пациент: {data.get('patient_name') or 'не указан'}\n"
        f"Дата рождения: {data.get('birth_date') or 'не указана'}\n"
        f"Количество срезов: {data.get('slices_count') or 0}\n"
        f"Находки модели:\n{findings_text}\n\n"
        "Структура: Методика, Находки, Заключение, Рекомендация."
    )


def fallback_report(data: dict) -> str:
    patient = data.get("patient_name") or "Пациент не указан"
    birth_date = data.get("birth_date") or "дата рождения не указана"
    detections = data.get("detections") or []
    slices_count = data.get("slices_count") or 0

    if detections:
        first = detections[0]
        confidence = round(float(first.get("confidence") or 0) * 100)
        findings = (
            f"Модель отметила подозрительный объект на срезе "
            f"{int(first.get('sliceIndex') or 0) + 1}, уверенность {confidence}%."
        )
        conclusion = "КТ-признаки требуют очной проверки врачом-рентгенологом."
    else:
        findings = "Автоматическая модель не отметила патологические объекты."
        conclusion = "Убедительных автоматических признаков патологии не выявлено."

    return (
        f"Пациент: {patient}. Дата рождения: {birth_date}.\n\n"
        f"Методика: загружено {slices_count} срез(ов) исследования.\n\n"
        f"Находки: {findings}\n\n"
        f"Заключение: {conclusion}\n\n"
        "Рекомендация: результат сформирован автоматически и должен быть подтвержден специалистом."
    )
