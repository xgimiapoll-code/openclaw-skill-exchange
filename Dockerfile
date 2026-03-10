FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY app/ app/

RUN pip install --no-cache-dir . && mkdir -p data

EXPOSE 8100

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
