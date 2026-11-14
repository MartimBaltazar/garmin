# syntax=docker/dockerfile:1.7
# Multi-stage, non-root, no build tooling in the final image.
#
# Dependencies come from uv.lock with hashes, not from a fresh resolve: the
# image built today and the one built in six months install the same bytes,
# and a tampered package fails the hash check instead of being installed.
# CI builds this on every PR and publishes it to GHCR on tags
# (.github/workflows/ci.yml); Dependabot keeps the base images current.
ARG PYTHON_IMAGE=python:3.12-slim

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
WORKDIR /build
ENV UV_NO_CACHE=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen: fail if uv.lock is out of date with pyproject.toml rather than
# silently re-resolving. --no-emit-project: the project itself is installed
# from the wheel below, with --no-deps.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        --output-file /build/requirements.txt \
    && uv build --wheel --out-dir /dist


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

COPY --from=builder /build/requirements.txt /tmp/requirements.txt
COPY --from=builder /dist/*.whl /tmp/wheels/
RUN pip install --require-hashes -r /tmp/requirements.txt \
    && pip install --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels /tmp/requirements.txt

# Token store and state file live here; mount a PVC. Pre-created with the right
# owner so a non-root process can write to an empty volume.
RUN mkdir -p /var/lib/garmin/tokens && chown -R 10001:10001 /var/lib/garmin

USER 10001:10001
WORKDIR /home/app

ENTRYPOINT ["garmin-exporter"]
CMD ["run"]
