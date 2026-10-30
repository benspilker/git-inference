---
name: duckai-automation-github-actions
description: Run three independent Duck.ai website candidates in logged-out Playwright browsers from GitHub Actions, capture and verify six CSS/HTML chunks per candidate, assemble and inline files, render screenshots, and save each run to the duckai branch.
category: productivity
---

# Duck.ai Website Automation (GitHub Actions)

## GitHub Actions runtime

Run the `Duck.ai Website Automation` workflow manually on the `duckai` branch. It uses the repository's pinned Playwright Python container (`mcr.microsoft.com/playwright/python:v1.58.0-noble`, matching `playwright==1.58.0`). For Duck.ai inference, Chromium runs headed under Xvfb to match the repository's existing inference workflow.

The workflow creates a fresh, non-persistent browser context for each candidate. It must stay logged out. If Duck.ai blocks guest chat, requires sign-in, or presents a human-verification/usage gate, stop and report it; do not evade the gate.

The workflow requests CSS parts 1–3, sends `acknowledged` as its own user turn and waits for Duck.ai to complete its response, then requests HTML parts 1–3 in that same conversation. It captures the newest rendered assistant message's single newly added code block and visible part label. Duck.ai's UI renders Markdown without its original backticks, so the runner wraps that message-scoped visible code block in the expected fence for validation. It never reads prior chat messages as the current response. Accepted chunks, hashes, and the captured turn order are saved in the run folder.

The GitHub Action runs three independent candidates sequentially. Each uses a fresh Duck.ai chat and captures CSS parts 1–3, sends `acknowledged` as its own user turn, then captures HTML parts 1–3. After assembly, the workflow saves desktop/mobile screenshots and runs the structural checker. Screenshot generation is not a visual audit. The workflow commits generated files and evidence under `duckai-runs/run-<run-id>-<attempt>-candidate-1/`, `-candidate-2/`, and `-candidate-3/`; it does not claim image relevance or visual review passed. Complete those human gates before describing a candidate as successful.

After generation and checking, the GitHub Action also writes `files/inlined/index.html`, which embeds the assembled `styles.css` into the assembled HTML. Keep the original `files/index.html` and `files/styles.css` for source review and checker compatibility. This inlined output embeds CSS only; externally referenced fonts and images still need network access.

Use this skill when the user asks Hermes to create or iterate on a website with Duck.ai. The provider is Duck.ai (`https://duck.ai/`).

## Run a website request

Open Duck.ai in a fresh private chat and verify the logged-out private-chat UI is available before sending the request. Do not sign in or subscribe. If Duck.ai blocks the guest chat, stop and report the gate instead of trying to bypass it.

For a normal single-site pilot, generate one candidate. When the user requests multiple successful versions or a high-candidate batch, make the requested number of separate candidates and count only candidates that pass every gate below.

Before sending code requests, prepare a compact design brief from the business and verified source material. Include the audience and conversion goal, one primary composition archetype, a specific visual direction, a small token palette and type pairing, page-section sequence, image treatment, and explicit desktop/mobile layout decisions. Pick an archetype that fits the business (for example, Heritage/Craft for a brewery or Bold Commercial for a contractor); do not apply one aesthetic to every candidate. Inspect the selected HTML5 UP demo and note its name, URL, useful structural patterns, and license. Do not show a visible design-reference label, template credit, or template link in the rendered page. Preserve template attribution in a concise HTML source comment, and do not copy template branding or unrelated proprietary copy. For HTML5 UP, include the template name, source URL, CC BY 3.0 license URL, and a note that the design was adapted. Tell Duck.ai what to adapt from the template and what must be business-specific. Keep this brief short enough to preserve room for the six code chunks.

Ask Duck.ai for exactly three CSS parts followed by exactly three HTML parts in the same conversation. Require one complete fenced code block per response. Use a `css` fence for CSS and a `text` fence containing raw markup for HTML. Capture every part, verify its part label, concatenate in order, and save the assembled files in a unique working folder. Do not invent missing chunks or report partial HTML as complete.

### Reliable response capture (required)

Treat each current Duck.ai assistant response as the source of truth. A generic **Download file** action is not proof that the file belongs to the current response; it may return a previously generated attachment. Never accept a download based only on its extension, filename, or successful download notification.

For each of the six expected responses:

