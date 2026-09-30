# Multi-stage, aarch64-friendly image for Raspberry Pi OS Lite.
# Minor version pinned, patch not, so each build picks up Python and Debian security fixes.
ARG PYTHON_IMAGE=python:3.14-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# evdev builds against the kernel headers only (no libevdev), so a C compiler is all it needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

RUN python -m venv /opt/venv && \
    . /opt/venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt

FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pihub ./pihub

# Logged at startup, to tell which build is running. After the code COPY so a
# code change always stamps a new date.
RUN date -u +"%Y-%m-%d %H:%M UTC" > /app/BUILD_DATE

CMD ["/opt/venv/bin/python", "-m", "pihub.app"]