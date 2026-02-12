# ================================
# File: Dockerfile
# ================================
# Multi-stage, aarch64-friendly image for Raspberry Pi OS Lite
# Stage 1: build Python wheels in a clean env
ARG PYTHON_IMAGE=python:3.11.9-slim
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential python3-dev libevdev-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

# Build a virtualenv with all deps
RUN python -m venv /opt/venv && \
    . /opt/venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# Stage 2: slim runtime with only shared libs needed for evdev/DBus/BLE client
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libdbus-1-3 libbluetooth3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring the prebuilt virtualenv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy your app
COPY pihub ./pihub

# If you keep version metadata or default configs, copy them here too
# COPY VERSION .
# COPY config ./config

# Entrypoint
CMD ["/opt/venv/bin/python", "-m", "pihub.app"]