1. Record the current Duck.ai chat ID from the URL and the expected part (`CSS 1/3` through `HTML 3/3`). Capture only the newest assistant message in that chat, using a message-scoped locator or the visible response text. Do not read the first code block on the page, a prior assistant turn, another tab, or the page-wide clipboard.
2. Save that exact response text temporarily as `chunks/responses/<kind>-<n>.txt`. Parse exactly one fenced code block from it and require the expected fence (`css` or `text`) and an explicit matching part label. Require the first nonblank line inside the block to be exactly `/* CSS PART n OF 3 */` for CSS or `<!-- HTML PART n OF 3 -->` for HTML. Ask Duck.ai to include these standalone markers so downloaded or copied chunks can be independently identified.
3. Prefer extracting the fence from the captured current response text and writing it to `chunks/<kind>-<n>.<ext>`. If using a download or clipboard instead, compare its complete code content byte-for-byte with the code block extracted from that same response before accepting it. A changed timestamp or filename alone is insufficient.
4. Save a `chunks/capture_manifest.json` entry for each accepted part with chat ID, expected part, response capture time, code character count, and SHA-256. Reject a repeated hash, a missing/mismatched marker, more than one code fence, an empty/truncated response, or a different chat ID. Never overwrite an accepted chunk with an unchecked file.
5. Before assembly, require exactly six manifest entries in order. Assemble CSS 1+2+3 and HTML 1+2+3 only from the verified chunk files. Recheck that the concatenated files contain the expected start/end markers and that the HTML ends in `</body></html>`.

Use `duckai_automation/scripts/capture_chunk.py` to extract and validate a code fence from a saved response, then `duckai_automation/scripts/assemble_chunks.py` to verify the six chunks and assemble the working files. For a targeted repair, capture the corrected part with `--replace`; the helper archives the previous accepted part under `chunks/revisions/` before updating the manifest. If the current-response text cannot be captured, try the browser's message-scoped copy action and read that same browser session's clipboard; compare it with the response text. If it still cannot be captured or verified, ask for that exact part once more in the same chat. If the retry is still stale, incomplete, or mismatched, stop the candidate and report the capture failure. Do not proceed to the next part, render, or count that candidate as successful.

Use this output contract in each Duck.ai request:

> Follow the verified business facts and compact design brief supplied above. Build a polished, responsive website with a distinct visual identity specific to this business. Adapt the selected HTML5 UP template rather than merely naming it. Do not expose the template name or a design-reference credit in the rendered page; put the attribution in an HTML comment as specified below. Provide separate CSS and HTML files in exactly 3 CSS parts followed by exactly 3 HTML parts, one complete contiguous code block per part. Use `css` fences for CSS and `text` fences with raw HTML for HTML. Keep the HTML compatible with the CSS and link `styles.css`. Use CSS variables for the core color, typography, spacing, and container tokens. Create varied section compositions and visual rhythm; do not repeat the same card grid or center every section. Avoid generic gradients, decorative blobs, repeated icon-card grids, filler copy, and invented statistics, testimonials, credentials, or trust signals unless the design brief specifically calls for them and facts support them. Include a compelling, image-led hero and at least three distinct subject-relevant images. Include a complete, visibly designed footer section after the page content. The footer must be a real semantic `<footer>` element, not a lone copyright line: include the business or brand name, useful links to page sections, location and hours when known, contact links only when supported by the supplied or verified business information, and a copyright line. Style the footer with the page's visual system and make it work on mobile. Do not invent contact information for a real business. The final HTML part must close the document with `</body>` and `</html>`.

For each part, include the exact part label in the response and make the first nonblank line inside its code fence exactly the matching in-code marker (`/* CSS PART n OF 3 */` or `<!-- HTML PART n OF 3 -->`) so capture can reject stale output. Do not put other commentary on the marker line.

For all requests, expand “image-led hero” into a full-bleed image spanning the entire hero region edge to edge; do not confine the hero photo to one half-column. Require cover cropping and readable overlay copy on desktop and mobile. At mobile widths, require a recognizable three-bar hamburger icon instead of a visible “Menu” text label, with a keyboard-accessible control and programmatic name/state.

## Template attribution in source, not the rendered page

Never add a visible “Design reference,” “Built with,” or template-credit line/link to the public page. When an HTML5 UP template is used, retain attribution in a non-rendering HTML comment in the source, preferably near the document footer: include the template name, its source URL, the CC BY 3.0 license URL, and that the page design was adapted. Do not place this comment inside a visible element or use CSS to hide a visible credit. Keep the template name and license in the internal run notes too. If the user explicitly requires removal of all template attribution, stop using HTML5 UP and choose a source whose license permits attribution-free use; do not represent an attribution-removed HTML5 UP derivative as cleared for public release. Verify the rendered page contains no visible template-credit text or link.

