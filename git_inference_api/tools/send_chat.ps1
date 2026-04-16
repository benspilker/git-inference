param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$Model = "git-chatgpt",
    [string]$Prompt = "",
    [string]$SystemPrompt = "You are a retrieval-first assistant.",
    [switch]$ShowCombined,
    [switch]$CombinedInMessage,
    [ValidateRange(1, 3600)][int]$PollIntervalSeconds = 10,
    [ValidateRange(1, 86400)][int]$MaxWaitSeconds = 600
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Prompt)) {
    $Prompt = Read-Host "Enter prompt to send"
}
if ([string]::IsNullOrWhiteSpace($Prompt)) {
    throw "Prompt cannot be empty."
}

$idPart = ([guid]::NewGuid().ToString("N")).Substring(0, 8)
$idempotencyKey = "api-test-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())-$idPart"

$messages = @(
    @{ role = "system"; content = $SystemPrompt },
    @{ role = "user"; content = $Prompt }
)
$payload = @{
    model = $Model
    messages = $messages
    stream = $false
}
if ($CombinedInMessage.IsPresent) {
    $payload.options = @{
        return_combined = $true
        response_mode = "combined_json"
    }
}

$chatUri = "$($ApiBaseUrl.TrimEnd('/'))/api/chat"
$headers = @{
    "Content-Type" = "application/json"
    "Idempotency-Key" = $idempotencyKey
}

Write-Host "Sending request to $chatUri ..."
$ack = Invoke-RestMethod -Method Post -Uri $chatUri -Headers $headers -Body ($payload | ConvertTo-Json -Depth 20 -Compress)
Write-Host ("ACK: " + ($ack | ConvertTo-Json -Depth 20 -Compress))

$ackDone = [bool]($ack.done)
$ackContent = ""
if ($ack.message -and $ack.message.content) {
    $ackContent = [string]$ack.message.content
}

if ($ackDone -and -not [string]::IsNullOrWhiteSpace($ackContent)) {
    if ($ShowCombined.IsPresent -and $ack.combined) {
        Write-Host ""
        Write-Host "Combined:"
        $ack.combined | ConvertTo-Json -Depth 20
    } else {
        Write-Host ""
        Write-Host "Response:"
        Write-Host $ackContent
    }
    exit 0
}

$jobId = [string]$ack.job_id
if ([string]::IsNullOrWhiteSpace($jobId)) {
    throw "API ACK did not include a job_id."
}

$deadline = (Get-Date).ToUniversalTime().AddSeconds($MaxWaitSeconds)
$jobUri = "$($ApiBaseUrl.TrimEnd('/'))/api/jobs/$jobId"

while ((Get-Date).ToUniversalTime() -lt $deadline) {
    Start-Sleep -Seconds $PollIntervalSeconds
    $job = Invoke-RestMethod -Method Get -Uri $jobUri
    $status = [string]$job.status

    if ($status -eq "completed") {
        $content = ""
        if ($job.result -and $job.result.message -and $job.result.message.content) {
            $content = [string]$job.result.message.content
        }

        Write-Host ""
        if ($ShowCombined.IsPresent -and $job.combined) {
            Write-Host "Combined:"
            $job.combined | ConvertTo-Json -Depth 20
        } elseif (-not [string]::IsNullOrWhiteSpace($content)) {
            Write-Host "Response:"
            Write-Host $content
        } else {
            Write-Host "Result:"
            $job.result | ConvertTo-Json -Depth 20
        }
        exit 0
    }

    if ($status -eq "failed" -or $status -eq "expired") {
        throw "Job ended with status '$status': $($job | ConvertTo-Json -Depth 20 -Compress)"
    }

    Write-Host "Waiting... job_id=$jobId status=$status"
}

throw "Timed out waiting for job completion."
