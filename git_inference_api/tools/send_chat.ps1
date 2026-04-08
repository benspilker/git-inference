param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PassThruArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$target = Join-Path $PSScriptRoot 'send-chat-v2.ps1'
if (-not (Test-Path $target)) {
    throw "Missing target script: $target"
}

& $target @PassThruArgs
exit $LASTEXITCODE
