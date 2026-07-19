# Diode plugin config for the local compose stack.
# diode_target points at the Diode nginx service on the shared compose network.
# netbox_to_diode client secret is mounted at /run/secrets/netbox_to_diode.

PLUGINS = [
    "netbox_diode_plugin",
]

PLUGINS_CONFIG = {
    "netbox_diode_plugin": {
        "diode_target_override": "grpc://ingress-nginx:80/diode",
        "diode_username": "diode",
        # Prefer file at /run/secrets/netbox_to_diode (see docker-compose volumes).
        "netbox_to_diode_client_secret": None,
        "secrets_path": "/run/secrets/",
        "netbox_to_diode_client_secret_name": "netbox_to_diode",
    },
}
