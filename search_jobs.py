"""
Module 1: Job Listings Scraper
Scrapes PM job listings from LinkedIn only.

Easy Apply detection (accurate)
---------------------------------
After scraping, each LinkedIn job's apply method is verified via LinkedIn's
Voyager API (linkedin-api library, cookie auth):

  applyMethod key = 'com.linkedin.voyager.jobs.OffsiteApply'
      → redirects to external company site → NOT Easy Apply

  applyMethod key = 'com.linkedin.voyager.jobs.ComplexOnsiteApply'
                  | 'com.linkedin.voyager.jobs.SimpleOnsiteApply'
      → application handled on LinkedIn → IS Easy Apply

This is the only 100% accurate signal for Easy Apply.
"""

import os
import time
import random
import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# LinkedIn only (Indeed + Glassdoor removed per user request)
SEARCH_CONFIGS = [
    # (site, search_term, location, country_indeed, results_wanted)
    ("linkedin", "Product Manager",        None,                   "USA", 50),
    ("linkedin", "Senior Product Manager", None,                   "USA", 50),
    ("linkedin", "Product Manager",        "India",                "USA", 30),
    ("linkedin", "Senior Product Manager", "India",                "USA", 30),
    ("linkedin", "Product Manager",        "Singapore",            "USA", 30),
    ("linkedin", "Senior Product Manager", "Singapore",            "USA", 30),
    ("linkedin", "Product Manager",        "United Arab Emirates", "USA", 30),
    ("linkedin", "Senior Product Manager", "United Arab Emirates", "USA", 30),
]

ONSITE_KEYS = {
    "com.linkedin.voyager.jobs.ComplexOnsiteApply",
    "com.linkedin.voyager.jobs.SimpleOnsiteApply",
}


def _job_id(url: str) -> str:
    try:
        return url.rstrip("/").split("/")[-1].split("?")[0]
    except Exception:
        return ""


