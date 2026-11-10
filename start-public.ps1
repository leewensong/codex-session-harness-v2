param([int]$Port = 8055, [string]$BindAddress = "0.0.0.0")

# Public V2 deployment uses 8055 because local 8050 belongs to Strata.
# Keep C:\frp\frpc-win11.toml's V2 localPort in sync if this port changes.
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "start.ps1") -Port $Port -BindAddress $BindAddress
