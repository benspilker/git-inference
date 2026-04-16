#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SCRIPT_DIR}"

REPO_PATH="${REPO_PATH:-/tmp/git_inference_github/api-workrepo}"
REPO_BRANCH="${REPO_BRANCH:-main}"
DB_PATH="${DB_PATH:-/tmp/git_inference_github/jobs.db}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
ALLOW_UNSAFE_REPO_PATH="${ALLOW_UNSAFE_REPO_PATH:-false}"

if [[ -f "${APP_DIR}/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "${APP_DIR}/.venv/bin/activate"
fi

if [[ ! -d "${REPO_PATH}/.git" ]]; then
  echo "ERROR: REPO_PATH is not a git repo: ${REPO_PATH}" >&2
  echo "Set REPO_PATH to your api workrepo (for example /tmp/git_inference_github/api-workrepo)." >&2
  exit 1
fi

if [[ "${ALLOW_UNSAFE_REPO_PATH,,}" != "true" ]]; then
  if [[ -f "${REPO_PATH}/.github/workflows/process-requests.yml" && -f "${REPO_PATH}/git_inference_api/app/main.py" ]]; then
    echo "ERROR: REPO_PATH appears to be the source repo checkout: ${REPO_PATH}" >&2
    echo "The worker runs destructive sync (fetch/reset --hard/clean) on REPO_PATH." >&2
    echo "Use a dedicated workrepo clone. To bypass intentionally, set ALLOW_UNSAFE_REPO_PATH=true." >&2
    exit 1
  fi
fi

if ! git -C "${REPO_PATH}" fetch origin "${REPO_BRANCH}" >/dev/null 2>&1; then
  echo "ERROR: git fetch origin ${REPO_BRANCH} failed in ${REPO_PATH}" >&2
  echo "Check remote auth and branch name, then retry." >&2
  exit 1
fi

export REPO_PATH
export REPO_BRANCH
export DB_PATH

echo "Starting API with:"
echo "  REPO_PATH=${REPO_PATH}"
echo "  REPO_BRANCH=${REPO_BRANCH}"
echo "  DB_PATH=${DB_PATH}"
echo "  HOST=${HOST}"
echo "  PORT=${PORT}"

exec uvicorn app.main:app --host "${HOST}" --port "${PORT}" --app-dir "${APP_DIR}"
