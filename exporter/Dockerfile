# syntax=docker/dockerfile:1.7
# Multi-stage, non-root, no build tooling in the final image.
# The base tag is pinned by digest in CI (see .github/workflows); keep the
# human-readable tag here so local builds stay reproducible enough to debug.
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip build \
    && python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

COPY --from=builder /dist/*.whl /tmp/wheels/
RUN pip install /tmp/wheels/*.whl && rm -rf /tmp/wheels

# Token store and state file live here; mount a PVC. Pre-created with the right
# owner so a non-root process can write to an empty volume.
RUN mkdir -p /var/lib/garmin/tokens && chown -R 10001:10001 /var/lib/garmin

USER 10001:10001
WORKDIR /home/app

ENTRYPOINT ["garmin-exporter"]
CMD ["run"]
