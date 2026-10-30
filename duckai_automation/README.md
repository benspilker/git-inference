# Duck.ai website generation on GitHub Actions

The workflow is ready on branch `duckai`. To run it before it is merged into the repository's default branch, add both input files below to that branch and push them. The `push` trigger runs only when either input file changes, so generated output commits do not start another generation run. After the workflow file reaches the default branch, you can also use **Duck.ai Website Automation** from the Actions tab and select `duckai` as the branch.

- `duckai_automation/website_request.txt`: the requested site and audience/conversion goal.
- `duckai_automation/design_brief.txt`: verified business facts, chosen visual direction, imagery guidance, and the selected HTML5 UP template/source/license details if using one.

The manual workflow inputs use the same fields:

- **Website request**
- **Design brief**

The workflow opens a fresh, non-persistent Chromium context at `https://duck.ai/`, checks that guest chat is available, and requests exactly three CSS chunks followed by three HTML chunks in the same conversation. It captures only each newly rendered code block, validates its part label and first-line marker, records hashes in the manifest, assembles the files, renders desktop/mobile screenshots, and runs the structural checker.

On success, the generated run is committed and pushed to `duckai-runs/run-<run-id>-<attempt>/`. The folder contains `files/index.html`, `files/styles.css`, captured chunks and manifest, screenshots, and checker notes. The workflow does not commit the website request or design brief.

The screenshot and image relevance review remains a human gate. The action does not manufacture a passing visual review: `run_notes.json` will show visual review as pending until a reviewer inspects the exact rendered screenshots and image subjects. A generated candidate is saved even while that review is pending.

If Duck.ai blocks guest access, asks for sign-in, or displays a human-verification or usage gate, the run stops without attempting to bypass it. Branch protection or repository rules may also prevent the workflow token from pushing; in that case, allow GitHub Actions to write to the branch or use a pull-request based output flow.

The two input files are committed to the repository when using the `push` trigger. Keep them free of credentials and other information you do not want stored in Git history.

## Multi-turn smoke test

The separate **Duck.ai Multi-turn Smoke Test** workflow sends two related, harmless questions in one fresh guest chat. It checks that both assistant answers render, that the second turn stays in the same conversation, and then commits the captured prompts and answers as `conversation.json` and `conversation.md` under `duckai-runs/smoke-run-<run-id>-<attempt>/`. Changing `duckai_automation/smoke_test.json` on `duckai` starts the test; the file can be edited to change the prompts.

## Runtime

The workflow uses the same Playwright Python container/version as the repo's existing inference workflow (`mcr.microsoft.com/playwright/python:v1.58.0-noble` and `playwright==1.58.0`). Chromium runs headless; Xvfb is not needed for the normal run.
