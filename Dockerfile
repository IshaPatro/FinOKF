FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FINOKF_DATA_ROOT=/data

WORKDIR /app

RUN python -m pip install --no-cache-dir "pypdf>=5,<7"

COPY README.md LICENSE implementation.md QUESTIONS.md ./
COPY scripts ./scripts
COPY ui ./ui

EXPOSE 8080

CMD ["sh", "-c", "python scripts/serve_vault.py --processed-dir ${FINOKF_DATA_ROOT}/processed --index ${FINOKF_DATA_ROOT}/vault-index.json --vaults-dir ${FINOKF_DATA_ROOT}/vaults/answers --host 0.0.0.0 --port ${PORT:-8080} --response-cache-size ${FINOKF_RESPONSE_CACHE_SIZE:-128}"]
