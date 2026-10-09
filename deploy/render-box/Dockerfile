# Project Omnisearch — Render free-tier discovery box.
#
# One free instance hosts BOTH search (SearXNG) and scrape (crw serve)
# behind a single public port ($PORT), because Render's Hobby plan allots
# only 750 free instance-hours per workspace per month — two always-on
# services would blow the budget. Free instances also spin down after
# ~15 min idle; the first request then pays a one-time cold start.
#
# Build context is the REPO ROOT:
#   docker build -f deploy/render-box/Dockerfile -t omnisearch-box .
FROM searxng/searxng:latest

USER root

ARG CRW_VERSION=v0.37.2
# github (default, used by Render): download the official fastCRW release.
# local: use deploy/render-box/crw or crw-linux-x64.tar.gz from the build
# context (offline/local test builds — those files are gitignored).
ARG CRW_SOURCE=github

COPY deploy/render-box/ /tmp/ctx/
RUN set -e; \
    mkdir -p /opt/crw /tmp/crw-out; \
    if [ "$CRW_SOURCE" = "local" ]; then \
      if [ -f /tmp/ctx/crw-linux-x64.tar.gz ]; then \
        tar -xzf /tmp/ctx/crw-linux-x64.tar.gz -C /tmp/crw-out; \
      else \
        cp /tmp/ctx/crw /tmp/crw-out/crw; \
      fi; \
    else \
      wget -qO /tmp/crw.tar.gz \
        "https://github.com/fastcrw/crw/releases/download/${CRW_VERSION}/crw-linux-x64.tar.gz"; \
      tar -xzf /tmp/crw.tar.gz -C /tmp/crw-out; \
      rm -f /tmp/crw.tar.gz; \
    fi; \
    install -m 0755 "$(find /tmp/crw-out -type f -name crw | head -1)" /opt/crw/crw; \
    rm -rf /tmp/crw-out /tmp/ctx; \
    /opt/crw/crw --version

# SearXNG: JSON API on for the app's client; no redis (limiter off).
COPY deploy/render-box/settings.yml /etc/searxng/settings.yml
RUN chown searxng:searxng /etc/searxng/settings.yml

COPY deploy/render-box/box_proxy.py deploy/render-box/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/box_proxy.py /usr/local/bin/entrypoint.sh

# granian binds loopback only — the public surface is box_proxy on $PORT.
ENV CRW_HOST=0.0.0.0 \
    CRW_PORT=3001 \
    GRANIAN_HOST=127.0.0.1

EXPOSE 8080 3001

# Render uses its own dashboard health path (/health); this is for docker run.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
  CMD wget -qO- "http://127.0.0.1:${PORT:-10000}/health" >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
