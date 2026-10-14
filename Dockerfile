# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- runtime image
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models
WORKDIR /app
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /app app

# Dependencies first (better layer caching), then the application itself.
COPY pyproject.toml README.md ./
RUN mkdir -p src/voice_agent && touch src/voice_agent/__init__.py \
    && pip install ".[stt]" && pip uninstall -y voice-monday-agent && rm -rf src
COPY src ./src
RUN pip install --no-deps . \
    && mkdir -p /app/data /models /sounds \
    && chown -R app:app /app/data /models /sounds

USER app
EXPOSE 8000
ENTRYPOINT ["voice-agent"]
CMD ["api"]

# ---------------------------------------------------------------- dev/test image
FROM base AS dev
USER root
RUN pip install "pytest>=8.2" "pytest-asyncio>=0.24" "fakeredis>=2.23" "ruff>=0.6"
# The repository is bind-mounted at /app; prefer the mounted sources.
ENV PYTHONPATH=/app/src
ENTRYPOINT []
CMD ["pytest", "-q"]
