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
    probability: float | None = None
    diameterMm: float | None = None
    width: float = 0.0
    height: float = 0.0
    modelName: str | None = None
    threshold: float | None = None
    segment: dict | None = None


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
    finding_lines = [format_detection_for_prompt(item, index) for index, item in enumerate(findings, start=1)]
    findings_text = "\n".join(finding_lines) if finding_lines else "- Патологические объекты моделью не обнаружены."

    return (
        "Ты врач-рентгенолог. Сформируй структурированное КТ-заключение на русском языке "
        "для учебного сервиса. Используй только переданные данные, не выдумывай новые очаги, "
        "лимфоузлы, плевральный выпот или метастазы, если этого нет во входных данных. "
        "Формулируй осторожно: модель выявила подозрительный объект, требуется верификация врачом. "
        "Обязательно укажи локализацию, характер образования, ориентировочный размер и вероятность модели. "
        "Если находок нет, напиши, что автоматическая модель подозрительных очагов не отметила.\n\n"
        f"Пациент: {data.get('patient_name') or 'не указан'}\n"
        f"Дата рождения: {data.get('birth_date') or 'не указана'}\n"
        f"Количество срезов: {data.get('slices_count') or 0}\n"
        f"Находки модели:\n{findings_text}\n\n"
        "Строго соблюдай структуру:\n"
        "1. Методика.\n"
        "2. Описание.\n"
        "3. Заключение.\n"
        "4. Рекомендации.\n"
        "Не добавляй дисклеймеры вне раздела рекомендаций."
    )


def fallback_report(data: dict) -> str:
    patient = data.get("patient_name") or "Пациент не указан"
    birth_date = data.get("birth_date") or "дата рождения не указана"
    detections = data.get("detections") or []
    slices_count = data.get("slices_count") or 0

    if detections:
        first = detections[0]
        confidence = probability_percent(first)
        location = detection_location(first)
        diameter = detection_diameter(first)
        findings = (
            f"В {location} модель отметила подозрительный объект солидного характера "
            f"на срезе {int(first.get('sliceIndex') or 0) + 1}. "
            f"Ориентировочный размер: {diameter}. Вероятность модели: {confidence}%."
        )
        conclusion = (
            f"Подозрительное узловое образование {location}; требуется очная оценка "
            "врачом-рентгенологом с учетом исходных DICOM-данных."
        )
    else:
        findings = "Автоматическая модель не отметила патологические объекты."
        conclusion = "Убедительных автоматических признаков патологии не выявлено."

    return (
        f"Пациент: {patient}. Дата рождения: {birth_date}.\n\n"
        f"1. Методика: загружено {slices_count} срез(ов) КТ-исследования.\n\n"
        f"2. Описание: {findings}\n\n"
        f"3. Заключение: {conclusion}\n\n"
        "4. Рекомендации: результат сформирован автоматически и должен быть подтвержден специалистом."
    )


def format_detection_for_prompt(item: dict, index: int) -> str:
    return (
        f"- Находка {index}: {item.get('title') or 'подозрительный объект'}; "
        f"локализация: {detection_location(item)}; "
        f"срез: {int(item.get('sliceIndex') or 0) + 1}; "
        "характер: вероятное солидное узловое образование/очаг; "
        f"размер: {detection_diameter(item)}; "
        f"вероятность модели: {probability_percent(item)}%; "
        f"модель: {item.get('modelName') or 'не указана'}."
    )


def detection_location(item: dict) -> str:
    segment = item.get("segment") or {}
    if isinstance(segment, dict):
        label = segment.get("label") or segment.get("short")
        if label:
            return str(label)
    return "ориентировочной зоне, соответствующей координатам автоматической разметки"


def detection_diameter(item: dict) -> str:
    diameter = item.get("diameterMm")
    if diameter:
        return f"{float(diameter):.1f} мм"
    width = float(item.get("width") or 0)
    height = float(item.get("height") or 0)
    if width and height:
        return f"{width:.1f} x {height:.1f} px"
    return "размер не рассчитан"


def probability_percent(item: dict) -> int:
    probability = item.get("probability")
    if probability is None:
        probability = item.get("confidence") or 0
    return round(float(probability or 0) * 100)