For a real business, first inspect its public website and gather verified business details and image URLs. Prefer the business's own photos. If they are insufficient, use relevant photos from a reputable image library and verify the actual subject before including them. Do not treat generated alt text or filenames as proof of what an image depicts.

## Design requirements

Use the preflight design brief as implementation constraints, not just descriptive adjectives. The page should have:

- One clear focal point and CTA hierarchy, with readable type scale and line lengths.
- Keep call-to-action buttons free of arrow glyphs and arrow icons (for example, →, ➜, or chevrons). Use a clear, action-oriented text label; do not append directional decoration inside the button.
- A full-bleed, edge-to-edge photograph spanning the whole hero section, with legible overlay copy and deliberate crops at desktop and mobile sizes; do not limit the hero image to one side of a split layout.
- A consistent CSS token system for core colors, display/body fonts, spacing, and container width.
- A page sequence suited to the actual business, with varied image-led, text-led, dense, and sparse sections instead of repeated modules.
- A recognizable business-specific motif or image treatment, without unsupported claims or borrowed competitor branding.
- Mobile-specific decisions for navigation, hero crop, section order, grids, spacing, and CTA prominence; mobile should feel composed, not merely stacked.
- A standard three-bar hamburger icon for collapsed mobile navigation. Never use a visible “Menu” text label in place of the icon. Ensure the control remains keyboard accessible and has a programmatic name and state; hide the decorative bars from assistive technology.
- Purposeful imagery with varied crops/aspect ratios where appropriate, not images inserted only to fill slots.
- Preserve contact-form functionality from the source site: if the existing business website has a contact form, the redesign must include a contact form. Inspect its fields and purpose and carry over the relevant fields; do not omit it during the redesign.

Reject generic defaults unless the chosen direction calls for them: repeated three-card feature rows, excessive gradients or rounded pills, generic icon grids, decorative effects without a purpose, boilerplate marketing copy, fake metrics or testimonials, and the same composition in every section. Ask whether the page layout would still fit an unrelated business if only the name and copy changed; if so, make the direction more specific.

## Save the candidate

Keep the assembled working copy in a run folder with this structure:

```text
<working-run>/
  files/
    index.html
    styles.css
    inlined/
      index.html
  debug/
```

After all six chunks are captured, render the page, complete both the image audit and screenshot-backed desktop/mobile design audit, and save their JSON records. Then run the checker below for the final decision. You may run it earlier to diagnose structure, but an early run will remain unsuccessful until both visual audit records and screenshots are present.

```sh
python duckai_automation/scripts/check_website.py "<working-run>" "<original website request>"
```

Do not copy files to a completed-run folder unless the structural/image-byte checks pass, every image has passed the image audit, the desktop/mobile design review has screenshot evidence, and the checker returns `final_success: true`. For completed deliverables, save `index.html` and `styles.css` directly inside `completerun1/`, `completerun2/`, and so on. Do not place a redundant `files/` folder inside a completed-run folder.

## Required footer gate

The generated page must end its main content with a complete footer section. A copyright-only line fails. The footer should have a brand or business identity, at least two useful links (usually in-page navigation plus contact or directions), and accurate location, hours, contact, social, and legal information when those facts are available. Do not invent real-business details. It must have visible styling, readable contrast, keyboard-accessible links, and a responsive layout.

The checker separately requires a `<footer>` with meaningful visible text and at least two links. Also inspect the footer in the rendered page at desktop and mobile widths. If it is missing or too sparse, ask Duck.ai to revise the relevant CSS/HTML chunk, recapture the replacement chunk, reassemble, and rerun the checks. Do not silently add the footer by hand in the final files.

## Full-page visual design gate

Alongside the image-by-image audit, inspect each candidate at approximately 1440×900 desktop and 390×844 mobile, or the nearest available sizes. Capture both views and assess first-screen hierarchy and CTA, edge-to-edge hero scale and crop, section rhythm and repeated patterns, type scale and line length, color contrast, business specificity, footer, mobile navigation, overflow, and CTA visibility. Record the three highest-impact defects with screenshot evidence and a concrete correction; mark each blocking or minor. Broken or unrelated imagery, weak first-screen hierarchy, generic/repetitive composition, clipping, and unusable mobile controls are blocking defects. A candidate with any blocking defect fails, regardless of strengths elsewhere. After a targeted repair, render and inspect both viewport sizes again. If visual review cannot be completed, mark the result unverified and unsuccessful.

