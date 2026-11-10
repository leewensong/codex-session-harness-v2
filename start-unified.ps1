param([int]$Port = 8050, [string]$BindAddress = "0.0.0.0")

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
uv run --extra unified --project $ProjectDir python -m app.gateway.server `
  --adaptor-config config\adaptors.json `
  --agent-type codex `
  --host $BindAddress --port $Port
