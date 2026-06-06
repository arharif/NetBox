FROM netboxcommunity/netbox:v4.4-3.4.1

USER root

COPY plugin_requirements.txt /opt/netbox/plugin_requirements.txt

RUN /usr/local/bin/uv pip install -r /opt/netbox/plugin_requirements.txt

RUN mkdir -p /opt/netbox/netbox/static/netbox_topology_views/img && \
    chown -R unit:root /opt/netbox/netbox/static/netbox_topology_views

RUN SECRET_KEY="dummyKeyWithMinimumLength-----------------------------" \
    /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input

USER unit
