#!/usr/bin/env python3
"""Render the inlined page and automate objective desktop/mobile checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from site_quality import SECTION_ALIASES, _required_sections  # noqa: E402


SECTION_VISIBILITY_JS = r"""(required) => {
  const normalize = value => (value || '').toString().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = element => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    for (let node = element; node && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) <= 0.01) return false;
    }
    return true;
  };
  const candidates = Array.from(document.querySelectorAll('main,section,footer,header,article,aside,form,[role="region"],[id],[class]'));
  const results = {};
  for (const [name, aliases] of Object.entries(required)) {
    let matches = [];
    if (name === 'hero') {
      const h1 = document.querySelector('h1');
      const target = h1 && (h1.closest('section') || h1.closest('main') || h1.parentElement);
      if (target) matches = [target];
    } else if (name === 'footer') {
      matches = Array.from(document.querySelectorAll('footer'));
    }
    if (!matches.length) {
      matches = candidates.filter(element => {
        const headings = Array.from(element.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(node => node.innerText || node.textContent || '').join(' ');
        const evidence = normalize([element.id, element.className, headings].join(' '));
        return aliases.some(alias => evidence.includes(normalize(alias)));
      });
    }
    const usable = matches.filter(visible).sort((a, b) => {
      const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
      return (ar.width * ar.height) - (br.width * br.height);
    });
    const element = usable[0] || matches[0];
    const rect = element ? element.getBoundingClientRect() : null;
    results[name] = {
      visible: usable.length > 0,
      selector_hint: element ? `${element.tagName.toLowerCase()}${element.id ? '#' + element.id : ''}${element.className && typeof element.className === 'string' ? '.' + element.className.trim().split(/\s+/).slice(0, 2).join('.') : ''}` : null,
      width: rect ? Math.round(rect.width) : 0,
      height: rect ? Math.round(rect.height) : 0
    };
  }
  return results;
}"""


VIEWPORT_CHECKS_JS = r"""() => {
  const visible = element => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    for (let node = element; node && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) <= 0.01) return false;
    }
    return true;
  };
  const h1 = document.querySelector('h1');
  const hero = h1 && (h1.closest('section') || h1.closest('[class*="hero" i]') || h1.closest('main'));
  let heroData = {present: false, spans_viewport: false, media_covers_hero: false};
  if (hero) {
    const hr = hero.getBoundingClientRect();
    const nodes = [hero, ...hero.querySelectorAll('*')];
    const media = nodes.filter(node => {
      const style = getComputedStyle(node);
      const pseudoHasImage = ['::before', '::after'].some(pseudo => getComputedStyle(node, pseudo).backgroundImage !== 'none');
      return (node.tagName.toLowerCase() === 'img' || style.backgroundImage !== 'none' || pseudoHasImage) && visible(node);
    }).map(node => ({
      rect: node.getBoundingClientRect(),
      image: node.tagName.toLowerCase() === 'img' ? node.naturalWidth > 0 : true
    })).filter(item => item.image);
    const covers = media.some(item => item.rect.left <= hr.left + 2 && item.rect.right >= hr.right - 2 && item.rect.top <= hr.top + 2 && item.rect.bottom >= hr.bottom - 2);
    heroData = {
      present: true,
      left: Math.round(hr.left),
      right: Math.round(hr.right),
      width: Math.round(hr.width),
      height: Math.round(hr.height),
      viewport_width: window.innerWidth,
      spans_viewport: hr.left <= 2 && hr.right >= window.innerWidth - 2,
      media_covers_hero: covers
    };
  }

  const ctaWords = /ask|contact|book|schedule|inquir|reserve|visit|get in touch/i;
  const actions = Array.from(document.querySelectorAll('a,button,[role="button"],input[type="submit"]'));
  const cta = actions.find(element => {
    const label = [element.innerText, element.getAttribute('aria-label'), element.getAttribute('value'), element.getAttribute('href')].filter(Boolean).join(' ');
    if (!ctaWords.test(label) || !visible(element)) return false;
    const rect = element.getBoundingClientRect();
    return rect.top >= 0 && rect.bottom <= window.innerHeight && rect.left >= 0 && rect.right <= window.innerWidth;
  });
  const ctaRect = cta ? cta.getBoundingClientRect() : null;
  const images = Array.from(document.images);
  images.forEach(image => { image.loading = 'eager'; });
  const imageResults = images.map(image => ({
    src: image.currentSrc || image.src || '',
    loaded: image.complete && image.naturalWidth > 0,
    natural_width: image.naturalWidth
  }));
  const root = document.documentElement;
  const body = document.body;
  const documentWidth = Math.max(root.scrollWidth, body ? body.scrollWidth : 0);
  return {
    hero: heroData,
    cta: {visible_first_screen: Boolean(cta), text: cta ? (cta.innerText || cta.getAttribute('aria-label') || '') : '', bottom: ctaRect ? Math.round(ctaRect.bottom) : null},
    horizontal_overflow: {scroll_width: documentWidth, viewport_width: root.clientWidth, overflow: documentWidth > root.clientWidth + 1},
    images: {count: images.length, loaded_count: imageResults.filter(item => item.loaded).length, failures: imageResults.filter(item => !item.loaded)},
    fonts: {status: document.fonts.status, loaded_face_count: Array.from(document.fonts).filter(face => face.status === 'loaded').length, failed_face_count: Array.from(document.fonts).filter(face => face.status === 'error').length}
  };
}"""


MENU_BUTTON_JS = r"""button => {
  const controls = button.getAttribute('aria-controls');
  const nav = controls && document.getElementById(controls);
  return Boolean(controls && nav && button.getAttribute('aria-expanded') !== null && button.querySelectorAll('span').length === 3);
}"""


def _resource_event(event, collection: list[dict]) -> None:
    request = event.request if hasattr(event, "request") else event
    if request.resource_type not in {"image", "font"}:
        return
    if hasattr(event, "status"):
        if event.status < 400:
            return
        collection.append({"type": request.resource_type, "status": event.status, "url": request.url})
    else:
        collection.append({"type": request.resource_type, "error": request.failure, "url": request.url})


def _nav_visible(page, nav_id: str) -> bool:
    return bool(page.evaluate(
        """id => {
          const nav = document.getElementById(id);
          if (!nav) return false;
          const style = getComputedStyle(nav), rect = nav.getBoundingClientRect();
          return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0.01 && rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.left < innerWidth;
        }""",
        nav_id,
    ))


def _exercise_mobile_menu(page) -> dict:
    buttons = page.locator("button").all()
    button = next((item for item in buttons if item.evaluate(MENU_BUTTON_JS)), None)
    if button is None:
        return {"found": False, "click_opens": False, "click_closes": False, "tab_reachable": False, "enter_opens": False, "space_toggles": False, "escape_closes": False}

    nav_id = button.get_attribute("aria-controls")
    result = {"found": True}
    try:
        if button.get_attribute("aria-expanded") == "true":
            button.click()
        button.click()
        page.wait_for_timeout(150)
        result["click_opens"] = button.get_attribute("aria-expanded") == "true" and _nav_visible(page, nav_id)
        button.click()
        page.wait_for_timeout(150)
        result["click_closes"] = button.get_attribute("aria-expanded") == "false" and not _nav_visible(page, nav_id)

        page.evaluate("document.activeElement && document.activeElement.blur()")
        result["tab_reachable"] = False
        for _ in range(20):
            page.keyboard.press("Tab")
            if button.evaluate("element => element === document.activeElement"):
                result["tab_reachable"] = True
                break
        if result["tab_reachable"]:
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)
            result["enter_opens"] = button.get_attribute("aria-expanded") == "true" and _nav_visible(page, nav_id)
            page.keyboard.press("Space")
            page.wait_for_timeout(150)
            result["space_toggles"] = button.get_attribute("aria-expanded") == "false" and not _nav_visible(page, nav_id)
            if result["space_toggles"]:
                page.keyboard.press("Enter")
                page.wait_for_timeout(150)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            result["escape_closes"] = button.get_attribute("aria-expanded") == "false" and not _nav_visible(page, nav_id)
        else:
            result["enter_opens"] = False
            result["space_toggles"] = False
            result["escape_closes"] = False
    except Exception as error:
        result["error"] = str(error)
        result.setdefault("click_opens", False)
        result.setdefault("click_closes", False)
        result.setdefault("tab_reachable", False)
        result.setdefault("enter_opens", False)
        result.setdefault("space_toggles", False)
        result.setdefault("escape_closes", False)
    result["pass"] = all(result.get(key, False) for key in ("click_opens", "click_closes", "tab_reachable", "enter_opens", "space_toggles"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path)
    parser.add_argument("request", help="Original website request used to determine required sections")
    args = parser.parse_args()

    run_dir = args.run_folder.resolve()
    html_file = run_dir / "files" / "inlined" / "index.html"
    debug_dir = run_dir / "debug"
    screenshots_dir = debug_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    requested_sections = _required_sections(args.request)
    section_aliases = {name: list(SECTION_ALIASES.get(name, (name,))) for name in requested_sections}
    viewports = {"desktop": (1440, 900), "mobile": (390, 844)}
    report = {
        "status": "fail",
        "tested_file": str(html_file.relative_to(run_dir).as_posix()),
        "required_sections": requested_sections,
        "viewports": {},
        "mobile_menu": {},
        "checks": {},
        "resource_errors": [],
    }
    if not html_file.is_file():
        report["error"] = "inlined HTML file is missing"
    else:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for name, (width, height) in viewports.items():
                    page = browser.new_page(viewport={"width": width, "height": height})
                    page_errors: list[dict] = []
                    page.on("requestfailed", lambda request, dest=page_errors: _resource_event(request, dest))
                    page.on("response", lambda response, dest=page_errors: _resource_event(response, dest))
                    try:
                        page.goto(html_file.as_uri(), wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(800)
                        resource_state = page.evaluate(r"""async () => {
                          const timeout = ms => new Promise(resolve => setTimeout(resolve, ms));
                          const images = Array.from(document.images);
                          images.forEach(image => { image.loading = 'eager'; });
                          await Promise.race([
                            Promise.all(images.map(image => image.decode().catch(() => null))),
                            timeout(12000)
                          ]);
                          await Promise.race([document.fonts.ready, timeout(8000)]);
                          return {
                            image_count: images.length,
                            loaded_images: images.filter(image => image.complete && image.naturalWidth > 0).length,
                            failed_images: images.filter(image => image.complete && image.naturalWidth === 0).map(image => image.currentSrc || image.src),
                            font_status: document.fonts.status,
                            loaded_faces: Array.from(document.fonts).filter(face => face.status === 'loaded').length,
                            failed_faces: Array.from(document.fonts).filter(face => face.status === 'error').length
                          };
                        }""")
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(150)
                        page.screenshot(path=str(screenshots_dir / f"{name}.png"), full_page=False)
                        sections = page.evaluate(SECTION_VISIBILITY_JS, section_aliases)
                        visual = page.evaluate(VIEWPORT_CHECKS_JS)
                        font_errors = [item for item in page_errors if item["type"] == "font"]
                        image_errors = [item for item in page_errors if item["type"] == "image"]
                        required_present = all(sections.get(section, {}).get("visible", False) for section in requested_sections)
                        report["viewports"][name] = {
                            "width": width,
                            "height": height,
                            "sections": sections,
                            "all_required_sections_visible": required_present,
                            "hero": visual["hero"],
                            "hero_spans_viewport": bool(visual["hero"].get("spans_viewport")),
                            "cta": visual["cta"],
                            "cta_visible_first_screen": visual["cta"]["visible_first_screen"],
                            "horizontal_overflow": visual["horizontal_overflow"],
                            "no_horizontal_overflow": not visual["horizontal_overflow"]["overflow"],
                            "images": resource_state,
                            "images_loaded": (
                                resource_state["loaded_images"] == resource_state["image_count"]
                                and not resource_state["failed_images"]
                                and not image_errors
                            ),
                            "fonts": resource_state,
                            "fonts_loaded": (
                                resource_state["font_status"] == "loaded"
                                and resource_state["failed_faces"] == 0
                                and not font_errors
                            ),
                            "resource_errors": page_errors,
                            "screenshot": f"screenshots/{name}.png",
                        }
                        report["resource_errors"].extend(page_errors)
                        if name == "mobile":
                            report["mobile_menu"] = _exercise_mobile_menu(page)
                    except Exception as error:
                        report["viewports"][name] = {
                            "width": width,
                            "height": height,
                            "error": str(error),
                            "all_required_sections_visible": False,
                            "hero_spans_viewport": False,
                            "cta_visible_first_screen": False,
                            "no_horizontal_overflow": False,
                            "images_loaded": False,
                            "fonts_loaded": False,
                            "screenshot": f"screenshots/{name}.png",
                        }
                    finally:
                        page.close()
            finally:
                browser.close()

        desktop = report["viewports"].get("desktop", {})
        mobile = report["viewports"].get("mobile", {})
        mobile_menu = report["mobile_menu"]
        report["checks"] = {
            "rendered_sections_visible_desktop_and_mobile": bool(desktop.get("all_required_sections_visible") and mobile.get("all_required_sections_visible")),
            "hero_spans_viewport_desktop_and_mobile": bool(desktop.get("hero_spans_viewport") and mobile.get("hero_spans_viewport")),
            "cta_visible_first_screen_desktop_and_mobile": bool(desktop.get("cta_visible_first_screen") and mobile.get("cta_visible_first_screen")),
            "mobile_menu_clicks_work": bool(mobile_menu.get("click_opens") and mobile_menu.get("click_closes")),
            "mobile_menu_keyboard_works": bool(mobile_menu.get("tab_reachable") and mobile_menu.get("enter_opens") and mobile_menu.get("space_toggles")),
            "mobile_has_no_horizontal_overflow": bool(mobile.get("no_horizontal_overflow")),
            "fonts_load_in_browser": bool(desktop.get("fonts_loaded") and mobile.get("fonts_loaded")),
            "images_load_in_browser": bool(desktop.get("images_loaded") and mobile.get("images_loaded")),
        }
        report["status"] = "pass" if all(report["checks"].values()) else "fail"

    output = debug_dir / "browser_qa.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
