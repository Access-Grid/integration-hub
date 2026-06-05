#!/usr/bin/env bash
# Build a standalone macOS agsync binary with PyInstaller — mirrors the
# build-macos job in .github/workflows/release.yml so you can test locally.
#
# Usage: ./scripts/build_macos.sh
# Output: dist/agsync  (and dist/agsync-macos-<arch>.zip)
set -euo pipefail

cd "$(dirname "$0")/.."

pyinstaller \
  --noconfirm \
  --onefile \
  --name agsync \
  --collect-all agsync \
  --collect-all accessgrid \
  --hidden-import agsync.lib.pacs.avigilon \
  --hidden-import agsync.lib.pacs.lenel \
  --add-data "src/agsync/templates:agsync/templates" \
  --add-data "src/agsync/static:agsync/static" \
  --add-data "src/agsync/locales:agsync/locales" \
  scripts/pyinstaller_entry.py

./dist/agsync --version

arch=$(uname -m)
( cd dist && zip -9 "agsync-macos-$arch.zip" agsync )
echo "Built dist/agsync and dist/agsync-macos-$arch.zip"
