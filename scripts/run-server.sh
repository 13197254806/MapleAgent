#!/usr/bin/env sh
set -eu
conda run --no-capture-output -n maple_agent python -m maplebot.server
