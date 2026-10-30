"""Lightweight completeness checks for assembled CSS/HTML website responses."""

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import html as html_module
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path


SECTION_ALIASES = {
    "hero": ("hero", "banner", "welcome", "introduction", "intro", "site-header", "masthead"),
    "contact": ("contact", "booking", "book", "appointment", "get in touch", "form"),
    "footer": ("footer",),
    "services": ("services", "service", "repairs", "repair", "offerings", "experiences", "attractions"),
    "process": ("process", "how it works", "steps", "what to expect"),
    "testimonials": ("testimonials", "reviews", "customer stories", "guest stories", "player stories"),
    "location": ("location", "directions", "find us", "address", "visit"),
    "hours": ("hours", "opening times", "opening hours", "business hours", "shop hours", "when we're open"),
    "about": ("about", "our story", "who we are", "mission"),
    "faq": ("faq", "frequently asked", "questions"),
    "pricing": ("pricing", "prices", "cost", "rates"),
    "team": ("team", "staff", "our people"),
    "gallery": ("gallery", "portfolio", "our work", "photos"),
}
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
CTA_ARROW_GLYPHS = re.compile(r"[←↑→↓↗↘↙↖↩↪➔➜➝➞➟➠➡➢➤›»]")


def _has_alias(value, aliases):
    value = (value or "").lower()
    return any(
        re.search(rf"(?:^|[-_]){re.escape(alias)}(?:$|[-_])", value)
        for alias in aliases
    )


def _hero_has_css_background_image(css, hero_selectors):
    if not css or not hero_selectors:
        return False
    css = re.sub(r"/\*[\s\S]*?\*/", " ", css)

    image_variables = {
        name
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+)", css)
        if re.search(r"\b(?:url|image-set)\s*\(", value, re.IGNORECASE)
    }
    image_value = re.compile(r"\b(?:url|image-set)\s*\(", re.IGNORECASE)
    variable_value = re.compile(r"var\(\s*(--[\w-]+)", re.IGNORECASE)
    for selector_group, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css, re.DOTALL):
        selector_tokens = [
            token
            for prefix, token in hero_selectors
            if re.search(
                rf"(?<![\w-]){re.escape(prefix + token)}(?![\w-])",
                selector_group,
            )
        ]
        if not selector_tokens:
            continue
        for declaration in re.finditer(
            r"(?:^|;)\s*background(?:-image)?\s*:\s*([^;]+)",
            declarations,
            re.IGNORECASE,
        ):
            value = declaration.group(1)
            if image_value.search(value):
                return True
            if any(name in image_variables for name in variable_value.findall(value)):
                return True
    return False


def _urls_from_srcset(value):
    """Return URL tokens from a srcset attribute, ignoring density descriptors."""
    urls = []
    for candidate in (value or "").split(","):
        fields = candidate.strip().split()
        if fields:
            urls.append(fields[0])
    return urls


def _image_urls_from_css(css, hero_selectors):
    """Collect background-image URLs declared on hero selectors in the CSS."""
    urls = []
    if not css or not hero_selectors:
        return urls
    css = re.sub(r"/\*[\s\S]*?\*/", " ", css)
    image_value = re.compile(r"\b(?:url|image-set)\s*\(\s*([^)\s]+)\s*\)", re.IGNORECASE)
    variable_values = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+)", css))
    hero_declarations = []
    for selector_group, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css, re.DOTALL):
        selector_tokens = [
            token
            for prefix, token in hero_selectors
            if re.search(
                rf"(?<![\w-]){re.escape(prefix + token)}(?![\w-])",
                selector_group,
            )
        ]
        if not selector_tokens:
            continue
        hero_declarations.extend(re.findall(
            r"(?:^|;)\s*background(?:-image)?\s*:\s*([^;]+)",
            declarations,
            re.IGNORECASE,
        ))
    for value in hero_declarations:
        urls.extend(match.group(1).strip("'\"") for match in image_value.finditer(value))
        for variable in re.findall(r"var\(\s*(--[\w-]+)", value, re.IGNORECASE):
            urls.extend(
                match.group(1).strip("'\"")
                for match in image_value.finditer(variable_values.get(variable, ""))
            )
    return urls


