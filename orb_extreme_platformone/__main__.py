"""Standalone dry run: extract from Platform ONE, transform, print entities (no Diode push).

Run as `python -m orb_extreme_platformone`. Configuration comes from the
environment (plus a local `.env` file) instead of an Orb policy; nothing is
pushed to Diode.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timezone

from google.protobuf.json_format import MessageToDict
from worker.models import Config, Policy

from .backend import DEFAULT_CLASSIFICATION, Backend


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_env_file(path: str = ".env") -> None:
    """Read KEY=VALUE lines into os.environ; exported variables take precedence."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _standalone_config() -> dict:
    return {
        "package": "orb_extreme_platformone",
        "BOOTSTRAP": _env_bool("BOOTSTRAP", False),
        "NETBOX_API_URL": os.environ.get("NETBOX_API_URL"),
        "NETBOX_API_TOKEN": os.environ.get("NETBOX_API_TOKEN"),
        "PLATFORMONE_API_TOKEN": os.environ.get("PLATFORMONE_API_TOKEN"),
        "PLATFORMONE_USERNAME": os.environ.get("PLATFORMONE_USERNAME"),
        "PLATFORMONE_PASSWORD": os.environ.get("PLATFORMONE_PASSWORD"),
        "classification": os.environ.get("PLATFORMONE_CLASSIFICATION", DEFAULT_CLASSIFICATION),
        "name_source": os.environ.get("PLATFORMONE_NAME_SOURCE", "hostname"),
    }


def _quote_values(value):
    """Render every scalar as a string so the JSON dry-run output quotes all values."""
    if isinstance(value, dict):
        return {key: _quote_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_quote_values(item) for item in value]
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _load_env_file()
    policy = Policy(config=Config(**_standalone_config()), scope={"sites": ["*"]})
    backend = Backend()
    for entity in backend.run("standalone", policy):
        data = MessageToDict(entity, preserving_proto_field_name=True)
        ts = entity.timestamp.ToDatetime(tzinfo=timezone.utc).astimezone()
        data["timestamp"] = ts.isoformat(timespec="seconds")
        print(json.dumps(_quote_values(data), indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()
