FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY countries/ countries/
COPY scrapers/ scrapers/
COPY sources/ sources/
COPY guide.py http_utils.py iptv.py main.py matcher.py reporter.py ./

RUN adduser --disabled-password --gecos "" --home /app appuser \
    && mkdir -p /app/out /app/.cache \
    && chown -R appuser:appuser /app

# Data volumes (config.yaml is a read-only bind mount, not a volume)
VOLUME ["/app/out", "/app/.cache"]

USER appuser

ENTRYPOINT ["python3", "main.py"]
