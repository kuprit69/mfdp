from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from .rate_limiter import RateLimiter
    from .slice_store import SliceStore
    from .storage import InsufficientBalanceError, Storage
    from .workers import ModelWorkerPool
except ImportError:  # pragma: no cover - used when running this file directly.
    from rate_limiter import RateLimiter
    from slice_store import SliceStore
    from storage import InsufficientBalanceError, Storage
    from workers import ModelWorkerPool


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_POSTGRES_URL = "postgresql+psycopg://lung:lung@127.0.0.1:5432/lung_prometheus"

# A no-op if the root logger already has handlers (e.g. uvicorn configured its
# own loggers first) - this just makes sure "backend.*" loggers have somewhere
# to go with a readable format, instead of silently relying on logging's
# bare-bones "handler of last resort" fallback.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Quiet down chatty third-party HTTP client logging (httpx is used by
# FastAPI's TestClient in tests, and would otherwise log every request at
# INFO) - keep it to warnings and above.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    storage: Storage
    workers: ModelWorkerPool
    public_dir: Path
    slice_store: SliceStore
    nodule_model: object | None = None
    # 5 failed attempts per IP+username pair per minute before login is
    # throttled - generous enough for a person mistyping a password, tight
    # enough to make brute-forcing impractical.
    login_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(max_attempts=5, window_seconds=60.0))


class AuthPayload(BaseModel):
    username: str = ""
    password: str = ""


class TopUpPayload(BaseModel):
    amount: Any = None


class StudyPayload(BaseModel):
    patient_name: str = ""
    patient_id: str = ""
    description: str = ""
    birth_date: str = ""


class PatientMatchPayload(BaseModel):
    full_name: str = ""
    birth_date: str = ""


class PatientPayload(BaseModel):
    full_name: str = ""
    birth_date: str = ""
    patient_id: str = ""


class PatientStudyPayload(BaseModel):
    description: str = ""


class FindingPayload(BaseModel):
    title: str = ""
    diameter_mm: float = 0
    confidence: float = 0.8
    source: str = "manual"


class ViewerAnalyzePayload(BaseModel):
    slices: list[dict[str, Any]] = []


class AnalyzeStudyPayload(BaseModel):
    # Slices captured during the current upload, sent along with the analyze
    # request so the worker has real pixel data to run the model on. May be
    # omitted (empty list) to re-run analysis on slices already stored from
    # an earlier upload of the same study.
    slices: list[dict[str, Any]] = []


def default_database_url() -> str:
    return os.getenv("DATABASE_URL") or DEFAULT_POSTGRES_URL


def resolve_public_dir(public_dir: str | Path | None = None) -> Path:
    return Path(public_dir or os.getenv("PUBLIC_DIR") or ROOT_DIR / "public").resolve()


def cors_allowed_origins() -> list[str]:
    """Explicit allowlist instead of "*" - the browser UI is actually served
    by this same FastAPI app (see the StaticFiles mount below), so normal
    usage is same-origin and doesn't need CORS at all. This only matters for
    a frontend served from elsewhere (a separate dev server, a different
    port) or direct cross-origin API access - keep it to known hosts rather
    than opening the API to every website on the internet."""
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if origins:
        return origins
    return [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8775",
        "http://localhost:8775",
    ]


