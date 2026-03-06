"""
Module 2: LinkedIn Feed Posts Scraper
Gets posts from the user's LinkedIn feed and filters for PM hiring announcements.
Uses linkedin-api with li_at cookie auth (preferred) or email/password.
"""

import os
import time
import random
import logging
from datetime import datetime
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# Keywords to match against post text (case-insensitive)
HIRING_KEYWORDS = [
    "hiring product manager",
    "hiring senior product manager",
    "looking for a product manager",
    "looking for a senior product manager",
    "we're hiring a pm",
    "we are hiring a pm",
    "pm role open",
    "pm position open",
    "product manager role",
    "product manager position",
    "open role: product manager",
    "open position: product manager",
]


def _load_credentials() -> Tuple[str, str, str]:
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
    except ImportError:
        pass
    li_at    = os.environ.get("LINKEDIN_LI_AT", "").strip() or ""
    email    = os.environ.get("LINKEDIN_EMAIL", "").strip() or ""
    password = os.environ.get("LINKEDIN_PASSWORD", "").strip() or ""
    return li_at, email, password


def _fetch_session_cookies(li_at: str):
    """
    Build a proper RequestsCookieJar with li_at + JSESSIONID.
    linkedin-api needs both as real Cookie objects (not a plain dict)
    or requests raises 'dict has no attribute extract_cookies'.
    """
    import requests
    from requests.cookies import RequestsCookieJar

    probe = requests.Session()
    probe.cookies.set("li_at", li_at, domain=".linkedin.com")
    probe.headers.update({"User-Agent": "Mozilla/5.0"})
    jsessionid = '"ajax:0"'
    try:
        resp = probe.get("https://www.linkedin.com/feed/", allow_redirects=True, timeout=10)
        raw = probe.cookies.get("JSESSIONID") or resp.cookies.get("JSESSIONID") or ""
        if raw:
            jsessionid = raw
        logger.debug(f"JSESSIONID fetched: {jsessionid!r}")
    except Exception as e:
        logger.warning(f"Could not fetch JSESSIONID ({e}); using fallback")

    jar = RequestsCookieJar()
    jar.set("li_at",      li_at,      domain=".linkedin.com", path="/")
    jar.set("JSESSIONID", jsessionid, domain=".linkedin.com", path="/")
    return jar


def _post_text(post: dict) -> str:
    """Extract plain text from a linkedin-api feed post object."""
    # Direct text field
    for path in [
        ["commentary", "text", "text"],
        ["commentary", "text"],
        ["text", "text"],
        ["text"],
        ["description", "text"],
    ]:
        val = post
        for key in path:
            if not isinstance(val, dict):
                val = None
                break
            val = val.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Reshared content
    reshare = post.get("resharedPost") or post.get("repost") or {}
    if reshare:
        return _post_text(reshare)
    return ""


def _post_url(post: dict) -> str:
    """Extract or build a URL for a feed post."""
    for key in ("shareUrl", "url", "permalink", "shareUri"):
        val = post.get(key)
        if val:
            return str(val).strip()
    # Build from URN
    urn = post.get("entityUrn") or post.get("urn") or post.get("updateUrn") or ""
    if urn:
        urn_id = str(urn).split(":")[-1]
        if "activity" in str(urn):
            return f"https://www.linkedin.com/feed/update/urn:li:activity:{urn_id}/"
        return f"https://www.linkedin.com/feed/update/{urn}/"
    return ""


def _author_info(post: dict) -> Tuple[str, str]:
    """Return (author_name, author_url) from a feed post."""
    # Actor object
    actor = post.get("actor") or post.get("author") or {}
    name  = ""
    url   = ""

    if isinstance(actor, dict):
        name = (
            actor.get("name", {}).get("text", "") if isinstance(actor.get("name"), dict)
            else str(actor.get("name", ""))
        )
        urn = actor.get("urn") or actor.get("entityUrn") or ""
        if urn:
            uid = str(urn).split(":")[-1]
            url = f"https://www.linkedin.com/in/{uid}/"

    # Fallback top-level fields
    if not name:
        name = post.get("authorName") or post.get("authorFullName") or ""
    if not url:
        nav_url = post.get("actorNavigationUrl") or actor.get("navigationUrl") or ""
        if nav_url:
            url = nav_url

    return str(name).strip(), str(url).strip()


