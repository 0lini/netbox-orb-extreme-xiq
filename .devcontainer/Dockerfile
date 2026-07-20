# Workspace image for the compose Dev Container.
FROM docker.io/python:3.14-bookworm

RUN apt-get update \
  && apt-get install -y --no-install-recommends git curl jq \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir uv

WORKDIR /workspaces/netbox-orb-extreme-platformone