def _make_linkedin_api():
    """Authenticate against LinkedIn Voyager API using li_at cookie."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    li_at = os.environ.get("LINKEDIN_LI_AT", "").strip()
    if not li_at:
        return None
    try:
        import requests
        from requests.cookies import RequestsCookieJar
        from linkedin_api import Linkedin

        # Get JSESSIONID by probing the feed (same pattern as search_posts.py)
        session = requests.Session()
        session.cookies.set("li_at", li_at, domain=".linkedin.com")
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        jsessionid = '"ajax:0"'
        try:
            resp = session.get("https://www.linkedin.com/feed/", timeout=10)
            raw = session.cookies.get("JSESSIONID") or resp.cookies.get("JSESSIONID") or ""
            if raw:
                jsessionid = raw
        except Exception:
            pass

        jar = RequestsCookieJar()
        jar.set("li_at",      li_at,      domain=".linkedin.com", path="/")
        jar.set("JSESSIONID", jsessionid, domain=".linkedin.com", path="/")
        return Linkedin("", "", cookies=jar)
    except Exception as e:
        logger.warning(f"LinkedIn API auth failed: {e}")
        return None


def _verify_easy_apply(job_ids: List[str], api) -> Dict[str, bool]:
    """
    Call api.get_job() for each job ID and return {job_id: is_easy_apply}.
    Batches calls with delays to avoid rate limiting.
    """
    results: Dict[str, bool] = {}
    total = len(job_ids)
    logger.info(f"Verifying Easy Apply for {total} LinkedIn jobs via Voyager API…")

    for i, jid in enumerate(job_ids, 1):
        try:
            job = api.get_job(jid) or {}
            apply_method = job.get("applyMethod", {})
            is_ea = bool(set(apply_method.keys()) & ONSITE_KEYS)
            results[jid] = is_ea
            if i % 20 == 0:
                logger.info(f"  {i}/{total} verified…")
        except Exception as e:
            logger.warning(f"  get_job({jid}) failed: {e} — skipping")
            results[jid] = False

        # Polite delay: 0.8-1.5s per call, longer pause every 25 calls
        time.sleep(random.uniform(0.8, 1.5))
        if i % 25 == 0:
            time.sleep(random.uniform(3, 6))

    confirmed = sum(1 for v in results.values() if v)
    logger.info(f"Easy Apply verified: {confirmed} / {total}")
    return results


def scrape_job_listings() -> Tuple[List[Dict], Dict[str, str]]:
    """
    Scrape PM job listings from LinkedIn.
    Returns (list_of_job_dicts, source_status_dict).
    """
    try:
        from jobspy import scrape_jobs
        import pandas as pd
    except ImportError:
        logger.error("python-jobspy not installed. Run: pip install python-jobspy")
        return [], {"linkedin": "error"}

    # ── Authenticate with LinkedIn Voyager API for Easy Apply verification ─────
    api = _make_linkedin_api()
    if not api:
        logger.warning("LinkedIn API unavailable — Easy Apply will not be detected.")

    # ── Scrape ─────────────────────────────────────────────────────────────────
    raw_jobs: List[Dict] = []
    seen: set = set()
    source_status: Dict[str, str] = {"linkedin": "ok"}

    for (site, term, location, country_indeed, results_wanted) in SEARCH_CONFIGS:
        label = location or "global"
        logger.info(f"Searching {site} | '{term}' | {label}…")

        kwargs = dict(
            site_name=[site],
            search_term=term,
            results_wanted=results_wanted,
            hours_old=72,
            country_indeed=country_indeed,
        )
        if location:
            kwargs["location"] = location

        try:
            df = scrape_jobs(**kwargs)
            if df is not None and not df.empty:
                added = 0
                for _, row in df.iterrows():
                    title   = str(row.get("title",   "") or "").strip()
                    company = str(row.get("company", "") or "").strip()
                    loc_val = str(row.get("location","") or "").strip()
                    dedup   = (title.lower(), company.lower(), loc_val.lower())
                    if dedup in seen:
                        continue
                    seen.add(dedup)

                    date_posted = row.get("date_posted")
                    if hasattr(date_posted, "strftime"):
                        date_posted = date_posted.strftime("%Y-%m-%d")
                    elif date_posted is None or (isinstance(date_posted, float)
                                                 and pd.isna(date_posted)):
                        date_posted = ""
                    else:
                        date_posted = str(date_posted)

                    job_url  = str(row.get("job_url",  "") or "").strip()
                    job_type = str(row.get("job_type", "") or "").strip()

                    raw_jobs.append({
                        "title":       title,
                        "company":     company,
                        "location":    loc_val,
                        "date_posted": date_posted,
                        "job_url":     job_url,
                        "job_type":    job_type,
                        "site":        site,
                        "easy_apply":  False,   # filled in after Voyager verification
                        "description": str(row.get("description") or "")[:3000],
                        "type":        "job_listing",
                    })
                    added += 1
                logger.info(f"  → {len(df)} fetched, {added} new after dedup")
            else:
                logger.info("  → 0 results")

        except Exception as e:
            err = str(e).lower()
            if "429" in err or "rate" in err or "blocked" in err or "captcha" in err:
                logger.warning(f"[{site}/{label}] Rate-limited: {e}")
                source_status[site] = "blocked"
            else:
                logger.error(f"[{site}/{label}] Error: {e}", exc_info=True)
                source_status[site] = "error"

        time.sleep(random.uniform(2, 5))

    # ── Verify Easy Apply via Voyager API ──────────────────────────────────────
    if api and raw_jobs:
        job_ids = [_job_id(j["job_url"]) for j in raw_jobs if j["job_url"]]
        job_ids = [jid for jid in job_ids if jid]
        ea_map  = _verify_easy_apply(job_ids, api)
        for j in raw_jobs:
            jid = _job_id(j["job_url"])
            j["easy_apply"] = ea_map.get(jid, False)

    easy_count = sum(1 for j in raw_jobs if j["easy_apply"])
    logger.info(f"Total: {len(raw_jobs)} unique jobs ({easy_count} confirmed Easy Apply)")
    return raw_jobs, source_status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    jobs, status = scrape_job_listings()
    print(f"\nResults: {len(jobs)} jobs | Status: {status}")
    easy = [j for j in jobs if j["easy_apply"]]
    print(f"Easy Apply: {len(easy)}")
    for j in easy[:10]:
        print(f"  ⚡ {j['title']} @ {j['company']} — {j['location']}")
