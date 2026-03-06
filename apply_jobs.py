"""
apply_jobs.py — LinkedIn Easy Apply automation via Selenium.

Flow:
  1. Open job URL in Chrome (reusing li_at cookie for auth).
  2. Click the Easy Apply button.
  3. Walk through modal pages, filling fields automatically.
  4. STOP at the final Review / Submit page — do NOT click Submit.
  5. Leave browser open for user review.

Returns:
  {"status": "review_ready"}          — stopped at review page, browser open
  {"status": "error", "message": ...} — something went wrong
"""

import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Profile (from .env) ───────────────────────────────────────────────────────
def _load_profile() -> dict:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    return {
        "li_at":           os.getenv("LINKEDIN_LI_AT", ""),
        "resume_path":     os.getenv("LINKEDIN_RESUME_PATH", ""),
        "work_auth":       os.getenv("LINKEDIN_WORK_AUTH", "No"),
        "experience_years": os.getenv("LINKEDIN_EXPERIENCE_YEARS", "4"),
        "cover_letter":    os.getenv("LINKEDIN_COVER_LETTER", ""),
    }


# ── Driver setup ──────────────────────────────────────────────────────────────
def _make_driver():
    import subprocess
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver_path = ChromeDriverManager().install()
    # macOS Gatekeeper blocks unsigned downloaded binaries (SIGKILL / exit -9).
    # Ad-hoc codesign makes it runnable without needing a developer certificate.
    try:
        subprocess.run(["codesign", "--force", "--sign", "-", driver_path],
                       capture_output=True)
    except Exception:
        pass

    service = Service(driver_path)
    driver  = webdriver.Chrome(service=service, options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def _inject_cookie(driver, li_at: str):
    """Inject li_at cookie so we're logged into LinkedIn using CDP."""
    # Load any LinkedIn page first so the browser context is initialised
    driver.get("https://www.linkedin.com/robots.txt")
    time.sleep(1)
    # Use Chrome DevTools Protocol to set the cookie with the .linkedin.com domain
    # (driver.add_cookie() requires the current page domain to match — CDP bypasses that)
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setCookie", {
        "name":     "li_at",
        "value":    li_at,
        "domain":   ".linkedin.com",
        "path":     "/",
        "secure":   True,
        "httpOnly": True,
        "sameSite": "None",
    })
    driver.get("https://www.linkedin.com/feed/")
    time.sleep(3)


# ── Modal helpers ──────────────────────────────────────────────────────────────
def _wait_for(driver, by, selector, timeout=10):
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, selector))
    )


def _safe_find(driver, by, selector):
    from selenium.webdriver.common.by import By  # noqa — imported for callers
    try:
        return driver.find_element(by, selector)
    except Exception:
        return None


def _fill_text_input(el, value: str):
    """Clear and fill a text/number input."""
    from selenium.webdriver.common.keys import Keys
    el.click()
    el.send_keys(Keys.CONTROL + "a")
    el.send_keys(Keys.DELETE)
    el.send_keys(value)


def _label_text(driver, el) -> str:
    """Try to get the label text associated with an input element."""
    try:
        el_id = el.get_attribute("id")
        if el_id:
            label = _safe_find(driver, "css selector", f'label[for="{el_id}"]')
            if label:
                return label.text.lower()
        # Walk up to find a fieldset/legend
        parent = driver.execute_script("return arguments[0].closest('fieldset')", el)
        if parent:
            legend = parent.find_element("tag name", "legend")
            return legend.text.lower()
    except Exception:
        pass
    return ""


def _click_next_or_review(driver) -> str:
    """
    Click the 'Next', 'Review', or 'Continue' button.
    Returns: 'next' | 'review' | 'submit' | 'none'
    """
    from selenium.webdriver.common.by import By
    buttons = driver.find_elements(By.CSS_SELECTOR, "button[aria-label]")
    for btn in buttons:
        label = (btn.get_attribute("aria-label") or "").lower()
        text  = btn.text.strip().lower()
        if "submit application" in label or "submit application" in text:
            return "submit"
        if "review" in label or "review" in text:
            btn.click()
            time.sleep(1.5)
            return "review"
        if "next" in label or "next" in text:
            btn.click()
            time.sleep(1.5)
            return "next"
        if "continue" in label or "continue" in text:
            btn.click()
            time.sleep(1.5)
            return "next"
    return "none"


