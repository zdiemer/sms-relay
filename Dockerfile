# sms-relay — self-hosted SMS gateway. Single stage: the UI is one static HTML
# file served by FastAPI, so there is no frontend build to do.

FROM python:3.12-slim AS runtime
WORKDIR /app

COPY app/ /app/app/
RUN pip install --no-cache-dir /app/app

ENV SMS_RELAY_DB_PATH=/data/sms-relay.db \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

EXPOSE 8000
USER 1000:1000
CMD ["uvicorn", "sms_relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
