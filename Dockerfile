FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/cache \
    HF_HOME=/cache/huggingface

WORKDIR /app

RUN sed -i 's/Suites: bookworm bookworm-updates/Suites: bookworm/' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --timeout 120 --retries 5 -r requirements.txt

COPY video_summary.py .
COPY prompts ./prompts

ENTRYPOINT ["python", "/app/video_summary.py"]
