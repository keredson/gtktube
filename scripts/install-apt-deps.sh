#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
mapfile -t PACKAGES < "${REPO_DIR}/gtktube/assets/apt-packages.txt"

sudo apt-get update
sudo apt-get install -y "${PACKAGES[@]}"
