# Git-backed Ollama-style API (FastAPI)

This is a V1 prototype for the architecture you described:

1. Client calls an Ollama-like `/api/chat` endpoint.
2. API enqueues a job in SQLite.
3. A single background worker serializes all repo access.
4. The worker writes `requests/<job_id>.json`, commits, and pushes.
5. Your pipeline processes the request and writes either:
   - `responses/<job_id>.json`, or
   - `errors/<job_id>.json`
6. The worker fetches the result commit and stores the final result in SQLite.
7. The API returns the result inline if it finishes within 180 seconds; otherwise it returns `202 Accepted` with `job_id`.

## Current V1 constraints

- Python + FastAPI
- strict sequential processing
- required `Idempotency-Key` header
- one `user` message exactly
- optional one `system` message
- no assistant history
- no streaming (`stream` must be `false`)

## Files

- `app/main.py` - FastAPI endpoints
- `app/models.py` - request/response schemas
- `app/db.py` - SQLite queue, idempotency, job state
- `app/worker.py` - single background worker
- `app/git_ops.py` - repo sync, commit/push, result polling
- `requirements.txt`

## Request shape

```json
{
  "model": "git-chatgpt",
  "messages": [
    { "role": "system", "content": "Be concise." },
    { "role": "user", "content": "Explain CI." }
  ],
  "stream": false
}
```

Supported browser-backed model ids include:
- `git-chatgpt`
- `git-perplexity`
- `git-grok`
- `git-inceptionlabs`
- `git-qwen`
- `git-allsequential` (API fan-out: runs multiple models sequentially and returns ordered source-labeled sections)

## Response behavior

### Completed within API timeout

`200 OK`

```json
{
  "model": "git-chatgpt",
  "created_at": "2026-03-22T18:30:00Z",
  "message": {
    "role": "assistant",
    "content": "..."
  },
  "done": true,
  "job_id": "job_abc123"
}
```

### Still running after API timeout

`202 Accepted`

```json
{
  "job_id": "job_abc123",
  "status": "running",
  "done": false
}
```

## Pipeline contract

The worker writes request artifacts like:

```json
{
  "job_id": "job_abc123",
  "created_at": "2026-03-22T18:30:00Z",
  "type": "chat",
  "system_prompt": "Be concise.",
  "user_prompt": "Explain CI.",
  "request": {
    "model": "git-chatgpt",
    "messages": [
      { "role": "system", "content": "Be concise." },
      { "role": "user", "content": "Explain CI." }
    ],
    "stream": false
  }
}
```

Your pipeline should eventually push one of:

### Success

`responses/<job_id>.json`

```json
{
  "job_id": "job_abc123",
  "message": {
    "role": "assistant",
    "content": "Continuous integration is ..."
  },
  "done": true,
  "completed_at": "2026-03-22T18:31:10Z"
}
```

### Failure

`errors/<job_id>.json`

```json
{
  "job_id": "job_abc123",
  "error": {
    "code": "MODEL_EXECUTION_ERROR",
    "message": "Inference container exited with code 137"
  }
}
```

## Environment variables

Copy `.env.example` and set at least:

- `REPO_PATH` - local checkout used by the worker
- `REPO_BRANCH` - branch the worker pushes to
- `DB_PATH` - SQLite database path
- `GIT_AUTHOR_NAME`
- `GIT_AUTHOR_EMAIL`
- `ALL_SEQUENTIAL_MODELS` (optional, comma-separated model list used by `git-allsequential`; default: `git-perplexity,git-grok,git-inceptionlabs,git-qwen`)

### Telegram display note for `git-allsequential`

If you want `git-allsequential` output to arrive as large per-source Telegram messages (instead of many small intra-source chunks), set OpenClaw Telegram chunking to paragraph mode with a high limit:

```bash
openclaw config set channels.telegram.chunkMode newline
openclaw config set channels.telegram.textChunkLimit 3800
```

Each source section is labeled in the response as:

`[index/total] Source: <model> | Status: <status>`

`git-allsequential` also compacts internal blank-line runs in each source reply so newline chunking prefers source boundaries.
If one source reply is still too long for Telegram chunk limits, the API pre-splits it and repeats the source header with `Part x/y` on each segment.

