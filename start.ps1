param([int]$Port = 8050, [string]$BindAddress = "0.0.0.0", [switch]$LegacyCodex)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
if ($LegacyCodex) {
    uv run --project $ProjectDir python -m app.gateway.server --legacy-codex --host $BindAddress --port $Port
} else {
    uv run --extra unified --project $ProjectDir python -m app.gateway.server --host $BindAddress --port $Port
}
