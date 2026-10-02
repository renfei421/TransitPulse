#!/bin/sh
# Release source is a SHA-256 verified archive, not executable ConfigMap data.
set -eu
python -m pip install --disable-pip-version-check --no-cache-dir --no-build-isolation \
  --require-hashes --no-compile --target /work/vendor \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  -r /work/release/requirements-model.txt
python -m backend.data_process.prepare_model
