# Duck.ai website generation

This branch runs a multi-turn Duck.ai website-generation workflow from GitHub Actions. Each candidate uses a fresh logged-out Duck.ai chat. Each CSV row supplies a website URL, business name, and industry category. Duck.ai receives the name and category as identification and design context, then assesses the live site in that same chat and uses the audit as its factual source.

## Run it

On branch `duckai`, add or edit rows in [`source_urls.csv`](source_urls.csv) and push the change. The CSV requires `business`, `category`, and `website` columns; `location` is optional. Every valid row is a separate target; a change to the CSV processes all rows. Changes to the reusable request or design brief do not start a generation run. To redesign one site manually, use **Actions → Duck.ai Website Automation → Run workflow**. Choose `full_batch` for the CSV or a manual URL override. Choose `smoke_test` to run exactly one candidate against one CSV row; enter a `smoke_website` value to select a row, or leave it blank to use the first row.

### CSV fields currently used

| Column | Required | Use in the workflow |
| --- | --- | --- |
| `website` | Yes | The target site Duck.ai assesses and the domain used to validate its source audit. |
| `business` | Yes | Identifies the intended business; sent to Duck.ai as unverified context. |
| `category` | Yes | Primary industry cue; sent to Duck.ai as unverified design context. |
| `location` | No | Sent as unverified location context; use as a fact only if the source site confirms it. |

Only these four columns are read. Other columns in a CSV are ignored. The website is authoritative for factual claims; business, category, and optional location help Duck.ai identify the target and guide design. The current CSV contains 21 websites.

- [`website_request.txt`](website_request.txt): reusable content, sourcing, asset, and output requirements.
- [`design_brief.txt`](design_brief.txt): reusable guidance for deriving a brand-specific visual direction from the source.
- [`source_urls.csv`](source_urls.csv): one row per website target; the business and category fields are sent to Duck.ai.

Keep the request and brief separate: the request defines what to learn and build; the brief translates the source site's brand into a design, using industry cues as a secondary guide rather than assuming a preset palette. The runner combines them in the same prompt, so this separation does not add a chat turn.

The workflow uses Ubuntu, a pinned Playwright Python container, and headed Chromium under Xvfb for Duck.ai. It asks for CSS parts 1–3, sends `acknowledged` as a separate user turn, and then asks for HTML parts 1–3 in the same chat. It verifies and assembles those captured parts, creates an inlined-CSS version, and renders the result for browser QA. The documented end-to-end sequence and output layout are in [`docs/workflow.md`](docs/workflow.md).

One workflow execution validates the CSV and creates one matrix job per website. Those jobs run one at a time. Each site job generates full candidates in a loop until it has three automated passes or has made six attempts; it does not start three separate workflow executions. Thus each site can take 3–6 candidate runs, and the current 21-row CSV can produce up to 126 attempts. Each site job has a 35-minute limit. Every attempted candidate is saved to `duckai-runs/` and committed to `duckai`, including failures, so reports can be inspected. Generated-output commits use `[skip ci]` and do not start another generation run. The separate **Duck.ai Rendered Website QA** action reruns browser and structural checks for every website in a saved batch without opening new Duck.ai chats.

The manual `smoke_test` mode still runs one complete source assessment, CSS/acknowledgment/HTML capture, assembly, and browser QA. It limits the run to one selected CSV row and one candidate attempt, requiring one automated pass. This checks the CSV-to-prompt path without generating the full batch.

## What the checks mean

The automated checks require captured files and expected structure, then use Playwright to inspect the rendered page at desktop and mobile sizes. They check requested sections, hero sizing, first-screen CTA placement, mobile menu behavior, horizontal overflow, fonts and images, and CTA labels/icons for arrows. These checks do not judge whether the design is attractive or whether an image subject is appropriate.

`automated_success: true` means the structural and browser checks passed. Image relevance and screenshot-based visual design review are separate human checks; they remain pending unless a reviewer records that evidence. The workflow does not claim that three candidates have received human approval.

The CTA rule is part of both the generation prompt and structural checker: CTA buttons and button-style links should use clear action text without arrow glyphs or directional icons, including chevrons. The checker reports `cta_buttons_no_arrows` and fails the automated candidate if it finds one.

## Safety and access

The browser stays logged out. If Duck.ai requires sign-in or presents a human-verification or usage gate, the candidate stops and saves its failure evidence; the workflow does not try to bypass the gate. Keep the CSV limited to the fields the workflow uses because it is committed to Git history. The action needs permission to write candidate evidence to the branch.

## Multi-turn smoke test

The separate **Duck.ai Multi-turn Smoke Test** checks two related questions in one guest chat and saves the captured conversation as JSON and Markdown under `duckai-runs/`. See [`smoke_test.json`](smoke_test.json) for its prompts.
