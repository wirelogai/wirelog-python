#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./publish.sh
  ./publish.sh --dry-run
  ./publish.sh --testpypi

Environment:
  PYPI_TOKEN     Optional. If set, used as a PyPI API token for upload.
                 Otherwise twine reads ~/.pypirc.

Notes:
  - Default upload target: PyPI (https://upload.pypi.org/legacy/)
  - --dry-run builds and validates artifacts, but does not upload.
  - Uses uv for build + twine so this works on macOS Homebrew Python
    (where `python3 -m pip install` fails with externally-managed-environment).
USAGE
}

mode="${1:-}"
if [[ "$mode" != "" && "$mode" != "--dry-run" && "$mode" != "--testpypi" ]]; then
  usage
  exit 1
fi

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required (install: https://docs.astral.sh/uv/getting-started/installation/)" >&2
  exit 1
fi

echo "Building source and wheel distributions..."
rm -rf dist
uv build

echo "Validating distributions..."
uv tool run twine check dist/*

if [[ "$mode" == "--dry-run" ]]; then
  echo "Dry run complete. Skipping upload."
  exit 0
fi

if [[ -n "${PYPI_TOKEN:-}" ]]; then
  export TWINE_USERNAME="__token__"
  export TWINE_PASSWORD="$PYPI_TOKEN"
fi

upload_cmd=(uv tool run twine upload)
if [[ "$mode" == "--testpypi" ]]; then
  upload_cmd+=(--repository-url "https://test.pypi.org/legacy/")
fi
upload_cmd+=(dist/*)

echo "Uploading distributions..."
"${upload_cmd[@]}"
