from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import sys

try:
    from .storage import InsufficientBalanceError, Storage
    from .workers import ModelWorkerPool
except ImportError:  # pragma: no cover - used when running this file directly.
    from storage import InsufficientBalanceError, Storage
    from workers import ModelWorkerPool


@dataclass
class AppContext:
    storage: Storage
    workers: ModelWorkerPool
    public_dir: Path
    nodule_model: object | None = None


class PrototypeRequestHandler(SimpleHTTPRequestHandler):
    app_context: AppContext

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.clean_path()
        parts = self.path_parts(path)

        try:
            if path == "/api/health":
                self.send_json(
                    {
                        "ok": True,
                        "service": "lung-prometheus",
                        "database": str(self.app_context.storage.path),
                        "workers": self.app_context.workers.worker_count,
                    }
                )
                return

            if path == "/api/auth/me":
                self.send_json({"ok": True, "user": self.current_user()})
                return

            if path == "/api/stats":
                stats = self.app_context.storage.stats()
                stats["workers"] = self.app_context.workers.worker_count
                self.send_json({"ok": True, "stats": stats})
                return

            if parts == ["api", "studies"]:
                user = self.current_user()
                studies = self.app_context.storage.list_studies(user["id"])
                self.send_json({"ok": True, "studies": studies})
                return

            if len(parts) == 3 and parts[:2] == ["api", "studies"]:
                user = self.current_user()
                study = self.app_context.storage.get_study(parts[2], user["id"])
                self.send_json({"ok": True, "study": study})
                return

            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                user = self.current_user()
                job = self.app_context.storage.get_job(parts[2], user["id"])
                self.send_json({"ok": True, "job": job})
                return
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except PermissionError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return

        super().do_GET()

    def do_POST(self) -> None:
        path = self.clean_path()
        parts = self.path_parts(path)

        try:
            if path == "/api/auth/register":
                payload = self.read_json()
                user = self.app_context.storage.create_user(
                    username=str(payload.get("username", "")),
                    password=str(payload.get("password", "")),
                )
                token = str(user.pop("token"))
                self.send_json({"ok": True, "user": user, "token": token}, HTTPStatus.CREATED)
                return

            if path == "/api/auth/login":
                payload = self.read_json()
                user = self.app_context.storage.authenticate_user(
                    username=str(payload.get("username", "")),
                    password=str(payload.get("password", "")),
                )
                token = str(user.pop("token"))
                self.send_json({"ok": True, "user": user, "token": token})
                return

            if path == "/api/balance/top-up":
                user = self.current_user()
                payload = self.read_json()
                updated_user = self.app_context.storage.top_up_balance(user["id"], payload.get("amount"))
                self.send_json({"ok": True, "user": updated_user})
                return

            if parts == ["api", "studies"]:
                user = self.current_user()
                payload = self.read_json()
                study = self.app_context.storage.create_study(
                    patient_name=str(payload.get("patient_name", "")),
                    patient_id=str(payload.get("patient_id", "")),
                    description=str(payload.get("description", "")),
                    birth_date=str(payload.get("birth_date", "")),
                    user_id=user["id"],
                    charge_request=True,
                )
                updated_user = self.app_context.storage.get_user(user["id"])
                self.send_json({"ok": True, "study": study, "user": updated_user}, HTTPStatus.CREATED)
                return

            if len(parts) == 4 and parts[:2] == ["api", "studies"] and parts[3] == "findings":
                user = self.current_user()
                payload = self.read_json()
                finding = self.app_context.storage.create_finding(
                    study_id=parts[2],
                    title=str(payload.get("title", "")),
                    diameter_mm=float(payload.get("diameter_mm", 0)),
                    confidence=float(payload.get("confidence", 0.8)),
                    source=str(payload.get("source", "manual")),
                    user_id=user["id"],
                )
                self.send_json({"ok": True, "finding": finding}, HTTPStatus.CREATED)
                return

            if len(parts) == 4 and parts[:2] == ["api", "studies"] and parts[3] == "analyze":
                user = self.current_user()
                job = self.app_context.storage.create_job(parts[2], user["id"])
                self.app_context.workers.enqueue(job["id"])
                self.send_json({"ok": True, "job": job}, HTTPStatus.ACCEPTED)
                return

            if path == "/api/model/analyze":
                self.handle_model_analyze()
                return

            if path == "/api/viewer/analyze":
                self.handle_viewer_analyze()
                return

            if path == "/api/reports/generate":
                self.handle_generate_report()
                return
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except PermissionError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return
        except InsufficientBalanceError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.PAYMENT_REQUIRED)
            return
        except (TypeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def do_DELETE(self) -> None:
        parts = self.path_parts(self.clean_path())
        try:
            if len(parts) == 3 and parts[:2] == ["api", "studies"]:
                user = self.current_user()
                self.app_context.storage.delete_study(parts[2], user["id"])
                self.send_json({"ok": True})
                return
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except PermissionError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def handle_model_analyze(self) -> None:
        try:
            self.current_user()
            payload = self.read_json()
            model = self.get_nodule_model()
            annotations = model.analyze(payload)
        except PermissionError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return
        except Exception as exc:  # noqa: BLE001 - response should stay JSON in prototype.
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_json({"ok": True, "annotations": annotations})

    def handle_viewer_analyze(self) -> None:
        try:
            self.current_user()
            payload = self.read_json()
            slices = payload.get("slices") or []
            if not slices:
                raise ValueError("slices are required")
            detections = [self.detect_pathology(slices)]
        except PermissionError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return
        except (TypeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_json({"ok": True, "detections": detections})

    def handle_generate_report(self) -> None:
        try:
            user = self.current_user()
            payload = self.read_json()
            report = self.request_report_from_fastapi(payload)
            study_id = str(payload.get("study_id") or "")
            study = None
            if study_id:
                study = self.app_context.storage.save_report(study_id, report, user["id"])
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except PermissionError as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return
        except Exception as exc:  # noqa: BLE001 - report endpoint should stay JSON.
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_json({"ok": True, "report": report, "study": study})

    def current_user(self) -> dict:
        token = self.headers.get("X-Auth-Token", "").strip()
        return self.app_context.storage.get_user_by_token(token)

    @staticmethod
    def request_report_from_fastapi(payload: dict) -> str:
        service_url = os.getenv("REPORT_SERVICE_URL", "http://127.0.0.1:8766/api/reports/generate")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
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
            return fallback_report(payload)

        if not data.get("ok"):
            raise ValueError(data.get("error") or "report service error")
        return str(data.get("report") or "").strip()

    @staticmethod
    def detect_pathology(slices: list[dict]) -> dict:
        seed_text = "|".join(str(item.get("name", "")) for item in slices)
        digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
        slice_index = digest[0] % len(slices)
        image = slices[slice_index]
        width = max(1, int(image.get("width") or 1))
        height = max(1, int(image.get("height") or 1))
        smaller_side = min(width, height)
        box_size = max(1, min(smaller_side * 0.22, smaller_side * (0.08 + digest[1] / 255 * 0.04)))
        x_space = max(1, width - box_size)
        y_space = max(1, height - box_size)

        return {
            "id": hashlib.sha1(seed_text.encode("utf-8")).hexdigest()[:12],
            "title": "Подозрительный объект",
            "sliceIndex": slice_index,
            "x": round((digest[2] / 255) * x_space, 1),
            "y": round((digest[3] / 255) * y_space, 1),
            "width": round(box_size, 1),
            "height": round(box_size, 1),
            "confidence": round(0.72 + digest[4] / 255 * 0.22, 2),
        }

    def get_nodule_model(self) -> object:
        if self.app_context.nodule_model is None:
            try:
                from .model_adapter import NoduleModel
            except ImportError:
                from model_adapter import NoduleModel
            self.app_context.nodule_model = NoduleModel()
        return self.app_context.nodule_model

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def clean_path(self) -> str:
        return urlparse(self.path).path

    @staticmethod
    def path_parts(path: str) -> list[str]:
        return [part for part in path.split("/") if part]

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("[python-backend] " + (format % args) + "\n")


def build_handler(context: AppContext):
    class BoundRequestHandler(PrototypeRequestHandler):
        app_context = context

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(context.public_dir), **kwargs)

    return BoundRequestHandler


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Simple LungPrometheus Python MVP")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    parser.add_argument("--public", default=os.getenv("PUBLIC_DIR", str(root / "public")))
    parser.add_argument("--db", default=os.getenv("DB_PATH", str(root / "data" / "app.sqlite3")))
    parser.add_argument("--workers", type=int, default=int(os.getenv("MODEL_WORKERS", "1")))
    return parser.parse_args()


def create_context(args: argparse.Namespace) -> AppContext:
    public_dir = Path(args.public).resolve()
    if not public_dir.exists():
        raise SystemExit(f"Public directory does not exist: {public_dir}")

    storage = Storage(args.db)
    workers = ModelWorkerPool(storage, args.workers)
    return AppContext(storage=storage, workers=workers, public_dir=public_dir)


def main() -> None:
    args = parse_args()
    context = create_context(args)
    server = ThreadingHTTPServer((args.host, args.port), build_handler(context))
    print(f"LungPrometheus: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        context.workers.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