def create_app(
    context: AppContext | None = None,
    *,
    public_dir: str | Path | None = None,
    database_url: str | None = None,
    worker_count: int | None = None,
    redis_url: str | None = None,
) -> FastAPI:
    public_path = resolve_public_dir(public_dir)

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        if not hasattr(fastapi_app.state, "context"):
            if context is not None:
                fastapi_app.state.context = context
            else:
                storage = Storage(database_url or default_database_url())
                slice_store = SliceStore()
                workers = ModelWorkerPool(
                    storage,
                    worker_count or int(os.getenv("MODEL_WORKERS", "1")),
                    redis_url=redis_url or os.getenv("REDIS_URL", ""),
                    slice_store=slice_store,
                )
                fastapi_app.state.context = AppContext(
                    storage=storage,
                    workers=workers,
                    public_dir=public_path,
                    slice_store=slice_store,
                )
        try:
            yield
        finally:
            if hasattr(fastapi_app.state, "context"):
                fastapi_app.state.context.workers.shutdown()
                fastapi_app.state.context.storage.close()

    app = FastAPI(title="LungPrometheus API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if context is not None:
        app.state.context = context

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "error": str(exc.detail)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"ok": False, "error": str(exc)},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Anything that reaches here is a bug, not an expected user error -
        # log the full traceback so it's actually visible somewhere (previously
        # such exceptions could get silently turned into a plain 400/500 with
        # nothing recorded server-side), and keep the API's response JSON
        # instead of FastAPI's default HTML error page.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"ok": False, "error": "internal server error"},
        )

    @app.get("/api/health")
    def health(ctx: AppContext = Depends(get_context)) -> dict:
        return {
            "ok": True,
            "service": "lung-prometheus",
            "database": sanitize_database_url(ctx.storage.database_url),
            "workers": ctx.workers.worker_count,
            "queue": ctx.workers.queue_backend,
        }

    @app.get("/api/auth/me")
    def auth_me(user: dict = Depends(get_current_user)) -> dict:
        return {"ok": True, "user": user}

    @app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
    def auth_register(payload: AuthPayload, ctx: AppContext = Depends(get_context)) -> dict:
        try:
            user = ctx.storage.create_user(payload.username, payload.password)
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        token = str(user.pop("token"))
        return {"ok": True, "user": user, "token": token}

    @app.post("/api/auth/login")
    def auth_login(payload: AuthPayload, request: Request, ctx: AppContext = Depends(get_context)) -> dict:
        limiter_key = f"{client_ip(request)}:{payload.username.strip().lower()}"
        if not ctx.login_limiter.is_allowed(limiter_key):
            retry_after = ctx.login_limiter.retry_after_seconds(limiter_key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="слишком много попыток входа, попробуйте позже",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        try:
            user = ctx.storage.authenticate_user(payload.username, payload.password)
        except PermissionError as exc:
            ctx.login_limiter.record(limiter_key)
            raise api_error(status.HTTP_401_UNAUTHORIZED, exc) from exc
        ctx.login_limiter.reset(limiter_key)
        token = str(user.pop("token"))
        return {"ok": True, "user": user, "token": token}

    @app.post("/api/auth/logout")
    def auth_logout(user: dict = Depends(get_current_user), ctx: AppContext = Depends(get_context)) -> dict:
        ctx.storage.logout(user["id"])
        return {"ok": True}

    @app.post("/api/balance/top-up")
    def top_up(
        payload: TopUpPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            updated_user = ctx.storage.top_up_balance(user["id"], payload.amount)
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        return {"ok": True, "user": updated_user}

    @app.get("/api/stats")
    def stats(ctx: AppContext = Depends(get_context)) -> dict:
        data = ctx.storage.stats()
        data["workers"] = ctx.workers.worker_count
        data["queue"] = ctx.workers.queue_backend
        return {"ok": True, "stats": data}

    @app.get("/api/studies")
    def list_studies(user: dict = Depends(get_current_user), ctx: AppContext = Depends(get_context)) -> dict:
        studies = ctx.storage.list_studies(user["id"])
        for study in studies:
            study["has_slices"] = ctx.slice_store.exists(study["id"])
        return {"ok": True, "studies": studies}

    @app.post("/api/studies", status_code=status.HTTP_201_CREATED)
    def create_study(
        payload: StudyPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            study = ctx.storage.create_study(
                patient_name=payload.patient_name,
                patient_id=payload.patient_id,
                description=payload.description,
                birth_date=payload.birth_date,
                user_id=user["id"],
                charge_request=True,
            )
            updated_user = ctx.storage.get_user(user["id"])
        except InsufficientBalanceError as exc:
            raise api_error(status.HTTP_402_PAYMENT_REQUIRED, exc) from exc
        except (PermissionError, ValueError) as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        study["has_slices"] = ctx.slice_store.exists(study["id"])
        return {"ok": True, "study": study, "user": updated_user}

    @app.get("/api/studies/{study_id}")
    def get_study(
        study_id: str,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            study = ctx.storage.get_study(study_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        study["has_slices"] = ctx.slice_store.exists(study_id)
        return {"ok": True, "study": study}

    @app.get("/api/studies/{study_id}/slices")
    def get_study_slices(
        study_id: str,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            ctx.storage.get_study(study_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        return {"ok": True, "slices": ctx.slice_store.load(study_id) or []}

    @app.delete("/api/studies/{study_id}")
    def delete_study(
        study_id: str,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            ctx.storage.delete_study(study_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        ctx.slice_store.delete(study_id)
        return {"ok": True}

    @app.post("/api/patients/match")
    def match_patient(
        payload: PatientMatchPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        match = ctx.storage.find_matching_patient(payload.full_name, payload.birth_date, user["id"])
        return {"ok": True, "match": match}

    @app.get("/api/patients")
    def list_patients(
        query: str = "",
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        patients = ctx.storage.list_patients(user["id"], query=query)
        return {"ok": True, "patients": patients}

    @app.post("/api/patients", status_code=status.HTTP_201_CREATED)
    def create_patient(
        payload: PatientPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            patient = ctx.storage.create_patient(
                full_name=payload.full_name,
                birth_date=payload.birth_date,
                patient_id=payload.patient_id,
                user_id=user["id"],
            )
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        return {"ok": True, "patient": patient}

    @app.get("/api/patients/{patient_id}")
    def get_patient(
        patient_id: str,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            patient = ctx.storage.get_patient(patient_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        for study in patient["studies"]:
            study["has_slices"] = ctx.slice_store.exists(study["id"])
        return {"ok": True, "patient": patient}

    @app.delete("/api/patients/{patient_id}")
    def delete_patient(
        patient_id: str,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            ctx.storage.delete_patient(patient_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        return {"ok": True}

    @app.post("/api/patients/{patient_id}/studies", status_code=status.HTTP_201_CREATED)
    def create_patient_study(
        patient_id: str,
        payload: PatientStudyPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            study = ctx.storage.create_study_for_patient(
                patient_record_id=patient_id,
                description=payload.description,
                user_id=user["id"],
            )
            updated_user = ctx.storage.get_user(user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        except InsufficientBalanceError as exc:
            raise api_error(status.HTTP_402_PAYMENT_REQUIRED, exc) from exc
        except (PermissionError, ValueError) as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        study["has_slices"] = ctx.slice_store.exists(study["id"])
        return {"ok": True, "study": study, "user": updated_user}

    @app.post("/api/studies/{study_id}/findings", status_code=status.HTTP_201_CREATED)
    def create_finding(
        study_id: str,
        payload: FindingPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            finding = ctx.storage.create_finding(
                study_id=study_id,
                title=payload.title,
                diameter_mm=payload.diameter_mm,
                confidence=payload.confidence,
                source=payload.source,
                user_id=user["id"],
            )
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        except ValueError as exc:
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        return {"ok": True, "finding": finding}

    @app.post("/api/studies/{study_id}/analyze", status_code=status.HTTP_202_ACCEPTED)
    def create_analysis_job(
        study_id: str,
        payload: AnalyzeStudyPayload,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            ctx.storage.get_study(study_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc

        if payload.slices:
            ctx.slice_store.save(study_id, payload.slices)
        if not ctx.slice_store.exists(study_id):
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "нет сохранённых срезов для этого исследования — сначала загрузите файл",
            )

        try:
            job = ctx.storage.create_job(study_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        ctx.workers.enqueue(job["id"])
        return {"ok": True, "job": job}

    @app.get("/api/jobs/{job_id}")
    def get_job(
        job_id: str,
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            job = ctx.storage.get_job(job_id, user["id"])
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        return {"ok": True, "job": job}

    @app.post("/api/model/analyze")
    def model_analyze(
        payload: dict[str, Any],
        user: dict = Depends(get_current_user),  # noqa: ARG001
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            model = get_nodule_model(ctx)
            annotations = model.analyze(payload)
        except Exception as exc:  # noqa: BLE001 - keep prototype responses JSON.
            logger.exception("model_analyze failed")
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        return {
            "ok": True,
            "annotations": annotations,
            "model": model_result_metadata(model),
        }

    @app.post("/api/viewer/analyze")
    def viewer_analyze(
        payload: ViewerAnalyzePayload,
        user: dict = Depends(get_current_user),  # noqa: ARG001
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            if not payload.slices:
                raise ValueError("slices are required")
            if any((item.get("pixelData") or {}).get("data") for item in payload.slices):
                model = get_nodule_model(ctx)
                detections = model.analyze({"slices": payload.slices})
                model_meta = model_result_metadata(model)
            else:
                detections = []
                model_meta = {
                    "name": "no-medical-pixels",
                    "probability": 0.0,
                    "threshold": None,
                    "message": "Для анализа моделью загрузите DICOM или MHD/RAW с пиксельными данными.",
                }
        except (TypeError, ValueError) as exc:
            # Expected user-input errors (missing/malformed slices) - not
            # worth a full traceback, just a note of what was rejected.
            logger.info("viewer_analyze rejected input: %s", exc)
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        except Exception as exc:  # noqa: BLE001 - keep model errors JSON for the UI.
            logger.exception("viewer_analyze failed")
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        return {"ok": True, "detections": detections, "model": model_meta}

    @app.post("/api/reports/generate")
    def generate_report(
        payload: dict[str, Any],
        user: dict = Depends(get_current_user),
        ctx: AppContext = Depends(get_context),
    ) -> dict:
        try:
            report, source = request_report_from_fastapi(payload)
            study_id = str(payload.get("study_id") or "")
            study = ctx.storage.save_report(study_id, report, user["id"], source=source) if study_id else None
        except KeyError as exc:
            raise api_error(status.HTTP_404_NOT_FOUND, exc) from exc
        except Exception as exc:  # noqa: BLE001 - report endpoint should stay JSON.
            logger.exception("generate_report failed")
            raise api_error(status.HTTP_400_BAD_REQUEST, exc) from exc
        return {"ok": True, "report": report, "source": source, "study": study}

    if public_path.exists():
        app.mount("/", StaticFiles(directory=str(public_path), html=True), name="public")

    return app


def get_context(request: Request) -> AppContext:
    if not hasattr(request.app.state, "context"):
        raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "service is starting")
    return request.app.state.context


def get_current_user(
    request: Request,
    x_auth_token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    ctx = get_context(request)
    try:
        return ctx.storage.get_user_by_token(x_auth_token)
    except PermissionError as exc:
        raise api_error(status.HTTP_401_UNAUTHORIZED, exc) from exc


def api_error(status_code: int, error: object) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(error))


def client_ip(request: Request) -> str:
    # Trust X-Forwarded-For's first hop when present (e.g. behind a reverse
    # proxy in Docker); fall back to the direct connection address.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def sanitize_database_url(database_url: str) -> str:
    if "://" not in database_url or "@" not in database_url:
        return database_url
    scheme, rest = database_url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def request_report_from_fastapi(payload: dict[str, Any]) -> tuple[str, str]:
    """Returns (report_text, source) where source is "ollama" when the LLM
    produced the text, or "fallback" when either the report service itself
    fell back to its template, or this API couldn't reach the report service
    at all and used the local template instead."""
    service_url = os.getenv("REPORT_SERVICE_URL", "http://127.0.0.1:8766/api/reports/generate")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = UrlRequest(
        service_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
    except URLError:
        try:
            from .report_service import fallback_report
        except ImportError:
            from report_service import fallback_report
        return fallback_report(payload), "fallback"

    if not data.get("ok"):
        raise ValueError(data.get("error") or "report service error")
    report = str(data.get("report") or "").strip()
    source = str(data.get("source") or "fallback")
    return report, source


def model_result_metadata(model: object) -> dict:
    return {
        "name": getattr(model, "model_name", "3D CNN"),
        "probability": getattr(model, "last_probability", None),
        "threshold": getattr(model, "last_threshold", None),
        "checkpoint_threshold": getattr(model, "checkpoint_threshold", None),
        "weights_loaded": getattr(model, "weights_loaded", False),
    }


def get_nodule_model(context: AppContext) -> object:
    if context.nodule_model is None:
        try:
            from .model_adapter import NoduleModel
        except ImportError:
            from model_adapter import NoduleModel
        context.nodule_model = NoduleModel()
    return context.nodule_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LungPrometheus FastAPI service")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    parser.add_argument("--public", default=os.getenv("PUBLIC_DIR", str(ROOT_DIR / "public")))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", ""))
    parser.add_argument("--workers", type=int, default=int(os.getenv("MODEL_WORKERS", "1")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_url = args.database_url or default_database_url()
    runtime_app = create_app(
        public_dir=args.public,
        database_url=database_url,
        worker_count=args.workers,
        redis_url=args.redis_url,
    )
    print(f"LungPrometheus: http://{args.host}:{args.port}", flush=True)
    uvicorn.run(runtime_app, host=args.host, port=args.port, log_level="info")


app = create_app()


if __name__ == "__main__":
    main()
