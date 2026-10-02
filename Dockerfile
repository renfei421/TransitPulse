# Base manifest is pinned; update deliberately with the dependency lock.
FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY backend backend
COPY database database
COPY scripts scripts
RUN useradd --uid 10001 --create-home worker && mkdir -p /app/data /app/artifacts && chown -R worker:worker /app
USER worker
CMD ["python", "-m", "backend.api.app"]

FROM base AS model
USER root
COPY requirements-model.txt .
RUN pip install --no-cache-dir --require-hashes --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-model.txt
ENV HF_HOME=/opt/model-cache TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
RUN python -c "from backend.data_process.sentiment import SentimentModel; SentimentModel()" && chmod -R a+rX /opt/model-cache
ENV HF_HUB_OFFLINE=1
USER worker
CMD ["python", "-m", "backend.ingestion.queue", "worker"]