def _all_css_image_urls(css):
    """Collect image URLs from CSS image-bearing properties, excluding font URLs."""
    urls = []
    if not css:
        return urls
    css = re.sub(r"/\*[\s\S]*?\*/", " ", css)
    image_property = re.compile(
        r"(?:^|;)\s*(?:background(?:-image)?|border-image(?:-source)?|"
        r"list-style-image|mask(?:-image)?|content)\s*:\s*([^;]+)",
        re.IGNORECASE,
    )
    image_url = re.compile(r"\burl\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
    for _selector_group, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css, re.DOTALL):
        for match in image_property.finditer(declarations):
            urls.extend(url_match.group(2).strip() for url_match in image_url.finditer(match.group(1)))
    return [url for url in urls if url]


def _looks_like_image(data, content_type=""):
    """Accept recognized image data and reject HTML, JSON, or MIME-only claims."""
    if not data:
        return False
    sample = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if sample.startswith((b"<!doctype html", b"<html", b"{", b"[")):
        return False
    if (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) >= 24
        and int.from_bytes(data[16:20], "big") > 0
        and int.from_bytes(data[20:24], "big") > 0
    ):
        return True
    if data.startswith(b"\xff\xd8\xff") and len(data) >= 4:
        return True
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 14:
        return True
    if (data.startswith(b"BM") and len(data) >= 26) or (
        data.startswith((b"II*\x00", b"MM\x00*")) and len(data) >= 8
    ):
        return True
    if data.startswith(b"\x00\x00\x01\x00") and len(data) >= 22:
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        return True
    if len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in {
        b"avif", b"avis", b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"jxl "
    }:
        return True
    if re.match(
        rb"^(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<svg(?:\s|>)",
        sample,
        re.DOTALL,
    ):
        return True
    return False


def _image_loads_ok(url, base_url=None, timeout=8):
    """Return True only if a referenced image contains plausible image bytes."""
    try:
        url = (url or "").strip().strip("'\"")
        if url.startswith("data:"):
            header, payload = url.split(",", 1)
            if ";base64" in header.lower():
                data = base64.b64decode(payload, validate=True)
            else:
                data = urllib.parse.unquote_to_bytes(payload)
            return _looks_like_image(data, header[5:].split(";", 1)[0])
        if base_url and not urllib.parse.urlparse(url).scheme:
            url = urllib.parse.urljoin(base_url, url)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.getcode()
            if not 200 <= status < 300:
                return False
            return _looks_like_image(
                response.read(65536), response.headers.get("Content-Type", "")
            )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, binascii.Error):
        return False


def _hero_images_load_ok(urls, base_url=None):
    """Return True only when all image references in the hero resolve to images."""
    # Deduplicate while preserving order.
    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    if not unique_urls:
        return False
    return all(_image_loads_ok(url, base_url=base_url) for url in unique_urls)


def _unique_image_urls(urls, base_url=None):
    unique = []
    seen = set()
    for url in urls:
        clean = html_module.unescape((url or "").strip().strip("'\""))
        if not clean:
            continue
        key = _url_key(clean, base_url) or clean
        if key not in seen:
            seen.add(key)
            unique.append(clean)
    return unique


def _failed_image_urls(urls, base_url=None):
    unique_urls = _unique_image_urls(urls, base_url)
    if not unique_urls:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(unique_urls))) as pool:
        results = list(pool.map(lambda url: _image_loads_ok(url, base_url=base_url), unique_urls))
    return [url for url, loaded in zip(unique_urls, results) if not loaded]


def _visual_review_result(session_dir, image_urls, base_url=None):
    """Require a per-asset Hermes visual audit before a machine-passing run is final."""
    review_path = Path(session_dir) / "debug" / "image_review.json"
    if not review_path.is_file():
        return "pending", list(image_urls), []
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", list(image_urls), ["image_review.json is unreadable"]
    if not isinstance(review, dict):
        return "invalid", list(image_urls), ["image_review.json must contain an object"]

    expected = {_url_key(url, base_url) or url for url in image_urls}
    reviewed = set()
    issues = []
    for item in review.get("images", []):
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        key = _url_key(url, base_url) or html_module.unescape(str(url).strip())
        if key in expected:
            reviewed.add(key)
            for field in ("relevant", "crop_ok", "alt_accurate"):
                if item.get(field) is not True:
                    issues.append(f"{url}: {field} did not pass")
            if not item.get("observed_subject"):
                issues.append(f"{url}: observed_subject is missing")
    missing = sorted(expected - reviewed)
    if review.get("status") == "pass" and not missing and not issues:
        return "pass", [], []
    if review.get("status") == "fail" or issues:
        return "fail", missing, issues
    return "pending", missing, issues