def _fill_modal_page(driver, profile: dict):
    """Fill all recognisable fields on the current modal page."""
    from selenium.webdriver.common.by import By

    # ── Resume upload ──────────────────────────────────────────────────────────
    resume_path = profile["resume_path"]
    if resume_path and Path(resume_path).exists():
        file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
        for fi in file_inputs:
            try:
                fi.send_keys(resume_path)
                time.sleep(1)
            except Exception:
                pass

    # ── Text / number inputs ───────────────────────────────────────────────────
    text_inputs = driver.find_elements(
        By.CSS_SELECTOR,
        'input[type="text"], input[type="number"], input[type="tel"]'
    )
    for inp in text_inputs:
        lbl = _label_text(driver, inp)
        inp_type = (inp.get_attribute("type") or "").lower()

        if inp_type == "tel":
            # Phone — skipped per user preference
            continue

        if any(k in lbl for k in ["year", "experience", "how many"]):
            _fill_text_input(inp, profile["experience_years"])

        elif "linkedin" in lbl:
            # Skip — already on LinkedIn
            pass

        elif "website" in lbl or "portfolio" in lbl or "url" in lbl:
            pass  # skip optional URL fields

    # ── Textareas (cover letter / additional info) ─────────────────────────────
    textareas = driver.find_elements(By.CSS_SELECTOR, "textarea")
    for ta in textareas:
        lbl = _label_text(driver, ta)
        if ta.get_attribute("value") or ta.text.strip():
            continue  # already filled
        if any(k in lbl for k in ["cover", "letter", "additional", "note", "summary"]):
            _fill_text_input(ta, profile["cover_letter"])

    # ── Radio buttons (Yes/No questions, e.g. work authorisation) ─────────────
    radios = driver.find_elements(By.CSS_SELECTOR, 'input[type="radio"]')
    for radio in radios:
        lbl = _label_text(driver, radio)
        val = (radio.get_attribute("value") or "").strip().lower()
        # Work auth question
        if any(k in lbl for k in ["authorized", "authorised", "eligible", "legally",
                                   "sponsorship", "work in"]):
            desired = profile["work_auth"].strip().lower()  # "yes" or "no"
            if val == desired or val.startswith(desired):
                if not radio.is_selected():
                    radio.click()
                    time.sleep(0.5)

    # ── Dropdowns (select) ─────────────────────────────────────────────────────
    from selenium.webdriver.support.ui import Select
    selects = driver.find_elements(By.CSS_SELECTOR, "select")
    for sel in selects:
        lbl = _label_text(driver, sel)
        if any(k in lbl for k in ["year", "experience"]):
            try:
                s = Select(sel)
                # Pick option closest to experience_years
                target = int(profile["experience_years"])
                best = None
                for opt in s.options:
                    try:
                        n = int(opt.text.strip().split()[0])
                        if best is None or abs(n - target) < abs(best - target):
                            best = n
                    except ValueError:
                        pass
                if best is not None:
                    s.select_by_visible_text(str(best))
            except Exception:
                pass


# ── Main entry point ──────────────────────────────────────────────────────────
def run_easy_apply(job_url: str) -> dict:
    """
    Open job_url, click Easy Apply, fill form pages, stop at Review.
    Browser remains open for user to review and submit manually.

    Returns dict with 'status' key: 'review_ready' or 'error'.
    """
    profile = _load_profile()

    if not profile["li_at"]:
        return {"status": "error", "message": "LINKEDIN_LI_AT not set in .env"}

    # Build a default cover letter if none set
    if not profile["cover_letter"]:
        profile["cover_letter"] = (
            "I am excited to apply for this Product Manager role. "
            "With 4 years of PM experience driving cross-functional roadmaps, "
            "I am confident I can deliver meaningful impact to your team. "
            "I look forward to discussing how my background aligns with your needs."
        )

    driver = None
    try:
        from selenium.webdriver.common.by import By
        driver = _make_driver()
        _inject_cookie(driver, profile["li_at"])

        logger.info(f"Navigating to job: {job_url}")
        driver.get(job_url)
        time.sleep(5)  # Give LinkedIn JS time to fully render

        # ── Click Easy Apply ───────────────────────────────────────────────────
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        easy_btn = None
        # Try aria-label first (most reliable), then fall back to button text
        for locator in [
            (By.CSS_SELECTOR, 'button[aria-label*="Easy Apply"]'),
            (By.XPATH, '//button[contains(., "Easy Apply")]'),
        ]:
            try:
                easy_btn = WebDriverWait(driver, 20).until(
                    EC.element_to_be_clickable(locator)
                )
                if easy_btn:
                    break
            except Exception:
                continue

        if not easy_btn:
            # Don't crash — leave browser open so user can click manually
            logger.info("Easy Apply button not found — browser left open for manual interaction")
            return {"status": "review_ready", "message": "Browser open — click Easy Apply manually if visible"}

        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", easy_btn)
        time.sleep(0.5)
        easy_btn.click()
        time.sleep(2)

        # ── Walk through modal pages ───────────────────────────────────────────
        max_steps = 15
        for step in range(max_steps):
            logger.info(f"Easy Apply step {step + 1}")
            _fill_modal_page(driver, profile)
            time.sleep(1)

            action = _click_next_or_review(driver)
            logger.info(f"  → action: {action}")

            if action == "review":
                # Stopped at review — success
                logger.info("Reached review page. Stopping for user review.")
                return {"status": "review_ready"}

            if action == "submit":
                # We deliberately do NOT click Submit; stop here
                logger.info("Submit button visible — stopping for user review.")
                return {"status": "review_ready"}

            if action == "none":
                # No navigation button found — might be done or stuck
                logger.warning("No Next/Review/Submit button found. Stopping.")
                return {"status": "review_ready"}

        logger.warning("Reached max steps without finding review page.")
        return {"status": "review_ready"}

    except Exception as e:
        logger.error(f"Easy Apply automation error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    # Note: driver is intentionally left open for user review
