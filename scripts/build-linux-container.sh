#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image_name="mailarchive-linux-builder:ubuntu-22.04"

docker build \
  --file "$project_root/packaging/linux/Dockerfile" \
  --tag "$image_name" \
  "$project_root"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$project_root:/workspace" \
  "$image_name"