def _visual_design_review_result(session_dir):
    """Require desktop/mobile screenshot evidence and a clean design checklist."""
    review_path = Path(session_dir) / "debug" / "visual_review.json"
    if not review_path.is_file():
        return "pending", ["debug/visual_review.json is missing"], []
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", [], ["visual_review.json is unreadable"]
    if not isinstance(review, dict):
        return "invalid", [], ["visual_review.json must contain an object"]

    required_checks = (
        "first_screen_hierarchy", "section_rhythm", "typography",
        "business_specificity", "footer",
    )
    issues = []
    if review.get("status") != "pass":
        issues.append("visual review status is not pass")
    checks = review.get("checks")
    if not isinstance(checks, dict):
        issues.append("checks must be an object")
        checks = {}
    for name in required_checks:
        if checks.get(name) is not True:
            issues.append(f"design check did not pass: {name}")

    blocking = review.get("blocking_defects")
    if not isinstance(blocking, list):
        issues.append("blocking_defects must be a list")
    elif blocking:
        issues.extend(f"unresolved blocking defect: {item}" for item in blocking)

    debug_dir = Path(session_dir) / "debug"
    screenshots = review.get("screenshots")
    found = {}
    if not isinstance(screenshots, list):
        return "fail", [], issues + ["screenshots must be a list"]
    for item in screenshots:
        if not isinstance(item, dict) or item.get("viewport") not in {"desktop", "mobile"}:
            continue
        viewport = item["viewport"]
        relative = item.get("path")
        if not isinstance(relative, str) or not relative.strip():
            issues.append(f"{viewport} screenshot path is missing")
            continue
        candidate = (debug_dir / relative).resolve()
        try:
            candidate.relative_to(debug_dir.resolve())
        except ValueError:
            issues.append(f"{viewport} screenshot path must stay inside debug/")
            continue
        if not candidate.is_file():
            issues.append(f"{viewport} screenshot file is missing: {relative}")
            continue
        try:
            data = candidate.read_bytes()
        except OSError:
            issues.append(f"{viewport} screenshot could not be read: {relative}")
            continue
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
            issues.append(f"{viewport} screenshot must be a valid PNG: {relative}")
            continue
        pixel_width = int.from_bytes(data[16:20], "big")
        pixel_height = int.from_bytes(data[20:24], "big")
        declared_width = item.get("width")
        declared_height = item.get("height")
        if not isinstance(declared_width, int) or not isinstance(declared_height, int):
            issues.append(f"{viewport} viewport width and height are required")
            continue
        if viewport == "desktop":
            valid_viewport = declared_width >= 1024 and declared_height >= 700
            valid_pixels = pixel_width >= 1024 and pixel_height >= 700 and pixel_width > pixel_height
        else:
            valid_viewport = 320 <= declared_width <= 600 and declared_height >= 600
            valid_pixels = pixel_width <= 1200 and pixel_height >= 600 and pixel_width < pixel_height
        if not valid_viewport or not valid_pixels:
            issues.append(f"{viewport} screenshot dimensions do not match the required viewport")
            continue
        found[viewport] = str(candidate)

    missing = [f"{viewport} screenshot evidence is missing or invalid" for viewport in ("desktop", "mobile") if viewport not in found]
    if issues:
        return "fail", missing, issues
    if missing:
        return "pending", missing, []
    return "pass", [], []


def _browser_qa_result(session_dir):
    """Read objective rendered-page checks produced by browser_qa.py."""
    report_path = Path(session_dir) / "debug" / "browser_qa.json"
    if not report_path.is_file():
        return "missing", {}, {"error": "debug/browser_qa.json is missing"}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", {}, {"error": "browser_qa.json is unreadable"}
    if not isinstance(report, dict) or not isinstance(report.get("checks"), dict):
        return "invalid", {}, {"error": "browser_qa.json must contain a checks object"}
    checks = report["checks"]
    expected = (
        "rendered_sections_visible_desktop_and_mobile",
        "hero_spans_viewport_desktop_and_mobile",
        "cta_visible_first_screen_desktop_and_mobile",
        "mobile_menu_clicks_work",
        "mobile_menu_keyboard_works",
        "mobile_has_no_horizontal_overflow",
        "fonts_load_in_browser",
        "images_load_in_browser",
    )
    if any(not isinstance(checks.get(name), bool) for name in expected):
        return "invalid", {}, {"error": "browser_qa.json is missing one or more boolean checks"}
    status = "pass" if report.get("status") == "pass" and all(checks[name] for name in expected) else "fail"
    return status, checks, report


