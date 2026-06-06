FROM netboxcommunity/netbox:v4.4-3.4.1

USER root

COPY plugin_requirements.txt /opt/netbox/plugin_requirements.txt

RUN HTTPS_PROXY=http://wsg-proxy.oecd.org:443 \
    HTTP_PROXY=http://wsg-proxy.oecd.org:443 \
    /usr/local/bin/uv pip install \
    --allow-insecure-host pypi.org \
    --allow-insecure-host files.pythonhosted.org \
    --allow-insecure-host pypi.python.org \
    -r /opt/netbox/plugin_requirements.txt

RUN mkdir -p /opt/netbox/netbox/static/netbox_topology_views/img && \
    chown -R unit:root /opt/netbox/netbox/static/netbox_topology_views

USER unit

WORKDIR /opt/netbox/netbox
