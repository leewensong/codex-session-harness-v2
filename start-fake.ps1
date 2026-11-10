param([int]$Port = 8050, [string]$BindAddress = "127.0.0.1")

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
uv run --project $ProjectDir python -m app.gateway.server --fake --host $BindAddress --port $Port
