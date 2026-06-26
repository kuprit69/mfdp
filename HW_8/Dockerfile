FROM python:3.12-slim

WORKDIR /app

ENV HOST=0.0.0.0
ENV PORT=8765
ENV PUBLIC_DIR=/app/public
ENV DB_PATH=/app/data/app.sqlite3
ENV MODEL_WORKERS=2

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY public ./public
COPY README.md .

EXPOSE 8765 8766

CMD ["python", "backend/server.py"]
