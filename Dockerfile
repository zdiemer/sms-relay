# sms-relay — self-hosted SMS gateway. Single stage: the UI is one static HTML
# file served by FastAPI, so there is no frontend build to do.

FROM python:3.14-slim AS runtime
WORKDIR /app

# Install from uv.lock so every image build resolves the same dependency set
# instead of whatever PyPI serves that day.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
COPY app/ /app/app/
RUN uv export --directory /app/app --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps /app/app \
 && rm /tmp/requirements.txt

ENV SMS_RELAY_DB_PATH=/data/sms-relay.db \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

EXPOSE 8000
USER 1000:1000
CMD ["uvicorn", "sms_relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
