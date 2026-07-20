ARG NETBOX_VERSION=v4.6.5-5.0.2
FROM netboxcommunity/netbox:${NETBOX_VERSION}

# Install the Diode NetBox plugin from PyPI (no branching plugin required).
RUN /usr/local/bin/uv pip install --no-cache "netboxlabs-diode-netbox-plugin>=1.14.0,<2"
