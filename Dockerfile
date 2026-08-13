FROM node:22-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS frontend

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
ARG PUBLIC_URL=https://driftpatch.guillermozubikarai.dev
ENV VITE_PUBLIC_URL=${PUBLIC_URL}
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.8.13@sha256:4de5495181a281bc744845b9579acf7b221d6791f99bcc211b9ec13f417c2853 AS uv

FROM python:3.11-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS python

WORKDIR /code
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5 AS runtime

WORKDIR /code
COPY --from=python --chown=65532:65532 /code/.venv /code/.venv
COPY app/ ./app/
COPY benchmark/ ./benchmark/
COPY --from=frontend /build/frontend/dist ./frontend/dist

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION} \
    PORT=8080 \
    PYTHONPATH=/code/.venv/lib/python3.11/site-packages:/code \
    PYTHONUNBUFFERED=1

USER 65532:65532
EXPOSE 8080

CMD ["-m", "uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