def scrape_feed_posts() -> Tuple[List[Dict], str]:
    """
    Fetch the user's LinkedIn feed and return posts that mention PM hiring.
    Returns (list_of_post_dicts, status_string).
    status: "ok" | "skipped_no_creds" | "login_failed" | "captcha" | "error"
    """
    li_at, email, password = _load_credentials()

    if not li_at and (not email or not password):
        logger.warning(
            "No LinkedIn credentials. Set LINKEDIN_LI_AT (preferred) "
            "or LINKEDIN_EMAIL + LINKEDIN_PASSWORD in .env"
        )
        return [], "skipped_no_creds"

    try:
        from linkedin_api import Linkedin
    except ImportError:
        logger.error("linkedin-api not installed. Run: pip install linkedin-api")
        return [], "error"

    # ── Authenticate ─────────────────────────────────────────────────────────
    try:
        if li_at:
            logger.info("Authenticating with li_at cookie...")
            api = Linkedin("", "", cookies=_fetch_session_cookies(li_at))
        else:
            logger.info(f"Authenticating with email: {email}")
            api = Linkedin(email, password)
        logger.info("LinkedIn login successful")
    except Exception as e:
        err_str = str(e).lower()
        if "captcha" in err_str or "challenge" in err_str or "verification" in err_str:
            logger.warning(f"LinkedIn captcha/challenge during login: {e}")
            return [], "captcha"
        logger.error(f"LinkedIn login failed: {e}", exc_info=True)
        return [], "login_failed"

    # ── Fetch feed posts ──────────────────────────────────────────────────────
    date_scraped = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_posts: List[Dict] = []
    seen_urls: set = set()
    status = "ok"

    try:
        logger.info("Fetching LinkedIn feed posts (limit=100)...")
        feed_items = api.get_feed_posts(limit=100) or []
        logger.info(f"  -> {len(feed_items)} raw feed items fetched")

        for item in feed_items:
            # linkedin-api returns: {author_name, author_profile, content, url, old}
            text = (item.get("content") or _post_text(item) or "").strip()
            if not text:
                continue

            text_lower = text.lower()
            matched_kw = next(
                (kw for kw in HIRING_KEYWORDS if kw in text_lower), None
            )
            if not matched_kw:
                continue  # Not a hiring post

            post_url = item.get("url") or _post_url(item)
            if post_url in seen_urls:
                continue
            seen_urls.add(post_url)

            author_name = (item.get("author_name") or "").strip()
            author_url  = (item.get("author_profile") or "").strip()
            if not author_name or not author_url:
                name_fallback, url_fallback = _author_info(item)
                author_name = author_name or name_fallback
                author_url  = author_url  or url_fallback

            all_posts.append({
                "author_name":  author_name,
                "author_url":   author_url,
                "post_text":    text,
                "post_url":     post_url,
                "date_scraped": date_scraped,
                "keyword":      matched_kw,
                "type":         "feed_post",
            })

        logger.info(f"  -> {len(all_posts)} matching PM hiring posts found")

    except Exception as e:
        err_str = str(e).lower()
        if "captcha" in err_str or "challenge" in err_str:
            logger.warning(f"LinkedIn captcha detected: {e}")
            status = "captcha"
        else:
            logger.error(f"Error fetching feed posts: {e}", exc_info=True)
            status = "error"

    delay = random.uniform(2, 4)
    time.sleep(delay)

    logger.info(f"Feed posts total: {len(all_posts)} (matched from feed)")
    return all_posts, status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    posts, status = scrape_feed_posts()
    print(f"\nResults: {len(posts)} posts | Status: {status}")
    for p in posts[:10]:
        print(f"  [{p['keyword']}] {p['author_name']}: {p['post_text'][:100]}...")