def _url_key(url, base_url=None):
    url = html_module.unescape((url or "").strip().strip("'\""))
    if base_url and not urllib.parse.urlparse(url).scheme:
        url = urllib.parse.urljoin(base_url, url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return (parsed.netloc.lower(), urllib.parse.unquote(parsed.path))


def _source_image_urls_from_prompt(prompt):
    marker = re.search(r"VERIFIED_SOURCE_IMAGE_URLS:\s*\n([\s\S]*?)(?:\n\s*\n|$)", prompt)
    if not marker:
        return []
    urls = []
    for line in marker.group(1).splitlines():
        match = re.match(r"\s*-\s*(https?://\S+)", line)
        if match:
            urls.append(html_module.unescape(match.group(1)))
    return urls


class _PageInventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = set()
        self.ids = []
        self.classes = []
        self.headings = []
        self.text = []
        self.visible_text = []
        self.visible_links = []
        self._hidden_elements = []
        self._heading = None
        self.footer_text = []
        self.footer_links = []
        self._in_footer = 0
        self._buttons = []
        self._button_stack = []
        self.cta_arrow_issues = []
        self._cta_stack = []
        self.forms = 0
        self.hero_image_present = False
        self.hero_image_urls = []
        self.all_image_urls = []
        self.images_missing_alt = False
        self.hero_selectors = set()
        self._hero_stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.tags.add(tag)
        values = dict(attrs)
        class_names = (values.get("class") or "").lower()
        is_cta = tag == "button" or (
            tag == "a"
            and (
                (values.get("role") or "").lower() == "button"
                or values.get("href", "").lower().startswith(("tel:", "mailto:"))
                or re.search(r"(?:^|[\s_-])(?:button|btn|cta|call[-_]?to[-_]?action)(?:$|[\s_-])", class_names)
            )
        )
        if is_cta:
            label = " ".join((values.get("aria-label") or "", values.get("title") or "")).strip()
            self._cta_stack.append({"tag": tag, "text": [label] if label else [], "arrow_icon": False})
        icon_marker = re.search(r"(?:arrow|chevron)", " ".join(
            values.get(key, "") for key in ("class", "data-icon", "data-lucide", "aria-label")
        ), re.IGNORECASE)
        if icon_marker and tag in {"svg", "i", "span", "use"}:
            for cta in self._cta_stack:
                cta["arrow_icon"] = True
        if tag in {"head", "script", "style", "template"}:
            self._hidden_elements.append(tag)
        if tag == "button":
            self._button_stack.append({
                "label": (values.get("aria-label") or "").strip(),
                "labelledby": (values.get("aria-labelledby") or "").strip(),
                "expanded": (values.get("aria-expanded") or "").strip().lower(),
                "controls": (values.get("aria-controls") or "").strip(),
                "classes": (values.get("class") or "").lower(),
                "bars": 0,
                "text": [],
            })
        elif tag == "span":
            # Ignore a decorative wrapper such as <span class="menu-toggle__bars">
            # and count its three visual bar children instead.
            if "bars" not in (values.get("class") or "").lower():
                for control in self._button_stack:
                    control["bars"] += 1
        if not self._hidden_elements and tag == "a" and values.get("href"):
            self.visible_links.append(values["href"].strip())
        if tag == "footer":
            self._in_footer += 1
        if self._in_footer and tag == "a" and values.get("href"):
            self.footer_links.append(values["href"].strip())
        is_image_source = (
            tag == "img"
            or tag == "source"
            and (
                (values.get("type") or "").lower().startswith("image/")
                or bool(values.get("srcset") or values.get("data-srcset"))
            )
        )
        if tag == "img" and "alt" not in values:
            self.images_missing_alt = True
        if is_image_source:
            for attribute in ("src", "data-src", "data-lazy-src", "data-original", "data-image-url"):
                value = (values.get(attribute) or "").strip()
                if value:
                    self.all_image_urls.append(value)
            for attribute in ("srcset", "data-srcset"):
                self.all_image_urls.extend(_urls_from_srcset(values.get(attribute, "")))
        if tag == "video" and values.get("poster", "").strip():
            self.all_image_urls.append(values["poster"].strip())
        if values.get("id"):
            self.ids.append(values["id"])
        if values.get("class"):
            self.classes.extend(values["class"].split())
        if tag == "form":
            self.forms += 1
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = [""]

        parent_is_hero = self._hero_stack[-1][1] if self._hero_stack else False
        hero_aliases = SECTION_ALIASES["hero"]
        node_is_hero = parent_is_hero or _has_alias(values.get("id"), hero_aliases)
        node_is_hero = node_is_hero or any(
            _has_alias(class_name, hero_aliases)
            for class_name in values.get("class", "").split()
        )
        if node_is_hero:
            if values.get("id"):
                self.hero_selectors.add(("#", values["id"]))
            for class_name in values.get("class", "").split():
                self.hero_selectors.add((".", class_name))

            image_sources = (
                values.get("src"), values.get("data-src"), values.get("data-lazy-src"),
            )
            if tag == "img" and (
                any(source and source.strip() for source in image_sources)
                or values.get("srcset", "").strip()
                or values.get("data-srcset", "").strip()
            ):
                self.hero_image_present = True
                self.hero_image_urls.extend(
                    source.strip() for source in image_sources if source and source.strip()
                )
            if tag in {"img", "source"}:
                for attribute in ("srcset", "data-srcset"):
                    self.hero_image_urls.extend(_urls_from_srcset(values.get(attribute, "")))
                if tag == "source" and values.get("src", "").strip():
                    self.hero_image_urls.append(values["src"].strip())
                if tag == "source" and values.get("srcset", "").strip():
                    self.hero_image_present = True
            elif tag == "image" and any(
                values.get(attribute, "").strip()
                for attribute in ("href", "xlink:href")
            ):
                self.hero_image_present = True
                self.hero_image_urls.extend(
                    values.get(name, "").strip()
                    for name in ("href", "xlink:href")
                    if values.get(name, "").strip()
                )
            style = values.get("style", "")
            if re.search(r"background(?:-image)?\s*:[^;]*(?:url|image-set)\s*\(", style, re.IGNORECASE):
                self.hero_image_urls.extend(
                    match.group(1).strip("'\"")
                    for match in re.finditer(
                        r"(?:url|image-set)\s*\(\s*['\"]?([^)'\"]+)",
                        style,
                        re.IGNORECASE,
                    )
                )
            if tag == "video" and values.get("poster", "").strip():
                self.hero_image_present = True
                self.hero_image_urls.append(values["poster"].strip())

            if re.search(
                r"\bbackground(?:-image)?\s*:[^;]*(?:url|image-set)\s*\(",
                values.get("style", ""),
                re.IGNORECASE,
            ):
                self.hero_image_present = True

        if tag not in VOID_ELEMENTS:
            self._hero_stack.append((tag, node_is_hero))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "button" and self._button_stack:
            self._buttons.append(self._button_stack.pop())
        for index in range(len(self._cta_stack) - 1, -1, -1):
            if self._cta_stack[index]["tag"] == tag:
                cta = self._cta_stack.pop(index)
                label = " ".join(cta["text"])
                if CTA_ARROW_GLYPHS.search(label) or cta["arrow_icon"]:
                    self.cta_arrow_issues.append(label or "CTA contains an arrow icon")
                break
        if tag in self._hidden_elements:
            index = len(self._hidden_elements) - 1 - self._hidden_elements[::-1].index(tag)
            del self._hidden_elements[index]
        if tag == "footer" and self._in_footer:
            self._in_footer -= 1
        for index in range(len(self._hero_stack) - 1, -1, -1):
            if self._hero_stack[index][0] == tag:
                del self._hero_stack[index:]
                break
        if self._heading is not None and re.fullmatch(r"h[1-6]", tag.lower()):
            self.headings.append(" ".join(self._heading))
            self._heading = None

    def handle_data(self, data):
        if self._hidden_elements:
            return
        if data.strip():
            clean = data.strip()
            self.text.append(clean)
            self.visible_text.append(clean)
            for control in self._button_stack:
                control["text"].append(clean)
            for cta in self._cta_stack:
                cta["text"].append(clean)
            if self._in_footer:
                self.footer_text.append(clean)
        if self._heading is not None:
            self._heading.append(data.strip())

    def has_accessible_mobile_hamburger(self):
        known_ids = set(self.ids)
        for control in self._buttons:
            has_accessible_name = bool(control["label"] or control["labelledby"])
            controls_navigation = bool(control["controls"] and control["controls"] in known_ids)
            has_three_bars = control["bars"] == 3
            has_menu_class = bool(re.search(r"(?:^|[\s_-])(?:menu|nav)(?:$|[\s_-])", control["classes"]))
            is_expanded_control = control["expanded"] in {"true", "false"}
            has_visible_menu_label = bool(re.search(r"\bmenu\b", " ".join(control["text"]), re.IGNORECASE))
            if (has_accessible_name and controls_navigation and has_three_bars
                    and has_menu_class and is_expanded_control and not has_visible_menu_label):
                return True
        return False


def _required_sections(prompt):
    # The scraped source audit is factual input, not a list of requested sections.
    # Infer requirements only from the actual redesign request before the audit block.
    request_text = re.split(
        r"\n\s*(?:VERIFIED SOURCE SITE AUDIT|The runner could not fetch the public source homepage)",
        prompt,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    request_text = re.sub(
        r"\b(?:avoid|do\s+not|don't|never)\s+"
        r"(?:invent(?:ing)?|include|add|create|feature|use|request)\b[^.!?\n]*[.!?]?",
        " ",
        request_text,
        flags=re.IGNORECASE,
    )
    text = request_text.lower()
    required = {"hero", "contact", "footer"}
    trigger_terms = {
        "services": r"\b(?:services?|repairs?|offerings?|experiences?|attractions?)\b",
        "process": r"\b(?:process|steps?|how it works|what to expect)\b",
        "testimonials": r"\b(?:testimonials?|reviews?|customer stories|guest stories|player stories)\b",
        "location": r"\b(?:location|directions|find us|address|visit)\b",
        "hours": r"\b(?:hours|opening times|business hours)\b",
        "about": r"\babout(?:\s+(?:us|section|page|the\s+(?:business|company|brand|shop|team)))\b|\bour\s+story\b|\bmission\b",
        "faq": r"\b(?:faq|frequently asked questions)\b",
        "pricing": r"\b(?:pricing|prices|cost|rates)\b",
        "team": r"\b(?:team|staff)\b",
        "gallery": r"\b(?:gallery|portfolio|photos)\b",
    }
    for section, pattern in trigger_terms.items():
        if re.search(pattern, text, re.IGNORECASE):
            required.add(section)
    return sorted(required)


def assess_website(prompt, session_dir):
    """Assess whether assembled files are structurally complete for the request."""
    session_dir = Path(session_dir)
    files_dir = session_dir / "files"
    html_path = files_dir / "index.html" if (files_dir / "index.html").is_file() else session_dir / "index.html"
    css_path = files_dir / "styles.css" if (files_dir / "styles.css").is_file() else session_dir / "styles.css"
    html = html_path.read_text(encoding="utf-8", errors="replace") if html_path.is_file() else ""
    css = css_path.read_text(encoding="utf-8", errors="replace") if css_path.is_file() else ""

    parser = _PageInventory()
    parse_error = None
    if html:
        try:
            parser.feed(html)
            parser.close()
        except Exception as error:  # HTMLParser is tolerant; retain diagnostics if malformed.
            parse_error = str(error)

    evidence = " ".join(parser.ids + parser.classes + parser.headings).lower()
    page_text = " ".join(parser.text).lower()
    required = _required_sections(prompt)
    section_results = {}
    for section in required:
        aliases = SECTION_ALIASES.get(section, (section,))
        if section == "footer":
            section_results[section] = "footer" in parser.tags
        elif section == "hero":
            section_results[section] = (
                "h1" in parser.tags
                and (any(alias in evidence for alias in aliases) or "main" in parser.tags)
            )
        elif section == "pricing":
            has_price = bool(
                re.search(r"(?:[$€£]\s*\d+(?:,\d{3})*(?:\.\d{2})?|\b\d+(?:\.\d{2})?\s*(?:usd|dollars?)\b)", page_text, re.IGNORECASE)
            )
            section_results[section] = any(alias in evidence for alias in aliases) or has_price
        elif section == "hours":
            has_hours_schedule = bool(
                re.search(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2})\b", page_text, re.IGNORECASE)
            )
            section_results[section] = any(alias in evidence for alias in aliases) or has_hours_schedule
        else:
            section_results[section] = any(alias in evidence for alias in aliases)

    visible_content = " ".join(parser.visible_text)
    visible_links = " ".join(parser.visible_links)
    template_credit_visible = bool(
        re.search(r"\bhtml5\s*up\b|\bdesign reference\b|\bmassively\b", visible_content, re.IGNORECASE)
        or re.search(r"html5up\.net", visible_links, re.IGNORECASE)
    )

    checks = {
        "html_file_present": bool(html),
        "no_visible_template_credit": not template_credit_visible,
        "cta_buttons_no_arrows": not parser.cta_arrow_issues,
        "accessible_mobile_hamburger": parser.has_accessible_mobile_hamburger(),
        "css_file_present": bool(css),
        "document_has_main": "main" in parser.tags,
        "document_has_h1": "h1" in parser.tags,
        "document_closed": bool(
            re.search(r"</body\s*>[\s\S]*?</html\s*>\s*(?:<!--[\s\S]*?-->\s*)*$", html, re.IGNORECASE)
        ),
        "styles_css_linked": bool(
            re.search(
                r"<link\b[^>]*href\s*=\s*['\"](?:\./)?styles\.css['\"][^>]*>",
                html,
                re.IGNORECASE,
            )
        ),
        "css_braces_balanced": bool(css) and css.count("{") == css.count("}"),
        # A copyright-only footer is not a useful closing section. Require visible
        # content and at least two useful links or contact targets.
        "footer_has_substance": (
            len(" ".join(parser.footer_text)) >= 45
            and len(parser.footer_links) >= 2
        ),
    }
    for section, present in section_results.items():
        checks[f"section_{section}"] = present

    hero_image_present = parser.hero_image_present or _hero_has_css_background_image(
        css, parser.hero_selectors
    )
    checks["hero_has_image"] = hero_image_present

    parser.hero_image_urls.extend(_image_urls_from_css(css, parser.hero_selectors))
    # Check image-bearing CSS properties across the whole page, not just the hero.
    css_image_urls = _all_css_image_urls(css)
    parser.all_image_urls.extend(css_image_urls)
    target_match = re.search(r"https?://[^\s\"'<>]+", prompt, re.IGNORECASE)
    base_url = target_match.group(0).rstrip(".,);]") if target_match else None
    hero_images_load_ok = _hero_images_load_ok(parser.hero_image_urls, base_url=base_url)
    checks["hero_images_load_ok"] = hero_images_load_ok

    image_urls = _unique_image_urls(parser.all_image_urls, base_url)
    failed_images = _failed_image_urls(image_urls, base_url)
    checks["all_images_load_ok"] = bool(image_urls) and not failed_images
    checks["image_alt_text_present"] = not parser.images_missing_alt
    request_text = re.split(
        r"\n\s*(?:VERIFIED SOURCE SITE AUDIT|The runner could not fetch the public source homepage)",
        prompt,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    requires_image_coverage = bool(
        re.search(r"\b(?:imagery|images|photos|photographs|photo gallery|image gallery)\b", request_text, re.IGNORECASE)
    )
    minimum_image_count = 3 if requires_image_coverage else 1
    checks["image_coverage"] = len(image_urls) >= minimum_image_count
    visual_review_required = bool(image_urls)
    visual_review_status, visual_review_missing_urls, visual_review_issues = _visual_review_result(
        session_dir, image_urls, base_url
    )
    visual_design_review_status, visual_design_review_missing, visual_design_review_issues = (
        _visual_design_review_result(session_dir)
    )
    checks["visual_design_review_passed"] = visual_design_review_status == "pass"
    browser_qa_status, browser_qa_checks, browser_qa_report = _browser_qa_result(session_dir)
    browser_check_names = (
        "rendered_sections_visible_desktop_and_mobile",
        "hero_spans_viewport_desktop_and_mobile",
        "cta_visible_first_screen_desktop_and_mobile",
        "mobile_menu_clicks_work",
        "mobile_menu_keyboard_works",
        "mobile_has_no_horizontal_overflow",
        "fonts_load_in_browser",
        "images_load_in_browser",
    )
    for name in browser_check_names:
        checks[f"browser_{name}"] = browser_qa_status in {"pass", "fail"} and browser_qa_checks.get(name) is True

    source_image_urls = _source_image_urls_from_prompt(prompt)
    if source_image_urls:
        source_keys = {key for url in source_image_urls if (key := _url_key(url, base_url))}
        used_keys = {key for url in parser.all_image_urls if (key := _url_key(url, base_url))}
        hero_keys = {key for url in parser.hero_image_urls if (key := _url_key(url, base_url))}
        source_used = source_keys & used_keys
        source_hero_used = bool(source_keys & hero_keys)
        required_source_count = min(2, len(source_keys))
        checks["source_site_hero_photo_used"] = source_hero_used
        checks["source_site_photos_used"] = len(source_used) >= required_source_count
        checks["source_site_photos_load_ok"] = bool(source_used) and all(
            _image_loads_ok(url, base_url=base_url)
            for url in parser.all_image_urls
            if (key := _url_key(url, base_url)) in source_keys
        )

    if re.search(r"\b(?:form|booking form|contact form|request form)\b", prompt, re.IGNORECASE):
        checks["requested_form_present"] = parser.forms > 0

    passed = sum(checks.values())
    total = len(checks)
    automated_checks = {name: value for name, value in checks.items() if name != "visual_design_review_passed"}
    automated_success = bool(html and css and all(automated_checks.values()))
    missing_sections = [name for name, present in section_results.items() if not present]
    if not hero_image_present:
        missing_sections.append("hero image")
    if not hero_images_load_ok:
        missing_sections.append("hero image loads")
    if failed_images:
        missing_sections.append("all image assets load")
    if not checks["image_alt_text_present"]:
        missing_sections.append("alt attribute on every content image")
    if not checks["image_coverage"]:
        missing_sections.append(f"at least {minimum_image_count} distinct image assets")
    if not checks["footer_has_substance"]:
        missing_sections.append("a substantive footer with at least two useful links")
    if template_credit_visible:
        missing_sections.append("no visible template credit in page text or links")
    if parser.cta_arrow_issues:
        missing_sections.append("CTA buttons without arrow glyphs or arrow icons")
    if not checks["accessible_mobile_hamburger"]:
        missing_sections.append("accessible three-bar mobile navigation button")
    if visual_design_review_status != "pass":
        missing_sections.append("screenshot-backed desktop/mobile visual design review")
    if browser_qa_status != "pass":
        missing_sections.append("automated rendered-page browser checks")
        browser_labels = {
            "rendered_sections_visible_desktop_and_mobile": "required sections visibly render on desktop and mobile",
            "hero_spans_viewport_desktop_and_mobile": "hero spans the desktop and mobile viewports",
            "cta_visible_first_screen_desktop_and_mobile": "CTA appears on the first screen at desktop and mobile sizes",
            "mobile_menu_clicks_work": "mobile menu opens and closes by click",
            "mobile_menu_keyboard_works": "mobile menu works with keyboard controls",
            "mobile_has_no_horizontal_overflow": "mobile page has no horizontal overflow",
            "fonts_load_in_browser": "browser fonts load successfully",
            "images_load_in_browser": "browser images load successfully",
        }
        for name, label in browser_labels.items():
            if browser_qa_checks.get(name) is not True:
                missing_sections.append(label)
    if source_image_urls and not checks.get("source_site_hero_photo_used"):
        missing_sections.append("verified source-site photo in hero")
    if source_image_urls and not checks.get("source_site_photos_used"):
        missing_sections.append("at least two verified source-site photos")
    if source_image_urls and not checks.get("source_site_photos_load_ok"):
        missing_sections.append("verified source-site photos load")

    return {
        "session_folder": session_dir.name,
        "session_path": str(session_dir),
        "html_path": str(html_path),
        "css_path": str(css_path),
        "score": round(100 * passed / total) if total else 0,
        "checks_passed": passed,
        "checks_total": total,
        "checks": checks,
        "automated_success": automated_success,
        "required_sections": required,
        "missing_sections": missing_sections,
        "hero_image_present": hero_image_present,
        "hero_images_load_ok": hero_images_load_ok,
        "hero_image_urls": parser.hero_image_urls,
        "image_urls": image_urls,
        "failed_image_urls": failed_images,
        "minimum_image_count": minimum_image_count,
        "visual_relevance_review_required": visual_review_required,
        "visual_review_status": visual_review_status,
        "visual_review_missing_urls": visual_review_missing_urls,
        "visual_review_issues": visual_review_issues,
        "visual_design_review_status": visual_design_review_status,
        "visual_design_review_missing": visual_design_review_missing,
        "visual_design_review_issues": visual_design_review_issues,
        "browser_qa_status": browser_qa_status,
        "browser_qa": browser_qa_report,
        "source_image_urls_available": source_image_urls,
        "source_image_urls_used": sorted(source_used) if source_image_urls else [],
        "parse_diagnostic": parse_error,
        "html_bytes": len(html.encode("utf-8")),
        "css_bytes": len(css.encode("utf-8")),
        "footer_text": " ".join(parser.footer_text),
        "footer_links": parser.footer_links,
        "cta_arrow_issues": parser.cta_arrow_issues,
        "complete": bool(html and css and all(checks.values())),
        "final_success": bool(
            automated_success and checks["visual_design_review_passed"]
            and (not visual_review_required or visual_review_status == "pass")
            and visual_design_review_status == "pass"
        ),
    }
