FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        gir1.2-ayatanaappindicator3-0.1 \
        libcairo2-dev \
        libgirepository1.0-dev \
        libgtk-3-dev \
        pkg-config \
        python3 \
        python3-dev \
        python3-tk \
        python3-venv \
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

