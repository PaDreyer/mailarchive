FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        python3 \
        libpython3.10 \
        python3-tk \
        python3-venv \
        xauth \
        x11-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --retry 3 \
        --output /tmp/appimagetool.AppImage \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage \
    && chmod +x /tmp/appimagetool.AppImage \
    && cd /opt \
    && /tmp/appimagetool.AppImage --appimage-extract \
    && mv /opt/squashfs-root /opt/appimagetool \
    && ln -s /opt/appimagetool/AppRun /usr/local/bin/appimagetool \
    && rm /tmp/appimagetool.AppImage

WORKDIR /workspace
ENV APPIMAGETOOL=/usr/local/bin/appimagetool

CMD ["./scripts/build-linux.sh"]
