<#
.SYNOPSIS
Run the loopback-only chat server in the foreground using the project's Python environment.
.PARAMETER Port
Local port, default 8000. Pick another port if an existing process already uses it.
.EXAMPLE
.\scripts\start-chat.ps1 -Port 8010
#>
[CmdletBinding()]
param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
# Resolve relative paths from this script, so virtual-environment activation is unnecessary.
Set-Location (Split-Path $PSScriptRoot)
# demo.chat enforces the loopback binding. Keep this terminal open; Ctrl+C stops the server.
& .\.venv\Scripts\python -m demo.chat --port $Port
# Preserve the server's exit status for callers rather than reporting a failed launch as success.
exit $LASTEXITCODE
