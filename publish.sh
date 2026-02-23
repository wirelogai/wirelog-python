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

Notes:
  - Default upload target: PyPI (https://upload.pypi.org/legacy/)
  - --dry-run builds and validates artifacts, but does not upload.
USAGE
}

mode="${1:-}"
if [[ "$mode" != "" && "$mode" != "--dry-run" && "$mode" != "--testpypi" ]]; then
  usage
  exit 1
fi

cd "$(dirname "$0")"

echo "Building source and wheel distributions..."
python3 -m pip install --upgrade build twine
rm -rf dist
python3 -m build

echo "Validating distributions..."
python3 -m twine check dist/*

if [[ "$mode" == "--dry-run" ]]; then
  echo "Dry run complete. Skipping upload."
  exit 0
fi

if [[ -n "${PYPI_TOKEN:-}" ]]; then
  export TWINE_USERNAME="__token__"
  export TWINE_PASSWORD="$PYPI_TOKEN"
fi

upload_cmd=(python3 -m twine upload)
if [[ "$mode" == "--testpypi" ]]; then
  upload_cmd+=(--repository-url "https://test.pypi.org/legacy/")
fi
upload_cmd+=(dist/*)

echo "Uploading distributions..."
"${upload_cmd[@]}"
