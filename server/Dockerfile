FROM python:3.13-slim

ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="Stintara" \
      org.opencontainers.image.description="Simulator-independent live timing and race intelligence server" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$REVISION" \
      org.opencontainers.image.source="https://github.com/kalanur/stintara"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STINTARA_BUILD_VERSION=$VERSION \
    STINTARA_BUILD_REVISION=$REVISION

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY db ./db
COPY static ./static
RUN mkdir -p /data

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--ws-max-size", "2097152"]
