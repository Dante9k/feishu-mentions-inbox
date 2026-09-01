ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app
COPY pyproject.toml ./
COPY README.md LICENSE ./
COPY app ./app
RUN python -m pip install --upgrade "pip>=26.2" && \
    python -m pip install . && \
    python -m pip uninstall --yes pip setuptools wheel

RUN groupadd --system mentions && \
    useradd --system --gid mentions --no-create-home --home-dir /nonexistent mentions && \
    mkdir /data && chown mentions:mentions /data
USER mentions

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.getenv('APP_PORT','8090'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3)"

CMD ["python", "-m", "app"]
