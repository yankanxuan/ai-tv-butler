# The caller must supply a reviewed immutable python:3.11-slim@sha256:... reference.
ARG BASE_IMAGE=python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2
WORKDIR /app
COPY poc/requirements-v41.lock /tmp/requirements-v41.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
        -r /tmp/requirements-v41.lock \
    && python -m pip check \
    && groupadd --gid 10001 asrlab \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp asrlab
COPY poc/*.py /app/poc/
COPY poc/models-v41.lock.json /app/poc/models-v41.lock.json
COPY poc/licenses/v41/ /app/poc/licenses/v41/
# Read-only library metadata parser for optional title suggestions; no TV service.
COPY adbtv/__init__.py adbtv/media_index.py /app/adbtv/
# Workspace sources may be 0600. Expose only image code to any non-root UID;
# host model/audio/index permissions are never changed.
RUN chmod -R a+rX /app
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=3s --start-period=40s --retries=3 \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2)); assert r.get('service')=='asr-lab' and r.get('ready') is True"
ENTRYPOINT ["python", "-m", "poc.asr_lab_server"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--manifest", "/models/model.json", "--audio-root", "/audio", "--num-threads", "2"]
