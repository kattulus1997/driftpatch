FROM node:22-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS frontend

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
ARG PUBLIC_URL=https://driftpatch.guillermozubikarai.dev
ENV VITE_PUBLIC_URL=${PUBLIC_URL}
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.8.13@sha256:4de5495181a281bc744845b9579acf7b221d6791f99bcc211b9ec13f417c2853 AS uv

FROM python:3.12-alpine3.22@sha256:a190708a2dec1bd18b1decb539f8e8f5407abaa9bf39cacda583f7f8c11db322 AS python

WORKDIR /code
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-alpine3.22@sha256:a190708a2dec1bd18b1decb539f8e8f5407abaa9bf39cacda583f7f8c11db322 AS runtime

USER root
RUN apk upgrade --no-cache && python -m pip uninstall --yes pip
WORKDIR /code
COPY --from=python --chown=65532:65532 /code/.venv /code/.venv
COPY app/ ./app/
COPY benchmark/ ./benchmark/
COPY --from=frontend /build/frontend/dist ./frontend/dist

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION} \
    PORT=8080 \
    PYTHONPATH=/code/.venv/lib/python3.12/site-packages:/code \
    PYTHONUNBUFFERED=1

USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