The code assumes `origin/<branch>` already exists unless `AUTO_INIT_REPO=true`.

## Initialize, start, and test (macOS/Linux/WSL)

### 1. Create venv and install deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Initialize a local demo remote/work repos

```bash
bash tools/local_demo_setup.sh /tmp/git_inference_demo
```

### 3. Start the API

```bash
export REPO_PATH=/tmp/git_inference_demo/api-workrepo
export DB_PATH=/tmp/git_inference_demo/jobs.db
export REPO_BRANCH=main
uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-di
r .
```

### 4. Send a test request

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-001' \
  -d '{
    "model": "git-chatgpt",
    "messages": [
      {"role": "system", "content": "Be concise."},
      {"role": "user", "content": "Explain CI."}
    ],
    "stream": false
  }'
```

Or use the helper script:

```bash
bash tools/send_chat.sh --api-base-url "http://127.0.0.1:8000" --prompt "Tell me a joke"
```

## Initialize, start, and test (PowerShell on Windows + WSL API)

### 1. In WSL, run the API

```bash
cd /mnt/c/Users/bspilker/repos/git-inference/git_inference_api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash tools/local_demo_setup.sh /tmp/git_inference_demo
export REPO_PATH=/tmp/git_inference_demo/api-workrepo
export DB_PATH=/tmp/git_inference_demo/jobs.db
export REPO_BRANCH=main
uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir .
```

### 2. Trigger the workflow pipeline

Use the repository workflow to process queued requests (or wait for push triggers):

```bash
# from repo root
# gh workflow run process-requests.yml
```

### 3. In PowerShell, send request

```powershell
cd C:\Users\bspilker\repos\git-inference\git_inference_api\tools
./send_chat.ps1 -ApiBaseUrl "http://127.0.0.1:8000" -Prompt "Tell me a joke"
```

### 4. In WSL/Git Bash, send request with bash helper

```bash
cd /mnt/c/Users/bspilker/repos/git-inference/git_inference_api
bash tools/send_chat.sh --api-base-url "http://127.0.0.1:8000" --prompt "Tell me a joke"
```

## Important note on Git conflicts

This prototype avoids API-side Git conflicts by design:

- request handlers never touch the repo directly
- exactly one worker owns repo writes
- the worker syncs from `origin/<branch>` before each new request submission

That said, your pipeline also writes to the repo. This is why the worker always refreshes from remote before reading results and before writing the next request.

## Known limitations

- one process / one worker only
- no streaming
- no real multi-turn chat state
- no auth layer
- no cancellation endpoint
- no observability/exported metrics yet

## Recommended next steps

1. Add auth.
2. Add `/api/generate`.
3. Add structured logging.
4. Add recovery rules for stuck jobs.
5. Add queue position in `POST /api/chat` responses.
6. Add tests with a fake repo remote.

## Local demo

If you want to test this without your real pipeline:

```bash
bash tools/local_demo_setup.sh /tmp/git_inference_demo
```

Then run the API with:

```bash
export REPO_PATH=/tmp/git_inference_demo/api-workrepo
export DB_PATH=/tmp/git_inference_demo/jobs.db
uvicorn app.main:app --reload --app-dir .
```

Then send a request to `/api/chat`.


## Additional pipeline artifact support

The worker now understands failure and terminal-state artifacts in more than one shape.

Supported failure locations:

- `errors/<job_id>.json`
- `status/<job_id>.json`
- `responses/<job_id>.json` when the response payload itself declares failure

Recognized failure shapes include:

```json
{"error": {"code": "MODEL_EXECUTION_ERROR", "message": "Inference failed"}}
```

```json
{"failed": true, "message": "Inference failed", "code": "MODEL_EXECUTION_ERROR"}
```

```json
{"status": "failed", "message": "Inference failed"}
```

```json
{"state": "error", "detail": "Container exited 137"}
```

Supported success fallback location:

- `status/<job_id>.json`

For example:

```json
{
  "job_id": "job_abc123",
  "status": "completed",
  "done": true,
  "message": {
    "role": "assistant",
    "content": "..."
  }
}
```
