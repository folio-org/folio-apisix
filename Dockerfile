ARG APISIX_VERSION=3.17.0-debian
FROM docker.io/apache/apisix:${APISIX_VERSION}

USER root

RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y curl jq gettext-base \
    && apt-get clean

COPY --chown=apisix:apisix config/config.yaml /usr/local/apisix/conf/config.yaml
COPY --chown=apisix:apisix config/cors.json /opt/apisix/folio-config/cors.json
COPY --chown=apisix:apisix config/resources/ /opt/apisix/folio-config/resources/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER apisix

ENTRYPOINT ["/entrypoint.sh"]
CMD ["docker-start"]
