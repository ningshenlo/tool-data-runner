FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runner.py .
COPY report_exports.py .
COPY market_snapshot_refresh.py .
COPY search_demand_refresh.py .
COPY search_demand_brand.py .
COPY scripts/check-search-demand-publication.py ./scripts/
COPY scripts/check-market-publication.py ./scripts/
COPY report_review_api.py .
COPY report-markets.json .
COPY d1_costguard.py .
COPY build_revision.py .
COPY taxonomy_shadow.py .
COPY taxonomy_batch.py .
COPY anti_bot_signatures.py .
COPY classification_anomalies.py .
COPY pricing ./pricing
COPY sitemap_monitor ./sitemap_monitor
RUN python build_revision.py > BUILD_REVISION

CMD ["python", "runner.py", "--all", "--loop", "--interval-seconds", "300"]
