FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MUSE2API_HOME=/app MUSE2API_PROFILE_ROOT=/app/data/profiles \
    MUSE2API_CHROMIUM=/usr/bin/chromium MUSE2API_CDP_PORT=19210 HOME=/tmp
WORKDIR /app
# OS packages come from Debian's signed repositories. Rebuild intentionally to
# pick up Chromium/security updates; the OS package set is not a frozen snapshot.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium fonts-wqy-zenhei ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 muse && useradd --uid 10001 --gid muse --no-create-home muse
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY app.py cdp.py config.py engine.py store.py scheduler.py security.py ./
COPY web/ ./web/
COPY tools/get_muse_cookie.py ./tools/get_muse_cookie.py
RUN mkdir -p /app/data && chown 10001:10001 /app/data
VOLUME ["/app/data"]
USER 10001:10001
EXPOSE 18610
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18610/healthz',timeout=2)"
CMD ["sh", "-c", "umask 077; exec python -m uvicorn app:app --host 0.0.0.0 --port 18610 --workers 1 --no-access-log --no-proxy-headers"]
