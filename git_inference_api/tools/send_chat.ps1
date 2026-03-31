param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$Model = "git-chatgpt-json",
    [string]$Prompt,
    [switch]$ShowCombined,
    [switch]$CombinedInMessage,
    [string]$SystemPrompt = @"
You are a retrieval-first assistant.

Rules:
1. For time-sensitive or dynamic requests (weather, stocks, prices, sports, news, schedules, "today", "now", "current"), you must use web search before answering.
2. If web search is unavailable in this session, respond exactly with:
WEB_SEARCH_UNAVAILABLE
3. If web search is available, include concrete, current facts in the answer.
4. Do not claim uncertainty for time-sensitive requests when web search is available.
"@,
    [int]$PollIntervalSeconds = 10,
    [int]$MaxWaitSeconds = 600
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PollIntervalSeconds -lt 1) {
    throw "PollIntervalSeconds must be >= 1."
}
if ($MaxWaitSeconds -lt 1) {
    throw "MaxWaitSeconds must be >= 1."
}

if ([string]::IsNullOrWhiteSpace($Prompt)) {
    $Prompt = Read-Host "Enter prompt to send"
}
if ([string]::IsNullOrWhiteSpace($Prompt)) {
    throw "Prompt cannot be empty."
}

$idPart = ([guid]::NewGuid().ToString("N")).Substring(0, 8)
$key = "api-test-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())-$idPart"

$messages = @()
if (-not [string]::IsNullOrWhiteSpace($SystemPrompt)) {
    $messages += @{
        role = "system"
        content = $SystemPrompt.Trim()
    }
}
$messages += @{
    role = "user"
    content = $Prompt
}

$bodyObject = @{
    model = $Model
    messages = $messages
    stream = $false
}
if ($CombinedInMessage) {
    $bodyObject.options = @{
        return_combined = $true
        response_mode = "combined_json"
    }
}

$body = $bodyObject | ConvertTo-Json -Depth 10 -Compress

Write-Host "Sending request to $ApiBaseUrl/api/chat ..."
$ack = Invoke-RestMethod -Method Post `
    -Uri "$ApiBaseUrl/api/chat" `
    -Headers @{ "Idempotency-Key" = $key } `
    -ContentType "application/json" `
    -Body $body

Write-Host ("ACK: " + ($ack | ConvertTo-Json -Depth 10 -Compress))

if ($ack.done -eq $true -and $null -ne $ack.message) {
    if ($ShowCombined -and $null -ne $ack.combined) {
        Write-Host ""
        Write-Host "Combined:"
        Write-Output ($ack.combined | ConvertTo-Json -Depth 50)
        exit 0
    }
    elseif (-not [string]::IsNullOrWhiteSpace($ack.message.content)) {
        Write-Host ""
        Write-Host "Response:"
        Write-Output $ack.message.content
        exit 0
    }
}

if ([string]::IsNullOrWhiteSpace($ack.job_id)) {
    throw "API ACK did not include a job_id."
}

$jobId = $ack.job_id
$deadline = (Get-Date).ToUniversalTime().AddSeconds($MaxWaitSeconds)

while ((Get-Date).ToUniversalTime() -lt $deadline) {
    Start-Sleep -Seconds $PollIntervalSeconds

    $job = Invoke-RestMethod -Method Get -Uri "$ApiBaseUrl/api/jobs/$jobId"
    $status = [string]$job.status

    if ($status -eq "completed") {
        Write-Host ""
        if ($ShowCombined -and $null -ne $job.combined) {
            Write-Host "Combined:"
            Write-Output ($job.combined | ConvertTo-Json -Depth 50)
        }
        else {
            Write-Host "Response:"
            if ($null -ne $job.result -and $null -ne $job.result.message -and
                -not [string]::IsNullOrWhiteSpace($job.result.message.content)) {
                Write-Output $job.result.message.content
            }
            else {
                # Fallback for alternate successful payload shapes.
                Write-Output ($job.result | ConvertTo-Json -Depth 20)
            }
        }
        exit 0
    }

    if ($status -eq "failed" -or $status -eq "expired") {
        Write-Error ("Job ended with status '" + $status + "': " + ($job | ConvertTo-Json -Depth 20))
        exit 2
    }

    Write-Host ("Waiting... job_id=$jobId status=$status")
}

Write-Error "Timed out waiting for job completion."
exit 3
