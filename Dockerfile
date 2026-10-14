# PlayX run server. The ROM is NOT baked in: mount it at /rom/game.gba (read-only).
ARG BUILD_IMAGE=debian:trixie-slim
ARG RUN_IMAGE=python:3.12-slim-trixie
FROM ${BUILD_IMAGE} AS core
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates cmake make gcc g++ zlib1g-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 0.10.5 https://github.com/mgba-emu/mgba.git /src/mgba
WORKDIR /src/mgba/build
RUN cmake .. -DBUILD_LIBRETRO=ON -DBUILD_QT=OFF -DBUILD_SDL=OFF -DBUILD_SHARED=OFF -DBUILD_STATIC=OFF \
      -DUSE_FFMPEG=OFF -DUSE_ZLIB=ON -DUSE_PNG=OFF -DUSE_LIBZIP=OFF -DUSE_SQLITE3=OFF -DUSE_ELF=OFF \
      -DUSE_LUA=OFF -DUSE_EDITLINE=OFF -DUSE_DISCORD_RPC=OFF -DENABLE_SCRIPTING=OFF \
      -DCMAKE_BUILD_TYPE=Release \
    && make -j"$(nproc)" mgba_libretro

FROM ${RUN_IMAGE}
RUN pip install --no-cache-dir numpy==2.1.3 aiohttp==3.10.11 pillow==11.0.0 \
    && useradd --create-home --uid 99 --gid 100 playx || useradd --create-home playx
WORKDIR /app
COPY --from=core /src/mgba/build/mgba_libretro.so /app/core/mgba_libretro.so
COPY server/ /app/server/
COPY web/ /app/web/
ENV PORT=8787 PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=4)"
USER 99:100
CMD ["python", "server/run.py", "--rom", "/rom/game.gba", "--core", "/app/core/mgba_libretro.so", "--data", "/data"]
