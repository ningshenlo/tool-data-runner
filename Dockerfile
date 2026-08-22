FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runner.py .
COPY taxonomy_shadow.py .
COPY anti_bot_signatures.py .
COPY classification_anomalies.py .
COPY pricing ./pricing
COPY sitemap_monitor ./sitemap_monitor

CMD ["python", "runner.py", "--all", "--loop", "--interval-seconds", "300"]