If headless Chromium cannot open the exact assembled local build under the workflow's normal browser settings, do not weaken browser security or use another surface to get around the block. Stop visual review for that candidate and report the exact blocked step. Ask for genuine desktop and mobile screenshots of that exact assembled build, or for a supported runtime change. User-provided screenshots may be used only if they are the actual page at the required viewports, can be placed in the run's `debug/screenshots/` folder, and are reviewed directly; until then the candidate remains unsuccessful and must not be copied to a `completerun` folder. Do not generate or infer `visual_review.json` pass evidence from source inspection alone.
## Image and visual quality gate

The checker verifies document structure, a semantic hero with an image, image bytes for HTML and CSS assets, alt attributes, at least three distinct image assets when the request calls for imagery, an accessible three-bar mobile hamburger button, and the absence of visible template-credit text or links. A successful HTTP response alone does not prove that the image is relevant or that the page composition is good.

Inspect the rendered page at desktop and mobile widths. Review the hero and every image-bearing section. Confirm the visible subject matches the section and alt text, the crop keeps the subject clear, and the photos are not misleading or needlessly repetitive. If an image is broken or weak, ask Duck.ai to replace it with a verified relevant asset, update the appropriate chunk, and rerun the checks. Do not manually patch the final HTML/CSS to disguise a failed generated run.

Record one review item for every distinct image URL in `<working-run>/debug/image_review.json`:

```json
{
  "status": "pass",
  "images": [
    {
      "url": "https://example.com/photo.jpg",
      "section": "hero",
      "observed_subject": "stainless brewing tanks",
      "relevant": true,
      "crop_ok": true,
      "alt_accurate": true
    }
  ]
}
```

Only mark the image review `pass` after checking every URL. Critique the rendered page for hierarchy, composition, typography, spacing, color roles, image use, brand specificity, conversion clarity, and mobile usability. Save actual desktop and mobile screenshots as PNG files under `<working-run>/debug/` (for example, `debug/screenshots/desktop.png` and `debug/screenshots/mobile.png`). Do not fabricate screenshots, substitute image-source tabs for page screenshots, or mark the visual review passed based only on the HTML/CSS source.

Record the screenshot-backed design audit in `<working-run>/debug/visual_review.json` using this schema:

```json
{
  "status": "pass",
  "screenshots": [
    {"viewport": "desktop", "width": 1440, "height": 900, "path": "screenshots/desktop.png"},
    {"viewport": "mobile", "width": 390, "height": 844, "path": "screenshots/mobile.png"}
  ],
  "checks": {
    "first_screen_hierarchy": true,
    "full_bleed_hero": true,
    "section_rhythm": true,
    "typography": true,
    "business_specificity": true,
    "footer": true,
    "mobile_navigation": true,
    "mobile_overflow": true,
    "cta_visibility": true
  },
  "blocking_defects": [],
  "observations": []
}
```

Screenshot paths are relative to the run's `debug/` directory and must point to readable PNG files inside that directory. Record the actual browser viewport dimensions. Set a check to `false` and list a blocking defect if it fails; do not turn a failed or uninspected view into a pass. The checker requires both screenshots, all listed checks to be true, no blocking defects, and the screenshot dimensions to match desktop/mobile layouts before `final_success` can be true. The visual review remains a human judgment; the checker verifies that its evidence and required fields exist, not that the reviewer made a correct judgment.

Fix major composition and hierarchy problems before polishing details. If any image, the footer, or a major design requirement fails, ask Duck.ai to revise the appropriate chunk, rebuild the candidate, and repeat both machine and visual checks. Preserve strong design decisions and make targeted revisions instead of restarting the whole page. Allow up to two targeted repair cycles per candidate. If a gate still fails or visual review cannot be completed, mark the candidate unsuccessful and explain why.

## Design regression batch

Run this small regression batch after changing the generation prompt, design requirements, or quality checker—not for every normal site request. Generate one candidate for each of these business types, using one candidate per test (the normal multi-version count is not needed for regression):

1. A local contractor or trades business (bold commercial direction).
2. A fictional brewery, restaurant, or hospitality business (heritage/craft or editorial direction).
3. A professional or technical B2B business (Swiss/minimal or technical direction).

For every candidate, require the structural checks, verified relevant imagery, desktop and mobile PNG evidence, accessible hamburger markup, no visible template credit, complete footer, and all visual checklist items to pass. Review the screenshots against the intended archetype and record pass/fail plus any recurring defect in the run notes. Do not call a prompt/checker change validated until all three categories have completed; stop and report if Duck.ai imposes a usage gate rather than attempting to bypass it.

## Quality standard

Report a candidate as successful only when the checker returns `final_success: true`, the footer is complete and responsive, and the rendered page has passed desktop/mobile review. A candidate that only has valid HTML/CSS or loadable image files is provisional. Keep the requested HTML5 UP template name and the quality-check result in the run notes.
