"""
extract_contacts.py — LinkedIn contact finder + Excel export.

Searches LinkedIn for HR/Recruiter/Talent and Product Manager contacts
at a given company using the linkedin-api library.
"""

import os
import time
import random
import logging
from pathlib import Path
from typing import List, Dict

logger = logging.getLogger(__name__)

KEYWORD_MAP = {
    "hr":      "recruiter OR HR OR talent OR people operations OR talent acquisition",
    "product": "product manager",   # used only as fallback for "all" type
    "all":     "recruiter OR HR OR talent OR product manager OR CPO",
}


# ── API factory ───────────────────────────────────────────────────────────────
def _build_cookie_jar(li_at: str):
    """
    Build a RequestsCookieJar with both li_at and JSESSIONID.
    linkedin-api requires JSESSIONID; we obtain it by probing /feed/.
    """
    import requests
    from requests.cookies import RequestsCookieJar

    session = requests.Session()
    session.cookies.set("li_at", li_at, domain=".linkedin.com")
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    jsessionid = '"ajax:0"'  # safe fallback
    try:
        resp = session.get("https://www.linkedin.com/feed/", timeout=10)
        raw = session.cookies.get("JSESSIONID") or resp.cookies.get("JSESSIONID") or ""
        if raw:
            jsessionid = raw
    except Exception as e:
        logger.warning(f"Could not fetch JSESSIONID ({e}); using fallback")

    jar = RequestsCookieJar()
    jar.set("li_at",      li_at,      domain=".linkedin.com", path="/")
    jar.set("JSESSIONID", jsessionid, domain=".linkedin.com", path="/")
    return jar


def make_api():
    """Create and return an authenticated linkedin_api.Linkedin instance."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

    from linkedin_api import Linkedin

    li_at    = os.getenv("LINKEDIN_LI_AT", "")
    email    = os.getenv("LINKEDIN_EMAIL", "")
    password = os.getenv("LINKEDIN_PASSWORD", "")

    if li_at:
        # Cookie-based auth — must include JSESSIONID or linkedin-api raises KeyError
        jar = _build_cookie_jar(li_at)
        api = Linkedin("", "", cookies=jar)
    elif email and password:
        api = Linkedin(email, password)
    else:
        raise RuntimeError("No LinkedIn credentials found in .env")

    return api


# ── Contact search ────────────────────────────────────────────────────────────
def search_company_contacts(
    company: str,
    contact_type: str,
    api,
    limit: int = 6,
) -> List[Dict]:
    """
    Search LinkedIn for people at `company` matching `contact_type`.

    contact_type: "hr" | "product" | "all"
    Returns list of contact dicts.
    """
    logger.info(f"Searching '{contact_type}' contacts at '{company}' …")

    if contact_type == "product":
        seen_urns = set()
        results   = []
        for kw in ["product manager", "director product OR head product OR VP product OR CPO"]:
            try:
                batch = api.search_people(
                    keyword_company=company,
                    keyword_title=kw,
                    limit=4,
                )
                for r in batch:
                    urn = r.get("urn_id") or r.get("public_id") or ""
                    if urn and urn not in seen_urns:
                        seen_urns.add(urn)
                        results.append(r)
            except Exception as e:
                logger.warning(f"Product search batch failed for '{kw}': {e}")
    else:
        keyword_title = KEYWORD_MAP.get(contact_type, KEYWORD_MAP["hr"])
        try:
            results = api.search_people(
                keyword_company=company,
                keyword_title=keyword_title,
                limit=limit,
            )
        except Exception as e:
            logger.error(f"LinkedIn people search failed: {e}", exc_info=True)
            return []

    contacts = []
    for r in results:
        urn = r.get("urn_id") or r.get("public_id") or ""
        if not urn:
            continue

        profile      = {}
        contact_info = {}

        try:
            profile = api.get_profile(urn) or {}
        except Exception as e:
            logger.warning(f"Could not fetch profile {urn}: {e}")

        try:
            contact_info = api.get_profile_contact_info(urn) or {}
        except Exception as e:
            logger.warning(f"Could not fetch contact info {urn}: {e}")

        # Name — try multiple locations
        first = profile.get("firstName") or r.get("firstName") or ""
        last  = profile.get("lastName")  or r.get("lastName")  or ""
        name  = f"{first} {last}".strip() or r.get("name", "")

        distance = r.get("distance") or ""   # "F", "S", "O", or ""

        contacts.append({
            "name":        name,
            "title":       profile.get("headline") or r.get("title") or "",
            "company":     company,
            "email":       contact_info.get("email_address") or "",
            "linkedin_url": f"https://www.linkedin.com/in/{urn}/",
            "contact_type": contact_type,
            "distance":    distance,
        })

        delay = random.uniform(1.0, 2.0)
        logger.debug(f"Sleeping {delay:.1f}s to avoid rate limits…")
        time.sleep(delay)

    logger.info(f"Found {len(contacts)} contacts for '{company}'")
    return contacts


# ── Connections export ────────────────────────────────────────────────────────
def get_connections(api, limit: int = 500) -> List[Dict]:
    """
    Fetch 1st-degree connections and their contact info.
    Returns list of contact dicts.
    """
    logger.info("Fetching 1st-degree connections…")
    try:
        raw = api.get_connections(limit=limit)
    except Exception as e:
        logger.error(f"get_connections failed: {e}", exc_info=True)
        return []

    contacts = []
    for r in raw:
        urn  = r.get("urn_id") or r.get("public_id") or ""
        contact_info = {}
        if urn:
            try:
                contact_info = api.get_profile_contact_info(urn) or {}
            except Exception:
                pass
            time.sleep(random.uniform(1, 2.5))

        first = r.get("firstName", "")
        last  = r.get("lastName", "")
        name  = f"{first} {last}".strip()

        contacts.append({
            "name":        name,
            "title":       r.get("headline") or r.get("occupation") or "",
            "company":     r.get("companyName") or "",
            "email":       contact_info.get("email_address") or "",
            "linkedin_url": f"https://www.linkedin.com/in/{urn}/" if urn else "",
            "contact_type": "connection",
        })

    logger.info(f"Fetched {len(contacts)} connections")
    return contacts


# ── Excel export ──────────────────────────────────────────────────────────────
def export_to_excel(contacts: List[Dict], path: str):
    """Write contacts list to an Excel file at `path`."""
    import pandas as pd

    if not contacts:
        logger.warning("No contacts to export.")
        return

    col_order = ["name", "title", "company", "email", "linkedin_url", "contact_type"]
    df = pd.DataFrame(contacts)
    # Ensure all columns exist
    for col in col_order:
        if col not in df.columns:
            df[col] = ""
    df = df[col_order]
    df.columns = ["Name", "Title", "Company", "Email", "LinkedIn URL", "Type"]

    df.to_excel(path, index=False, engine="openpyxl")
    logger.info(f"Exported {len(contacts)} contacts to {path}")


# ── CLI convenience ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Extract LinkedIn contacts for a company")
    parser.add_argument("company", help="Company name to search")
    parser.add_argument("--type", default="hr", choices=["hr", "product", "all"],
                        help="Contact type to search (default: hr)")
    parser.add_argument("--out", default="contacts.xlsx", help="Output Excel file path")
    args = parser.parse_args()

    api      = make_api()
    contacts = search_company_contacts(args.company, args.type, api)
    export_to_excel(contacts, args.out)
    print(f"Done — {len(contacts)} contacts exported to {args.out}")
