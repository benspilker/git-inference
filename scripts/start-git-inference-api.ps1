param(
    [string]$Distro = "Ubuntu",
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8000,
    [int]$ProxyPort = 18000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$apiDirWindows = Join-Path $repoRoot "git_inference_api"

if (-not (Test-Path $apiDirWindows)) {
    throw "API directory not found: $apiDirWindows"
}

$wslExe = "$env:WINDIR\System32\wsl.exe"
if (-not (Test-Path $wslExe)) {
    throw "wsl.exe not found at $wslExe"
}

$apiDirWsl = $apiDirWindows -replace "\\", "/"
if ($apiDirWsl -match "^([A-Za-z]):/(.*)$") {
    $drive = $matches[1].ToLowerInvariant()
    $rest = $matches[2]
    $apiDirWsl = "/mnt/$drive/$rest"
} else {
    throw "Could not convert Windows path to WSL path: $apiDirWindows"
}

$bashScriptTemplate = @'
set -euo pipefail

cd '__API_DIR__'

if [ ! -x ".venv/bin/python3" ]; then
  echo "ERROR: Missing Python venv at __API_DIR__/.venv" >&2
  exit 1
fi

REPO_PATH_CANDIDATE=""
DB_PATH_CANDIDATE=""
if [ -d "/tmp/git_inference_github/api-workrepo/.git" ]; then
  REPO_PATH_CANDIDATE="/tmp/git_inference_github/api-workrepo"
  DB_PATH_CANDIDATE="/tmp/git_inference_github/jobs.db"
elif [ -d "$HOME/git-inference-api-workrepo/.git" ]; then
  REPO_PATH_CANDIDATE="$HOME/git-inference-api-workrepo"
  DB_PATH_CANDIDATE="$HOME/git-inference-api-workrepo/jobs.db"
else
  echo "ERROR: Missing workrepo. Checked /tmp/git_inference_github/api-workrepo and $HOME/git-inference-api-workrepo" >&2
  exit 1
fi

pids=$(pgrep -f 'uvicorn app.main:app' || true)
if [ -n "$pids" ]; then
  kill $pids || true
  sleep 2
fi

left=$(pgrep -f 'uvicorn app.main:app' || true)
if [ -n "$left" ]; then
  kill -9 $left || true
  sleep 1
fi

export REPO_PATH="$REPO_PATH_CANDIDATE"
export DB_PATH="$DB_PATH_CANDIDATE"
export REPO_BRANCH=main
export ALLOW_UNSAFE_REPO_PATH=true
export ALLSEQUENTIAL_VIRTUAL_TURNS_ENABLED=true
export ALLPARALLEL_VIRTUAL_TURNS_ENABLED=true
export ALL_PARALLEL_MODELS=git-inceptionlabs,git-chatgpt,git-grok,git-qwen,git-perplexity
export OPENCLAW_CRON_SSH_TARGET=ubuntu@192.168.90.86
export OPENCLAW_CRON_CHANNEL=telegram
export OPENCLAW_CRON_TO=7706210501
export OPENCLAW_CRON_CLI_PATH=/home/ubuntu/.npm-global/bin/openclaw
export OPENCLAW_CRON_TIMEOUT_SECONDS=30
export JOB_TIMEOUT_SECONDS=900

nohup .venv/bin/uvicorn app.main:app --host '__HOST__' --port '__PORT__' --app-dir . > /tmp/git_inference_api.log 2>&1 < /dev/null &
echo $! > /tmp/git_inference_api.pid

for i in $(seq 1 20); do
  if curl -fsS --max-time 2 http://127.0.0.1:__PORT__/health >/dev/null 2>&1; then
    curl -fsS --max-time 5 http://127.0.0.1:__PORT__/health
    exit 0
  fi
  sleep 1
done

echo "ERROR: API failed health check on port __PORT__" >&2
exit 1
'@

$bashScript = $bashScriptTemplate.
    Replace("__API_DIR__", $apiDirWsl).
    Replace("__HOST__", $HostAddress).
    Replace("__PORT__", [string]$Port)
# Ensure WSL bash receives LF line endings even when this .ps1 file is CRLF.
$bashScript = $bashScript -replace "`r`n", "`n"

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bashScript))
$result = & $wslExe -d $Distro -- bash -lc "echo '$encoded' | base64 -d | bash"
$startExitCode = $LASTEXITCODE
if ($startExitCode -ne 0) {
    throw "WSL API start failed with exit code $startExitCode"
}
$wslPid = (& $wslExe -d $Distro -- bash -lc "cat /tmp/git_inference_api.pid 2>/dev/null || true").Trim()
$wslIp = (& $wslExe -d $Distro -- bash -lc "hostname -I | awk '{print `$1}'").Trim()
if (-not $wslIp) {
    throw "Could not determine WSL IP for distro '$Distro'"
}

$proxyScript = Join-Path $PSScriptRoot "wsl_tcp_proxy.py"
if (-not (Test-Path $proxyScript)) {
    throw "TCP proxy script missing: $proxyScript"
}

$proxyStateDir = Join-Path $env:LOCALAPPDATA "git-inference"
New-Item -ItemType Directory -Path $proxyStateDir -Force | Out-Null
$proxyPidFile = Join-Path $proxyStateDir "wsl_tcp_proxy.pid"
$proxyOutLog = Join-Path $proxyStateDir "wsl_tcp_proxy.out.log"
$proxyErrLog = Join-Path $proxyStateDir "wsl_tcp_proxy.err.log"

if (Test-Path $proxyPidFile) {
    $oldPidRaw = (Get-Content -Path $proxyPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPidRaw -match '^\d+$') {
        $oldPid = [int]$oldPidRaw
        $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($oldProc) {
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -Path $proxyPidFile -Force -ErrorAction SilentlyContinue
}

$proxyArgs = @(
    $proxyScript,
    "--listen-host", "0.0.0.0",
    "--listen-port", "$ProxyPort",
    "--target-host", $wslIp,
    "--target-port", "$Port",
    "--pid-file", $proxyPidFile
)

$proxyProc = Start-Process -FilePath "python" -ArgumentList $proxyArgs -PassThru -WindowStyle Hidden -RedirectStandardOutput $proxyOutLog -RedirectStandardError $proxyErrLog
Start-Sleep -Seconds 1
if ($proxyProc.HasExited) {
    throw "Failed to start TCP proxy process (exit code $($proxyProc.ExitCode))"
}

try {
    $null = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $ProxyPort) -TimeoutSec 5
} catch {
    throw "Proxy health check failed on port ${ProxyPort}: $($_.Exception.Message)"
}

Write-Output "API started in WSL distro '$Distro' on port $Port"
if ($wslPid) {
    Write-Output "WSL PID: $wslPid"
}
Write-Output "WSL IP: $wslIp"
Write-Output "TCP proxy: 0.0.0.0:${ProxyPort} -> ${wslIp}:${Port} (pid $($proxyProc.Id))"
if ($result) {
    Write-Output $result
}
