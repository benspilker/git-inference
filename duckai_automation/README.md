# Duck.ai website generation on GitHub Actions

The workflow is ready on branch `duckai`. To run it before it is merged into the repository's default branch, add both input files below to that branch and push them. The `push` trigger runs when either input file or the automation code/workflow changes, so generated output commits do not start another generation run. After the workflow file reaches the default branch, you can also use **Duck.ai Website Automation** from the Actions tab and select `duckai` as the branch.

- `duckai_automation/website_request.txt`: the requested site and audience/conversion goal.
- `duckai_automation/design_brief.txt`: verified business facts, chosen visual direction, imagery guidance, and the selected HTML5 UP template/source/license details if using one.

The manual workflow inputs use the same fields:

- **Website request**
- **Design brief**

The workflow creates three independent candidates sequentially. Each opens a fresh, logged-out Chromium context at `https://duck.ai/`, checks that guest chat is available, and requests exactly three CSS chunks in one conversation. After capturing and validating all CSS, the runner submits `acknowledged` as its own user turn and waits for Duck.ai to finish responding. It then requests exactly three HTML chunks in that same conversation. The runner captures only each newly rendered code block, validates its part label and first-line marker, records hashes in the manifest, assembles the files, renders desktop/mobile screenshots, and runs the structural checker. A following step creates `files/inlined/index.html` by embedding `styles.css` in a `<style>` element; the source HTML and CSS remain alongside it.

On success, all three generated run folders are committed and pushed to `duckai-runs/run-<run-id>-<attempt>-candidate-1/`, `-candidate-2/`, and `-candidate-3/`. Each contains `files/index.html`, `files/styles.css`, `files/inlined/index.html`, captured chunks and manifest, `conversation_turns.json` (showing CSS 1–3, the standalone acknowledgment, then HTML 1–3 in one chat), screenshots, and checker notes. The inlined version embeds CSS; fonts and images referenced by URL still load externally. The workflow does not commit the website request or design brief. If Duck.ai gates guest access during any candidate, the action stops and does not attempt to bypass the gate.

The screenshot and image relevance review remains a human gate. The action does not manufacture a passing visual review: `run_notes.json` will show visual review as pending until a reviewer inspects the exact rendered screenshots and image subjects. A generated candidate is saved even while that review is pending.

If Duck.ai blocks guest access, asks for sign-in, or displays a human-verification or usage gate, the run stops without attempting to bypass it. Branch protection or repository rules may also prevent the workflow token from pushing; in that case, allow GitHub Actions to write to the branch or use a pull-request based output flow.

The two input files are committed to the repository when using the `push` trigger. Keep them free of credentials and other information you do not want stored in Git history.

## Multi-turn smoke test

The separate **Duck.ai Multi-turn Smoke Test** workflow sends two related, harmless questions in one fresh guest chat. It checks that both assistant answers render, that the second turn stays in the same conversation, and then commits the captured prompts and answers as `conversation.json` and `conversation.md` under `duckai-runs/smoke-run-<run-id>-<attempt>/`. Changing `duckai_automation/smoke_test.json` on `duckai` starts the test; the file can be edited to change the prompts.

## Runtime

The workflows use the same Playwright Python container/version as the repo's existing inference workflow (`mcr.microsoft.com/playwright/python:v1.58.0-noble` and `playwright==1.58.0`). The multi-turn smoke test runs Chromium headed under `xvfb-run`, matching the existing inference workflow's browser setup.
