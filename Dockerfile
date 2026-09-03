FROM python:3.12-slim AS base

WORKDIR /app

ENV HOST=0.0.0.0
ENV PORT=8765
ENV PUBLIC_DIR=/app/public
ENV DATABASE_URL=postgresql+psycopg://lung:lung@db:5432/lung_prometheus
ENV REDIS_URL=redis://redis:6379/0
ENV MODEL_WEIGHTS_PATH=/app/backend/weights/improved_3dcnn_checkpoint.pth
ENV MODEL_DETECTION_THRESHOLD=0.85
ENV MODEL_WORKERS=2

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS app

RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

COPY backend ./backend
COPY public ./public
COPY README.md .

EXPOSE 8765

CMD ["python", "backend/server.py"]

FROM base AS report-api

COPY backend ./backend
COPY README.md .

EXPOSE 8766

CMD ["uvicorn", "backend.report_service:app", "--host", "0.0.0.0", "--port", "8766"]
