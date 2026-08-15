$ErrorActionPreference = "Stop"
$ConfigPath = if (Test-Path "config.local.yaml") { "config.local.yaml" } else { "config.yaml" }
conda run --no-capture-output -n maple_agent python -m maplebot.client --config $ConfigPath
