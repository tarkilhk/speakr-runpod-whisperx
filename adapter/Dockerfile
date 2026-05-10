FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN python -m pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn[standard]==0.34.0 \
    httpx==0.28.1 \
    python-multipart==0.0.20

COPY adapter/app.py /app/app.py
COPY adapter/adapter /app/adapter
COPY speakr_common /app/speakr_common

EXPOSE 9000

USER 1000:1000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
