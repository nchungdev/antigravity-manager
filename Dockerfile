FROM python:3.11-slim

LABEL maintainer="nchungdev"
LABEL description="Antigravity (AGY) Web Manager for Linux & NAS (OMV 7 / Unraid / TrueNAS / Docker)"

WORKDIR /app

# Install curl for container health check & procps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY server.py /app/server.py

# Configuration environment variables
ENV PORT=8585
ENV HOST_USER=root
ENV USER_HOME=/root
ENV SYSTEMD_SERVICE=antigravity-cli-daemon.service
ENV TZ=Asia/Ho_Chi_Minh

EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8585/api/status || exit 1

CMD ["python3", "-u", "/app/server.py"]
