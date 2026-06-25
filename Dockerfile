# Multi-stage image for the Croissant Stock Analyser dashboard + in-process
# APScheduler scrapers. Single container by design — the APScheduler jobs
# share memory with the Dash app for the dashboard's "live refresh" calls;
# a worker/web split is a future optimisation, not a v1 requirement.
#
# Targets linux/arm64 (Oracle Cloud Always-Free Ampere VMs) AND linux/amd64
# (so CI smoke + local dev on x86 Mac/PC both work). The C-extension
# packages this app depends on (numpy, scipy, psycopg2-binary, lxml) all
# publish manylinux + musllinux wheels for both architectures.
#
# Stage 1 builds wheels with a fat toolchain so any package that needs to
# compile from source can do so. Stage 2 is a clean runtime image with no
# compiler — keeps the final image around 600 MB instead of 1.5 GB.

# ----- Stage 1: build wheels with full toolchain -----
FROM python:3.11-slim-bookworm AS builder

# build-essential + gcc cover almost every Python C-ext we touch; libpq-dev
# is for psycopg2-binary's source fallback (the wheels usually win but the
# header file unblocks the rare ARM-musl edge case); curl stays in for the
# pip wheel download path on glitchy networks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
# Pre-resolve every wheel once. `--wheel-dir /wheels` collects them in one
# place so the runtime stage can `pip install --no-index` against the local
# directory — no network round-trips during the runtime image build.
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ----- Stage 2: lean runtime -----
FROM python:3.11-slim-bookworm

# curl for the HEALTHCHECK; tini as PID 1 so SIGTERM propagates cleanly to
# the Python process (without tini, `docker compose down` waits the full
# stop_grace_period because Dash's Flask dev server eats signals).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — never run a Python web app as root, especially one that
# talks to the network unsupervised every 30 minutes. UID 1000 lines up
# with the typical first user on the Oracle Ubuntu image, which simplifies
# bind-mount permissions for the /app/data volume.
RUN useradd --create-home --shell /bin/bash --uid 1000 app

WORKDIR /app

# Install Python deps from the pre-built wheels — fully offline, no PyPI
# network during this layer build, so this layer caches as long as
# requirements.txt is unchanged.
COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        -r requirements.txt \
    && rm -rf /wheels

# Application code last — changes most frequently, only invalidates the
# final layer and not the heavy pip-install layer above.
COPY --chown=app:app . .

# Persistent state goes here. Docker compose mounts /app/data from the
# host so `data/sentiment.db` survives container recreation.
RUN mkdir -p /app/data && chown app:app /app/data
VOLUME ["/app/data"]

USER app

# Unbuffered stdout so `docker logs` shows scrape lines in real time
# (Python defaults to block-buffered when stdout is a pipe).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8050

EXPOSE 8050

# A 60s start_period gives the dashboard's clientside callbacks + initial
# tab build time to complete on first boot. Subsequent checks fire every
# 30s; 3 consecutive failures = unhealthy → docker restarts the container.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:8050/_dash-layout \
        > /dev/null || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "main.py", "dashboard", "--port", "8050"]
