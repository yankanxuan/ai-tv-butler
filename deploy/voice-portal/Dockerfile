# Phase 2 gateway only; it forwards audio to the independent ASR lab and calls
# the existing localhost console after explicit confirmation.
ARG BASE_IMAGE=python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp
WORKDIR /app
COPY poc/requirements-portal.lock /tmp/requirements-portal.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
        -r /tmp/requirements-portal.lock \
    && python -m pip check
COPY poc/voice_portal.py poc/title_session.py poc/asr_lab_server.py /app/poc/
COPY poc/web/voice_portal/ /app/poc/web/voice_portal/
COPY adbtv/__init__.py adbtv/media_index.py /app/adbtv/
RUN groupadd --gid 10001 voiceportal \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp voiceportal \
    && chmod -R a+rX /app
USER 10001:10001
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=4s --retries=3 \
    CMD python -c "import ssl,urllib.request; c=ssl._create_unverified_context(); r=urllib.request.urlopen('https://127.0.0.1:8443/api/health',context=c,timeout=3); assert b'voice-portal' in r.read()"
ENTRYPOINT ["python", "-m", "poc.voice_portal"]
CMD ["--host", "0.0.0.0", "--port", "8443", "--index", "/index/media-index.json", "--cert", "/certs/cert.pem", "--key", "/certs/key.pem"]