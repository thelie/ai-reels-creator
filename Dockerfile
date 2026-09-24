FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY reels ./reels
COPY assets ./assets
ARG EXTRAS=""
RUN pip install --no-cache-dir ".${EXTRAS}"

ENV REELS_DATA_DIR=/data
VOLUME ["/data"]
CMD ["reels-bot"]
