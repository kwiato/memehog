# Multi-arch (works on Raspberry Pi ARM64 and x86_64)
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 2137

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"2137\")}/api/v1/health')" || exit 1

CMD ["python", "-m", "memehog"]
