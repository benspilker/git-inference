# Duck.ai website generation

This branch runs a multi-turn Duck.ai website-generation workflow from GitHub Actions. Each candidate uses a fresh logged-out Duck.ai chat. When a source URL is provided, Duck.ai first reviews the live site in that chat and returns a structured audit; the generation request then uses that audit in the same conversation.

## Run it

On branch `duckai`, open **Actions → Duck.ai Website Automation → Run workflow** and provide a website request, a short design brief, and an optional source URL. Alternatively, edit one or more of these files and push to `duckai`:

- [`website_request.txt`](website_request.txt): site, audience, content, and conversion requirements.
- [`design_brief.txt`](design_brief.txt): visual direction and implementation priorities.
- [`source_url.txt`](source_url.txt): optional public site for the in-chat source audit.

The workflow uses Ubuntu, a pinned Playwright Python container, and headed Chromium under Xvfb for Duck.ai. It asks for CSS parts 1–3, sends `acknowledged` as a separate user turn, and then asks for HTML parts 1–3 in the same chat. It verifies and assembles those captured parts, creates an inlined-CSS version, and renders the result for browser QA. The documented end-to-end sequence and output layout are in [`docs/workflow.md`](docs/workflow.md).

The action attempts up to six candidates and requires three to pass its automated checks. Every attempted candidate is saved to `duckai-runs/` and committed to `duckai`, including failures, so reports can be inspected. Generated-output commits use `[skip ci]` and do not start another generation run. The separate **Duck.ai Rendered Website QA** action reruns browser and structural checks on an already saved batch without opening new Duck.ai chats.

## What the checks mean

The automated checks require captured files and expected structure, then use Playwright to inspect the rendered page at desktop and mobile sizes. They check requested sections, hero sizing, first-screen CTA placement, mobile menu behavior, horizontal overflow, fonts and images, and CTA labels/icons for arrows. These checks do not judge whether the design is attractive or whether an image subject is appropriate.

`automated_success: true` means the structural and browser checks passed. Image relevance and screenshot-based visual design review are separate human checks; they remain pending unless a reviewer records that evidence. The workflow does not claim that three candidates have received human approval.

The CTA rule is part of both the generation prompt and structural checker: CTA buttons and button-style links should use clear action text without arrow glyphs or directional icons, including chevrons. The checker reports `cta_buttons_no_arrows` and fails the automated candidate if it finds one.

## Safety and access

The browser stays logged out. If Duck.ai requires sign-in or presents a human-verification or usage gate, the candidate stops and saves its failure evidence; the workflow does not try to bypass the gate. Do not put secrets or private information in the input files: they are committed to Git history. The action needs permission to write candidate evidence to the branch.

## Multi-turn smoke test

The separate **Duck.ai Multi-turn Smoke Test** checks two related questions in one guest chat and saves the captured conversation as JSON and Markdown under `duckai-runs/`. See [`smoke_test.json`](smoke_test.json) for its prompts.
