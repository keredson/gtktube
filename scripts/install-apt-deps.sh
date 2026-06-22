#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
mapfile -t PACKAGES < <(
  sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "${REPO_DIR}/gtktube/assets/apt-packages.txt"
)

sudo apt-get update

INSTALLABLE=()
UNAVAILABLE=()
for package in "${PACKAGES[@]}"; do
  if apt-cache show "$package" >/dev/null 2>&1; then
    INSTALLABLE+=("$package")
  else
    UNAVAILABLE+=("$package")
  fi
done

if ((${#UNAVAILABLE[@]})); then
  cat >&2 <<EOF
Some required GTKTube packages are not available from your configured apt repositories:

$(printf '  %s\n' "${UNAVAILABLE[@]}")

Ubuntu releases do not always package the Clapper library and GTK bindings.
Add a repository that provides these packages or use a distribution release that
includes them, then run this installer again.
EOF
fi

if ((${#INSTALLABLE[@]} == 0)); then
  exit 1
fi

sudo apt-get install -y "${INSTALLABLE[@]}"

if ((${#UNAVAILABLE[@]})); then
  exit 1
fi
