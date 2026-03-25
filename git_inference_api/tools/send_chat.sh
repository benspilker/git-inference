#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash tools/send_chat.sh [options]

Options:
  --api-base-url URL           API base URL (default: http://127.0.0.1:8000)
  --model NAME                 Model name (default: internal-model)
  --prompt TEXT                Prompt text (if omitted, prompted interactively)
  --system-prompt TEXT         System prompt text (default retrieval-first rules)
  --poll-interval-seconds N    Poll interval for /api/jobs (default: 10)
  --max-wait-seconds N         Max wait for job completion (default: 600)
  -h, --help                   Show this help
EOF
}

API_BASE_URL="http://127.0.0.1:8000"
MODEL="internal-model"
PROMPT=""
POLL_INTERVAL_SECONDS=10
MAX_WAIT_SECONDS=600
SYSTEM_PROMPT=$'You are a retrieval-first assistant.\n\nRules:\n1. For time-sensitive or dynamic requests (weather, stocks, prices, sports, news, schedules, "today", "now", "current"), you must use web search before answering.\n2. If web search is unavailable in this session, respond exactly with:\nWEB_SEARCH_UNAVAILABLE\n3. If web search is available, include concrete, current facts in the answer.\n4. Do not claim uncertainty for time-sensitive requests when web search is available.'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-base-url)
      API_BASE_URL="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --prompt)
      PROMPT="$2"
      shift 2
      ;;
    --system-prompt)
      SYSTEM_PROMPT="$2"
      shift 2
      ;;
    --poll-interval-seconds)
      POLL_INTERVAL_SECONDS="$2"
      shift 2
      ;;
    --max-wait-seconds)
      MAX_WAIT_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! [[ "$POLL_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || [[ "$POLL_INTERVAL_SECONDS" -lt 1 ]]; then
  echo "Poll interval must be a positive integer." >&2
  exit 1
fi
if ! [[ "$MAX_WAIT_SECONDS" =~ ^[0-9]+$ ]] || [[ "$MAX_WAIT_SECONDS" -lt 1 ]]; then
  echo "Max wait must be a positive integer." >&2
  exit 1
fi

if [[ -z "${PROMPT// }" ]]; then
  read -r -p "Enter prompt to send: " PROMPT
fi
if [[ -z "${PROMPT// }" ]]; then
  echo "Prompt cannot be empty." >&2
  exit 1
fi

id_part="$(python -c 'import uuid; print(uuid.uuid4().hex[:8])')"
key="api-test-$(date -u +%s)-${id_part}"

body="$(
  python - "$MODEL" "$SYSTEM_PROMPT" "$PROMPT" <<'PY'
import json
import sys

model = sys.argv[1]
system_prompt = sys.argv[2].strip()
prompt = sys.argv[3]
messages = []
if system_prompt:
    messages.append({"role": "system", "content": system_prompt})
messages.append({"role": "user", "content": prompt})
print(json.dumps({"model": model, "messages": messages, "stream": False}, separators=(",", ":")))
PY
)"

echo "Sending request to ${API_BASE_URL}/api/chat ..."
ack_response="$(
  curl -sS -X POST "${API_BASE_URL}/api/chat" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: ${key}" \
    -d "$body" \
    -w $'\n%{http_code}'
)"
ack_status="${ack_response##*$'\n'}"
ack_json="${ack_response%$'\n'*}"

if [[ "${ack_status}" -lt 200 || "${ack_status}" -ge 300 ]]; then
  echo "API request failed (HTTP ${ack_status}): ${ack_json}" >&2
  exit 1
fi

echo "ACK: ${ack_json}"

ack_done="$(printf '%s' "$ack_json" | python -c 'import json,sys; obj=json.load(sys.stdin); print(str(bool(obj.get("done", False))).lower())')"
ack_content="$(printf '%s' "$ack_json" | python -c 'import json,sys; obj=json.load(sys.stdin); msg=obj.get("message") or {}; print(msg.get("content","") if isinstance(msg, dict) else "")')"
ack_job_id="$(printf '%s' "$ack_json" | python -c 'import json,sys; obj=json.load(sys.stdin); print(obj.get("job_id","") or "")')"

if [[ "$ack_done" == "true" && -n "${ack_content}" ]]; then
  echo
  echo "Response:"
  printf '%s\n' "$ack_content"
  exit 0
fi

if [[ -z "${ack_job_id}" ]]; then
  echo "API ACK did not include a job_id." >&2
  exit 1
fi

deadline=$(( $(date -u +%s) + MAX_WAIT_SECONDS ))
while [[ "$(date -u +%s)" -lt "$deadline" ]]; do
  sleep "$POLL_INTERVAL_SECONDS"

  job_response="$(
    curl -sS -X GET "${API_BASE_URL}/api/jobs/${ack_job_id}" -w $'\n%{http_code}'
  )"
  job_status_code="${job_response##*$'\n'}"
  job_json="${job_response%$'\n'*}"

  if [[ "${job_status_code}" -lt 200 || "${job_status_code}" -ge 300 ]]; then
    echo "Job status request failed (HTTP ${job_status_code}): ${job_json}" >&2
    exit 1
  fi

  status="$(printf '%s' "$job_json" | python -c 'import json,sys; obj=json.load(sys.stdin); print(obj.get("status",""))')"

  if [[ "$status" == "completed" ]]; then
    content="$(printf '%s' "$job_json" | python -c 'import json,sys; obj=json.load(sys.stdin); result=obj.get("result") or {}; msg=result.get("message") if isinstance(result, dict) else None; print(msg.get("content","") if isinstance(msg, dict) else "")')"
    echo
    echo "Response:"
    if [[ -n "$content" ]]; then
      printf '%s\n' "$content"
    else
      printf '%s\n' "$(printf '%s' "$job_json" | python -c 'import json,sys; obj=json.load(sys.stdin); print(json.dumps(obj.get("result"), ensure_ascii=False))')"
    fi
    exit 0
  fi

  if [[ "$status" == "failed" || "$status" == "expired" ]]; then
    echo "Job ended with status '${status}': ${job_json}" >&2
    exit 2
  fi

  echo "Waiting... job_id=${ack_job_id} status=${status}"
done

echo "Timed out waiting for job completion." >&2
exit 3
