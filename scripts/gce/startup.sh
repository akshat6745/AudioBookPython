#!/usr/bin/env bash
# GCE startup script — runs as root on every boot, so every step is idempotent.
set -euo pipefail

# 1 GB swap: e2-micro has 1 GB RAM, and the Docker build (pip install) spikes.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show | grep -q /swapfile || swapon /swapfile

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades
fi
