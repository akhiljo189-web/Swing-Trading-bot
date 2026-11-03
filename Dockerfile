# fxbot on Railway: one process, standard library only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY fxbot ./fxbot
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "-m", "fxbot", "live"]
