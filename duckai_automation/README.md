# Duck.ai website generation on GitHub Actions

The workflow is ready on branch `duckai`. Select **Duck.ai Website Automation** from the Actions tab and choose `duckai`, or update the input files below to run it on push. Generated output commits do not start another generation run.

- `duckai_automation/website_request.txt`: the requested site and audience/conversion goal.
- `duckai_automation/design_brief.txt`: concise design direction, audience, and conversion goal. For a real site, do not paste a full fact audit here; Duck.ai assesses the source in the same chat before generating code.
- `duckai_automation/source_url.txt` (optional): public source website to assess. A URL in the website request or the manual `source_url` input also enables this preflight.

The manual workflow inputs use the same fields:

- **Website request**
- **Design brief**
- **Source URL** (optional)

The workflow creates candidates sequentially until three pass every automated structural and browser check, with at most six candidate attempts. Each attempt opens a fresh, logged-out Chromium context at `https://duck.ai/` and checks that guest chat is available. When a source URL is supplied, it enables Duck.ai Web Search, asks Duck.ai to assess the live site first, captures a structured source audit in the conversation, and requires the audit to list pages on the requested domain plus a named HTML5 UP demo and CC BY 3.0 license before continuing. The redesign request then refers back to that assessment in the same conversation instead of including a preassembled fact dump. After capture, the runner requests exactly three CSS chunks, submits `acknowledged` as its own user turn, and requests exactly three HTML chunks in the same conversation. It validates each code part, assembles the files, embeds `styles.css` into `files/inlined/index.html`, and runs Playwright browser QA against that final page. Browser QA captures desktop/mobile screenshots and saves objective rendered checks to `debug/browser_qa.json`.

All attempted run folders are committed, including candidates that failed automated checks, so their reports remain available for diagnosis. A workflow run succeeds only when three candidates pass the automated checks within six attempts. Each folder is saved to `duckai-runs/run-<run-id>-<attempt>-candidate-<candidate-index>/` and contains `files/index.html`, `files/styles.css`, `files/inlined/index.html`, captured chunks and manifest, `debug/inputs.json`, optional source-assessment JSON/Markdown, `conversation_turns.json` (source assessment when requested, CSS 1–3, standalone acknowledgment, then HTML 1–3 in one chat), screenshots, browser QA, and checker notes. The inlined version embeds CSS; fonts and images referenced by URL still load externally. The workflow does not commit the website request, design brief, or source URL. If Duck.ai gates guest access during any candidate, the action stops and does not attempt to bypass the gate.

The Playwright QA report checks that required sections render visibly at desktop and mobile sizes, the hero spans each viewport, a CTA fits on the first screen, the mobile menu opens and closes by click and keyboard, mobile content does not overflow horizontally, and browser fonts and images load. These are objective checks and use no AI inference. They do not assess whether the imagery is relevant or the design is attractive.

The separate `Duck.ai Rendered Website QA` workflow can recheck the newest saved candidate batch after browser or checker changes, without starting new Duck.ai chats. It refreshes each candidate's browser report, screenshots, checker report, and `run_notes.json`; a manual run can target a specific run ID.

The screenshot and image relevance review remains a human gate. `automated_success` in `run_notes.json` means the structural and browser checks passed; it does not approve visual design or image relevance. The action does not manufacture a passing visual review: `run_notes.json` will show visual review as pending until a reviewer inspects the exact rendered screenshots and image subjects.

If Duck.ai blocks guest access, asks for sign-in, or displays a human-verification or usage gate, the run stops without attempting to bypass it. Branch protection or repository rules may also prevent the workflow token from pushing; in that case, allow GitHub Actions to write to the branch or use a pull-request based output flow.

The two input files are committed to the repository when using the `push` trigger. Keep them free of credentials and other information you do not want stored in Git history.

## Multi-turn smoke test

The separate **Duck.ai Multi-turn Smoke Test** workflow sends two related, harmless questions in one fresh guest chat. It checks that both assistant answers render, that the second turn stays in the same conversation, and then commits the captured prompts and answers as `conversation.json` and `conversation.md` under `duckai-runs/smoke-run-<run-id>-<attempt>/`. Changing `duckai_automation/smoke_test.json` on `duckai` starts the test; the file can be edited to change the prompts.

## Runtime

The workflows use the same Playwright Python container/version as the repo's existing inference workflow (`mcr.microsoft.com/playwright/python:v1.58.0-noble` and `playwright==1.58.0`). The multi-turn smoke test runs Chromium headed under `xvfb-run`, matching the existing inference workflow's browser setup.
