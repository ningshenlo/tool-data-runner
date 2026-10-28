FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY requirements-postgres.txt .
RUN pip install --no-cache-dir -r requirements-postgres.txt
COPY postgres_client.py postgres_legacy.py postgres-query-catalog.json ./

COPY runner.py .
COPY report_exports.py .
COPY market_snapshot_refresh.py .
COPY search_demand_refresh.py .
COPY search_demand_brand.py .
COPY scripts/check-search-demand-publication.py ./scripts/
COPY scripts/check-market-publication.py ./scripts/
COPY report_review_api.py .
COPY report-markets.json .
COPY taxonomy_shadow.py .
COPY taxonomy_precision.py .
COPY taxonomy_schema.py .
COPY taxonomy_batch.py .
COPY jev.py .
COPY jev_capabilities.py .
COPY jev_taxonomy.py .
COPY anti_bot_signatures.py .
COPY classification_anomalies.py .
COPY sitemap_monitor ./sitemap_monitor

CMD ["python", "runner.py", "--all", "--loop", "--interval-seconds", "300"]
