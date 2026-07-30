# Multi-arch (works on Raspberry Pi ARM64 and x86_64)
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a non-root user (uid 1000 = the default user on Raspberry Pi OS,
# so files written to the bind-mounted data dir belong to "pi").
# The host data dir must be writable by uid 1000: chown -R 1000:1000 <dir>
RUN useradd --uid 1000 --create-home memehog
USER memehog

ENV DATA_DIR=/data \
    HOME=/home/memehog
VOLUME /data
EXPOSE 2137

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"2137\")}/api/v1/health')" || exit 1

# Baked in by CI so the UI can show which commit is running ("dev" locally).
ARG GIT_SHA=dev
ARG BUILD_DATE=
ENV MEMEHOG_BUILD_SHA=$GIT_SHA \
    MEMEHOG_BUILD_DATE=$BUILD_DATE

CMD ["python", "-m", "memehog"]
