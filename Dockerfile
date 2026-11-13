FROM python:3.11-slim-bookworm

ARG BUTLER_UID=10001
ARG BUTLER_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends adb ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${BUTLER_GID} butler \
    && useradd --uid ${BUTLER_UID} --gid ${BUTLER_GID} --create-home butler
WORKDIR /app
COPY requirements.txt requirements-runtime.lock requirements-build.lock pyproject.toml README.md THIRD_PARTY_NOTICES.md ./
COPY adbtv ./adbtv
COPY scripts/healthcheck.py /app/healthcheck.py
# 源码在宿主机上是 600/700，COPY 进镜像后归 root，非 root 用户读不到
RUN chmod -R a+rX /app
# Identical pinned/hash-verified runtime to requirements.txt. No resolver drift
# or Rust/source builds on x86_64/ARM64. Build tools are pinned separately.
ARG INSTALL_PIPER=0
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-runtime.lock \
    && python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-build.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation ".[schemas]" \
    && if [ "$INSTALL_PIPER" = "1" ]; then python -m pip install --no-cache-dir piper-tts==1.2.0; fi \
    && python -m pip check \
    && mkdir -p /data/.android \
    && chown -R butler:butler /data
USER ${BUTLER_UID}:${BUTLER_GID}
ENV AI_TV_CONFIG=/data/config.yaml AI_TV_DATA_DIR=/data HOME=/data PYTHONDONTWRITEBYTECODE=1
EXPOSE 8770
VOLUME ["/data"]
ENTRYPOINT ["python3", "-m", "adbtv.cli"]
CMD ["serve"]
