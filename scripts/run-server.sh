#!/usr/bin/env sh
set -eu
MAPLE_CONFIG_PATH=config.local.yaml
if [ ! -f "$MAPLE_CONFIG_PATH" ]; then
  MAPLE_CONFIG_PATH=config.yaml
fi
conda run --no-capture-output -n maple_agent python -m maplebot.server --config "$MAPLE_CONFIG_PATH"
