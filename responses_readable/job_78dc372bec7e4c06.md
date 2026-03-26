You can’t literally make a single Git commit exist on multiple branches simultaneously—but you can automate creating identical commits on multiple branches and pushing them in parallel so GitHub Actions runs for each branch at the same time.

Here’s a practical Bash script that does exactly that:

✅ Bash script: commit + push to multiple branches in parallel
Bash
#!/usr/bin/env bash

set -euo pipefail

# Usage:
# ./multi-commit.sh "Commit message" branch1 branch2 branch3 ...

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 \"commit message\" branch1 [branch2 ...]"
  exit 1
fi

COMMIT_MSG="$1"
shift
BRANCHES=("$@")

# Ensure working tree is clean or staged as intended
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "⚠️ You have uncommitted changes. They will be included."
fi

# Save current branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Create a temporary patch of current staged changes
git add -A
PATCH_FILE=$(mktemp)
git diff --cached > "$PATCH_FILE"

echo "📦 Patch created at $PATCH_FILE"

# Function to apply patch, commit, and push
process_branch() {
  local branch=$1

  echo "🚀 Processing $branch"

  git checkout "$branch"

  # Apply patch
  git apply "$PATCH_FILE"

  git add -A
  git commit -m "$COMMIT_MSG" || echo "Nothing to commit on $branch"

  # Push in background
  git push origin "$branch"

  echo "✅ Done $branch"
}

# Run in parallel
PIDS=()

for branch in "${BRANCHES[@]}"; do
  process_branch "$branch" &
  PIDS+=($!)
done

# Wait for all
for pid in "${PIDS[@]}"; do
  wait "$pid"
done

# Return to original branch
git checkout "$CURRENT_BRANCH"

# Cleanup
rm "$PATCH_FILE"

echo "🎉 All branches updated and pushed."
🔧 What this does

Takes your current changes (staged or not)

Converts them into a patch

Applies that patch to each target branch

Commits with the same message

Pushes all branches in parallel

Triggers GitHub Actions pipelines simultaneously

⚡ Example usage
Bash
./multi-commit.sh "Update config" dev staging prod
⚠️ Important caveats

If branches have diverged significantly, the patch may fail to apply cleanly

Each branch gets a different commit hash (that’s unavoidable in Git)

Parallel pushes may stress rate limits if you do many branches at once

If workflows depend on unique changes per branch, this approach might not fit

💡 Alternative (cleaner if applicable)

If your branches are meant to stay in sync, consider:

Merging one branch into others

Or using a single branch + matrix builds in GitHub Actions

If you want, I can also give you a version that:

Rebases instead of patching

Uses git worktree for safer parallelism

Or triggers workflows via API instead of commits
