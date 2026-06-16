# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile for the Multi-Agent A2A mesh.
#
# One image, many containers. Compose picks the command per service so the
# same artifact powers the six agents, the register sidecar, and the bridge
# (which also serves the built React UI).
#
# Build via compose (preferred):  docker compose build
# =============================================================================

# ----- Stage 1: frontend build ----------------------------------------------
# Build the React/Vite UI into static assets. Only the frontend/ dir busts
# this layer, so Python changes don't trigger an npm rebuild.
FROM node:20-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build          # emits /fe/dist


# ----- Stage 2: python builder ----------------------------------------------
# Install Python deps into a self-contained prefix so the runtime image stays
# slim and free of build toolchains.
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# ----- Stage 3: runtime ------------------------------------------------------
# Lean image, non-root user, curl for healthchecks.
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

# Python packages into /usr/local (on PATH + default sys.path).
COPY --from=builder /install/ /usr/local/

WORKDIR /app
COPY --chown=appuser:appuser . /app

# The built UI — served by the bridge via StaticFiles at runtime.
COPY --from=frontend --chown=appuser:appuser /fe/dist /app/frontend/dist

USER appuser

# Agent ports + the bridge/UI port.
EXPOSE 9101 9102 9103 9104 9105 9106 8080

ENTRYPOINT ["/usr/bin/tini", "--"]

# No CMD — compose sets `command:` per service.
