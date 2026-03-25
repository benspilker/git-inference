#!/usr/bin/env bash
set -eu

BASE_DIR="${1:-/tmp/git_inference_demo}"
BRANCH="main"

REMOTE_REPO="$BASE_DIR/remote.git"
API_REPO="$BASE_DIR/api-workrepo"
PIPELINE_REPO="$BASE_DIR/pipeline-workrepo"

mkdir -p "$BASE_DIR"
rm -rf "$REMOTE_REPO" "$API_REPO" "$PIPELINE_REPO"

git init --bare "$REMOTE_REPO"
git clone "$REMOTE_REPO" "$API_REPO"
git -C "$API_REPO" checkout -b "$BRANCH"
mkdir -p "$API_REPO/requests" "$API_REPO/responses" "$API_REPO/errors"
git -C "$API_REPO" add requests responses errors
git -C "$API_REPO" -c user.name='demo' -c user.email='demo@example.com' commit -m 'initial layout'
git -C "$API_REPO" push -u origin "$BRANCH"

git clone "$REMOTE_REPO" "$PIPELINE_REPO"
git -C "$PIPELINE_REPO" checkout "$BRANCH"

echo "Remote repo:   $REMOTE_REPO"
echo "API repo:      $API_REPO"
echo "Pipeline repo: $PIPELINE_REPO"
