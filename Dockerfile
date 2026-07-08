ARG APISIX_VERSION=3.17.0-debian
FROM docker.io/apache/apisix:${APISIX_VERSION}

USER root

ARG TARGETARCH
ARG ADC_VERSION=0.28.0
ARG ADC_ARTIFACT="adc_${ADC_VERSION}_linux_${TARGETARCH}.tar.gz"
ARG ADC_DOWNLOAD_URL="https://github.com/api7/adc/releases/download/v${ADC_VERSION}/${ADC_ARTIFACT}"

RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y curl jq gettext-base \
    && apt-get clean

RUN curl -sL "${ADC_DOWNLOAD_URL}" -o /tmp/adc.tar.gz \
    && tar -xzf /tmp/adc.tar.gz -C /usr/local/bin ./adc \
    && rm -f /tmp/adc.tar.gz \
    && chmod +x /usr/local/bin/adc

COPY --chown=apisix:apisix config/config.yaml /usr/local/apisix/conf/config.yaml
COPY --chown=apisix:apisix config/cors.yaml /opt/apisix/folio-config/cors.yaml
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER apisix

ENTRYPOINT ["/entrypoint.sh"]
CMD ["docker-start"]
