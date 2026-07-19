# Lean workspace image (avoid the Microsoft yarn apt source breakage).
FROM docker.io/python:3.12-bookworm

RUN apt-get update \
  && apt-get install -y --no-install-recommends jq curl git \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /workspaces/netbox-orb-extreme-platformone
