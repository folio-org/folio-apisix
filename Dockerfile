# syntax=docker/dockerfile:1
ARG APISIX_VERSION=3.17.0-debian

# --- Builder ----------------------------------------------------------------
# The hardened runtime image ships no package manager, so the tools the
# entrypoint needs at runtime (curl, jq, envsubst) are installed here — in the
# matching `-dev` variant, which runs as root and has apt — and then copied,
# with their shared libraries, into the final image.
FROM dhi.io/apisix:${APISIX_VERSION}-dev AS builder
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl jq gettext-base \
    && rm -rf /var/lib/apt/lists/*
COPY docker/collect-tools.sh /usr/local/bin/collect-tools.sh
RUN bash /usr/local/bin/collect-tools.sh curl jq envsubst

# --- Runtime ----------------------------------------------------------------
# The hardened APISIX image: minimal, non-root (UID 65532), no shell package
# manager. It keeps the upstream entrypoint at /docker-entrypoint.sh.
FROM dhi.io/apisix:${APISIX_VERSION}

# Runtime tools (with their libraries) used by entrypoint.sh to configure the
# live gateway through the Admin API.
COPY --from=builder /staging/ /

# FOLIO defaults and startup configuration.
COPY --chown=65532:65532 config/config.yaml /usr/local/apisix/conf/config.yaml
COPY config/cors.json /opt/apisix/folio-config/cors.json
COPY config/resources/ /opt/apisix/folio-config/resources/
COPY --chown=65532:65532 plugins/ /usr/local/apisix/apisix/plugins/
COPY --chmod=0755 entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["docker-start"]
