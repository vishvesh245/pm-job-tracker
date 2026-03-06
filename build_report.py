#!/usr/bin/env python3
"""
PM Job Tracker – main entry point.

Usage:
  python build_report.py            # generate HTML, open in browser (cron mode)
  python build_report.py --serve    # start local server + dashboard with Refresh button
"""

import argparse
import json
import os
import sys
import logging
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Dict
from urllib.parse import urlparse, parse_qs

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR    = BASE_DIR / "logs"
REPORTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

PORT = 5050

# ── Logging ──────────────────────────────────────────────────────────────────
log_file = LOGS_DIR / "job_tracker.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("build_report")

# ── Shared refresh state (for server mode) ───────────────────────────────────
_refresh_lock  = threading.Lock()
_refresh_state = {"running": False, "last_updated": "", "job_count": 0, "post_count": 0}

# ── Apply session lock (max 1 concurrent) ────────────────────────────────────
_apply_lock   = threading.Semaphore(1)
_apply_status = {"state": "idle", "message": ""}
_apply_lock_  = threading.Lock()

# ── Server-side contact cache (keyed by "company:type") ──────────────────────
_contacts_cache      = {}
_contacts_cache_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────
STATUS_COLORS = {
    "ok":             "#22c55e",
    "blocked":        "#f59e0b",
    "error":          "#ef4444",
    "skipped_no_creds": "#94a3b8",
    "login_failed":   "#ef4444",
    "captcha":        "#f59e0b",
}
STATUS_LABELS = {
    "ok":             "OK",
    "blocked":        "Blocked",
    "error":          "Error",
    "skipped_no_creds": "No Credentials",
    "login_failed":   "Login Failed",
    "captcha":        "Captcha – use li_at cookie",
}

def badge(label: str, status: str) -> str:
    color = STATUS_COLORS.get(status, "#94a3b8")
    text  = f"{label}: {STATUS_LABELS.get(status, status.upper())}"
    return (
        f'<span style="background:{color};color:#fff;padding:3px 10px;'
        f'border-radius:12px;font-size:12px;font-weight:600;margin:2px">{text}</span>'
    )

def esc(s: str) -> str:
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def detect_region(location: str) -> str:
    """Map a location string to a region key for chip filtering."""
    loc = location.lower()
    if not loc:
        return "other"
    if "remote" in loc:
        return "remote"
    if any(x in loc for x in ["india", "bengaluru", "bangalore", "mumbai", "delhi",
                               "hyderabad", "pune", "chennai", "kolkata", "gurugram",
                               "gurgaon", "noida"]):
        return "india"
    if "singapore" in loc:
        return "singapore"
    if any(x in loc for x in ["uae", "dubai", "emirates", "abu dhabi", "sharjah"]):
        return "uae"
    if any(x in loc for x in ["united kingdom", "london", "england", "manchester",
                               "birmingham", " uk", "(uk)"]):
        return "uk"
    if any(x in loc for x in ["canada", "toronto", "vancouver", "calgary",
                               "montreal", "ottawa", "ontario", "british columbia"]):
        return "canada"
    us_states = {", ca", ", ny", ", tx", ", wa", ", il", ", fl", ", ma",
                 ", ga", ", co", ", nc", ", va", ", nj", ", pa", ", oh",
                 ", mi", ", az", ", mn", ", md", ", dc", ", or", ", ct",
                 ", tn", ", in", ", mo", ", wi", ", ut", ", sc", ", nv",
                 ", ky", ", la", ", al", ", ia", ", ks", ", ne", ", nm",
                 ", id", ", wv", ", hi", ", mt", ", nd", ", sd", ", de",
                 ", vt", ", wy", ", ak", ", ri", ", nh", ", me", ", ok"}
    if any(s in loc for s in us_states) or "united states" in loc:
        return "usa"
    return "other"


def title_level(title: str) -> str:
    """Classify a job title into a level key for chip filtering."""
    t = title.lower()
    if any(x in t for x in ["director", "vp ", "vice president", "head of product"]):
        return "director"
    if any(x in t for x in ["principal", "staff pm", "staff product"]):
        return "principal"
    if "group" in t and "pm" in t:
        return "group"
    if "senior" in t or " sr " in t or " sr." in t:
        return "senior"
    return "pm"


# ── HTML Builder ──────────────────────────────────────────────────────────────
def build_html(
    jobs: List[Dict],
    job_status: Dict[str, str],
    posts: List[Dict],
    post_status: str,
    generated_at: str,
) -> str:

    # ─ Job rows ─
    job_rows = ""
    for j in jobs:
        url        = esc(j.get("job_url", ""))
        title      = j.get("title", "")
        company    = j.get("company", "")
        easy_apply = j.get("easy_apply", False)
        site       = j.get("site", "")
        loc        = j.get("location", "")
        date_p     = esc(j.get("date_posted", ""))
        region     = detect_region(loc)
        level      = title_level(title)

        score       = j.get("relevance_score")   # int or None
        score_label = j.get("relevance_label", "")
        score_attr  = str(score) if score is not None else ""

        easy_badge_html = '<span class="easy-badge">&#9889; Easy</span> ' if easy_apply else ""

        # Score badge in title cell (Strong/Good only)
        if score is not None and score >= 75:
            score_badge_html = f'<span class="score-badge score-strong">&#10022; {score}</span>'
        elif score is not None and score >= 55:
            score_badge_html = f'<span class="score-badge score-good">&#10022; {score}</span>'
        else:
            score_badge_html = ""

        onclick_attr = f'onclick="markVisited(\'{url}\')"' if url else ""
        link = (
            f'{easy_badge_html}{score_badge_html}'
            f'<a href="{url}" target="_blank" rel="noopener" {onclick_attr}>{esc(title)}</a>'
            if url else f'{easy_badge_html}{score_badge_html}{esc(title)}'
        )

        details_btn = (
            f'<button class="details-btn" data-url="{url}" '
            f'data-title="{esc(title)}" data-company="{esc(company)}" '
            f'onclick="openDetails(this)">&#128203; Details</button>'
        )

        score_td = str(score) if score is not None else "&mdash;"

        job_rows += (
            f'<tr data-url="{url}" data-site="{esc(site)}" data-date="{date_p}" '
            f'data-loc="{esc(loc.lower())}" data-region="{region}" data-level="{level}" '
            f'data-title="{esc(title.lower())}" data-easy="{"true" if easy_apply else "false"}" '
            f'data-score="{score_attr}">'
            f"<td>{link}</td>"
            f'<td>{esc(company)}</td>'
            f'<td>{esc(loc)}</td>'
            f'<td>{date_p}</td>'
            f'<td>{score_td}</td>'
            f'<td class="action-col">{details_btn}</td>'
            "</tr>\n"
        )
    if not job_rows:
        job_rows = '<tr><td colspan="7" style="text-align:center;color:#888;padding:20px">No job listings found.</td></tr>'

    # ─ Post cards ─
    post_cards = ""
    for p in posts:
        author_url  = esc(p.get("author_url", ""))
        author_name = esc(p.get("author_name", "Unknown"))
        author_link = (
            f'<a href="{author_url}" target="_blank" rel="noopener">{author_name}</a>'
            if author_url else author_name
        )
        post_url   = esc(p.get("post_url", ""))
        excerpt    = esc(p.get("post_text", "")[:200])
        post_link  = (
            f'<a href="{post_url}" target="_blank" rel="noopener" class="post-link">View Post →</a>'
            if post_url else ""
        )
        date_s = esc(p.get("date_scraped", ""))
        keyword= esc(p.get("keyword", ""))
        post_cards += f"""
        <div class="post-card" data-text="{esc(p.get('post_text','').lower())}">
          <div class="post-header">
            <span class="post-author">{author_link}</span>
            <span class="post-meta">{date_s} &bull; <em>{keyword}</em></span>
          </div>
          <p class="post-excerpt">{excerpt}{'...' if len(p.get('post_text','')) > 200 else ''}</p>
          <div class="post-footer">{post_link}</div>
        </div>"""

    # ─ Posts notice ─
    if post_status == "skipped_no_creds":
        posts_notice = """
        <div class="notice warning">
          <strong>Feed posts skipped:</strong> No LinkedIn credentials in <code>.env</code>.<br>
          <strong>Recommended:</strong> Add <code>LINKEDIN_LI_AT=&lt;your cookie&gt;</code> to .env
          (<a href="https://www.linkedin.com" target="_blank">log into LinkedIn</a>, open DevTools →
          Application → Cookies → linkedin.com → copy <strong>li_at</strong> value).
        </div>"""
    elif post_status == "captcha":
        posts_notice = """
        <div class="notice warning">
          <strong>LinkedIn blocked email/password login (captcha).</strong><br>
          Fix: Add <code>LINKEDIN_LI_AT=&lt;your cookie value&gt;</code> to .env instead.<br>
          How: Log into LinkedIn in Chrome → DevTools (F12) → Application → Cookies →
          <strong>linkedin.com</strong> → find <strong>li_at</strong> → copy Value.
        </div>"""
    elif post_status in ("login_failed", "error"):
        posts_notice = f"""
        <div class="notice error">
          <strong>Feed posts unavailable:</strong> status <em>{esc(post_status)}</em>.
          Check your credentials or try using <code>LINKEDIN_LI_AT</code> cookie auth.
        </div>"""
    elif not posts:
        posts_notice = '<div class="notice info">No matching feed posts found for this run.</div>'
    else:
        posts_notice = ""

    # ─ Badges ─
    badges_html = ""
    for site, s in job_status.items():
        badges_html += badge(site.capitalize(), s) + " "
    badges_html += badge("Feed Posts", post_status)

    # ─ Full HTML ─
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>PM Job Tracker — {generated_at}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #f8fafc; color: #1e293b; }}
    a {{ color: #0a66c2; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    /* Header */
    .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #0a66c2 100%);
               color: #fff; padding: 24px 32px; }}
    .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 6px; }}
    .header .meta {{ font-size: 13px; opacity: .8; margin-bottom: 8px; }}
    .header .counts {{ font-size: 15px; font-weight: 600; margin-bottom: 10px; }}
    .header-row {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-top: 10px; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 4px; flex: 1; }}
    #refreshBtn {{
      background: #fff; color: #0a66c2; border: none; padding: 8px 18px;
      border-radius: 8px; font-weight: 700; font-size: 14px; cursor: pointer;
      white-space: nowrap;
    }}
    #refreshBtn:disabled {{ opacity: .6; cursor: not-allowed; }}
    #refreshStatus {{ font-size: 12px; opacity: .85; margin-top: 4px; min-height: 16px; }}

    /* Sections */
    .section {{ padding: 24px 32px; }}
    .section h2 {{ font-size: 18px; font-weight: 700; margin-bottom: 16px;
                   color: #1e3a5f; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}

    /* Quick filter chips */
    .filter-bar {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
                   padding: 14px 16px; margin-bottom: 14px; display: flex;
                   flex-direction: column; gap: 10px; }}
    .filter-row  {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }}
    .filter-group {{ display: flex; flex-direction: column; gap: 5px; }}
    .filter-label {{ font-size: 10px; font-weight: 700; color: #94a3b8;
                     text-transform: uppercase; letter-spacing: .6px; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    .chip {{
      padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500;
      border: 1.5px solid #cbd5e1; background: #fff; color: #475569;
      cursor: pointer; transition: all .15s; white-space: nowrap;
    }}
    .chip:hover {{ border-color: #0a66c2; color: #0a66c2; background: #eff6ff; }}
    .chip.active {{ background: #0a66c2; color: #fff; border-color: #0a66c2; }}
    .chip.active:hover {{ background: #0856a8; }}

    /* City text input */
    .city-input {{
      padding: 4px 10px; border: 1.5px solid #cbd5e1; border-radius: 20px;
      font-size: 12px; background: #fff; outline: none; width: 140px;
      color: #475569;
    }}
    .city-input:focus {{ border-color: #0a66c2; box-shadow: 0 0 0 2px #0a66c220; }}

    /* Search input */
    .search-row {{ display: flex; gap: 8px; align-items: center; }}
    input[type=text] {{
      padding: 7px 12px; border: 1.5px solid #cbd5e1; border-radius: 8px;
      font-size: 13px; background: #fff; outline: none; flex: 1; max-width: 340px;
    }}
    input[type=text]:focus {{ border-color: #0a66c2; box-shadow: 0 0 0 2px #0a66c220; }}
    .clear-btn {{
      font-size: 12px; color: #64748b; background: none; border: none;
      cursor: pointer; padding: 4px 8px; border-radius: 6px;
    }}
    .clear-btn:hover {{ background: #f1f5f9; color: #1e293b; }}

    /* Table */
    .table-wrap {{ overflow-x: auto; border-radius: 8px; box-shadow: 0 1px 4px #0002; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 14px; }}
    th {{ background: #f1f5f9; color: #475569; font-weight: 600; padding: 10px 14px;
          text-align: left; cursor: pointer; user-select: none; white-space: nowrap; }}
    th:hover {{ background: #e2e8f0; }}
    th.no-sort {{ cursor: default; }}
    th.no-sort:hover {{ background: #f1f5f9; }}
    th .arr {{ margin-left: 4px; font-size: 11px; color: #94a3b8; }}
    td {{ padding: 9px 14px; border-top: 1px solid #f1f5f9; vertical-align: top; }}
    tr:hover td {{ background: #f8fafc; }}
    .count-label {{ font-size: 13px; color: #64748b; margin-bottom: 8px; }}

    /* Easy Apply badge */
    .easy-badge {{
      background: #f59e0b; color: #fff; font-size: 10px; font-weight: 700;
      padding: 2px 6px; border-radius: 10px; margin-right: 4px; vertical-align: middle;
    }}

    /* Relevance score badges */
    .score-badge {{
      font-size: 10px; font-weight: 700; padding: 2px 6px;
      border-radius: 10px; margin-right: 4px; vertical-align: middle;
    }}
    .score-strong {{ background: #dcfce7; color: #166534; }}
    .score-good   {{ background: #dbeafe; color: #1e40af; }}

    /* Visited job rows */
    tr.visited td:first-child a {{ color: #9ca3af; }}
    tr.visited {{ background: #f8fafc; }}

    /* Action buttons */
    .action-col {{ white-space: nowrap; }}
    /* Contact cards (shared by inline panel + modal) */
    .contact-card {{
      background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
      padding: 10px 14px; font-size: 13px; min-width: 220px; max-width: 280px;
    }}
    .contact-card .contact-name {{ font-weight: 600; color: #1e293b; }}
    .contact-pos {{ color: #64748b; font-size: 12px; margin-top: 2px; }}
    .contact-email-row {{ color: #334155; font-size: 12px; margin-top: 4px; }}
    .contact-card-actions {{ margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }}
    .copy-btn {{
      font-size: 11px; padding: 2px 7px; border-radius: 4px; border: 1px solid #cbd5e1;
      background: #fff; cursor: pointer; color: #475569;
    }}
    .copy-btn:hover {{ background: #f1f5f9; }}
    .export-row-btn {{
      font-size: 12px; color: #0a66c2; background: none; border: none;
      cursor: pointer; padding: 6px 0 0; text-decoration: underline;
    }}
    .contact-loading {{ color: #64748b; font-size: 13px; padding: 6px 0; }}
    .contact-grid {{ display: flex; flex-wrap: wrap; gap: 10px; }}

    /* Details button */
    .details-btn {{ background: #7c3aed; color: #fff; border: none; padding: 4px 10px;
      border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; }}
    .details-btn:hover {{ background: #6d28d9; }}

    /* Modal */
    .modal-overlay {{ position: fixed; inset: 0; background: #0006; z-index: 1000;
      display: flex; align-items: center; justify-content: center; }}
    .modal-box {{ background: #fff; border-radius: 14px; width: min(820px, 95vw);
      max-height: 88vh; display: flex; flex-direction: column; overflow: hidden;
      box-shadow: 0 8px 40px #0004; position: relative; }}
    .modal-close {{ position: absolute; top: 12px; right: 16px; background: none;
      border: none; font-size: 20px; cursor: pointer; color: #94a3b8; z-index: 1; }}
    .modal-header {{ padding: 20px 24px 14px; border-bottom: 1px solid #e2e8f0; }}
    .modal-header h2 {{ font-size: 17px; font-weight: 700; margin: 0 0 4px; color: #1e293b; }}
    .modal-header .mh-sub {{ font-size: 13px; color: #64748b; }}
    .modal-header .mh-link {{ font-size: 13px; color: #0a66c2; text-decoration: none; display: inline-block; margin-top: 4px; }}
    .modal-tabs {{ display: flex; border-bottom: 1px solid #e2e8f0; background: #f8fafc; flex-shrink: 0; overflow-x: auto; }}
    .tab-btn {{ padding: 10px 16px; border: none; background: none; cursor: pointer;
      font-size: 13px; font-weight: 500; color: #64748b; border-bottom: 2px solid transparent; white-space: nowrap; }}
    .tab-btn.active {{ color: #0a66c2; border-bottom-color: #0a66c2; background: #fff; }}
    .tab-pane {{ padding: 16px 24px; overflow-y: auto; flex: 1; min-height: 200px; }}
    .tab-export-bar {{ margin-bottom: 10px; display: flex; justify-content: flex-end; }}
    .template-box {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
      padding: 14px 16px; font-size: 13px; color: #334155; white-space: pre-wrap;
      line-height: 1.7; font-family: inherit; }}

    /* Post cards */
    .posts-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }}
    .post-card {{ background: #fff; border-radius: 10px; padding: 16px;
                  box-shadow: 0 1px 4px #0002; border: 1px solid #e2e8f0; }}
    .post-header {{ display: flex; justify-content: space-between; align-items: flex-start;
                    margin-bottom: 8px; gap: 8px; flex-wrap: wrap; }}
    .post-author {{ font-weight: 600; font-size: 14px; }}
    .post-meta {{ font-size: 12px; color: #64748b; white-space: nowrap; }}
    .post-excerpt {{ font-size: 13px; color: #334155; line-height: 1.5; margin-bottom: 10px; }}
    .post-footer {{ text-align: right; }}
    .post-link {{ font-size: 13px; font-weight: 600; color: #0a66c2; }}

    /* Notices */
    .notice {{ padding: 12px 16px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; line-height: 1.6; }}
    .notice.warning {{ background: #fef9c3; border: 1px solid #fde047; color: #713f12; }}
    .notice.error   {{ background: #fee2e2; border: 1px solid #fca5a5; color: #7f1d1d; }}
    .notice.info    {{ background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; }}

    .hidden {{ display: none !important; }}
    .server-only {{ display: none; }}
  </style>
</head>
<body>

<div class="header">
  <h1>PM Job Tracker</h1>
  <div class="meta" id="genTime">Generated: {esc(generated_at)}</div>
  <div class="counts">
    <span id="totalJobs">{len(jobs)}</span> job listing{'s' if len(jobs) != 1 else ''}
    &bull;
    <span id="totalPosts">{len(posts)}</span> feed post{'s' if len(posts) != 1 else ''}
  </div>
  <div class="header-row">
    <div class="badges">{badges_html}</div>
    <div style="display:flex;flex-direction:column;align-items:flex-end">
      <button id="refreshBtn" onclick="triggerRefresh()">&#x27F3; Refresh Now</button>
      <div id="refreshStatus"></div>
    </div>
  </div>
</div>

<!-- ═══════════════════ SECTION 1: JOB LISTINGS ══════════════════════ -->
<div class="section">
  <h2>Job Listings</h2>
  <div class="filter-bar">

    <!-- Row 1: search + clear -->
    <div class="search-row">
      <input type="text" id="jobSearch" placeholder="🔍  Search title, company, location…" oninput="filterJobs()"/>
      <button class="clear-btn" onclick="clearAllFilters()">✕ Clear all</button>
    </div>

    <!-- Row 2: chip groups -->
    <div class="filter-row">

      <div class="filter-group">
        <span class="filter-label">Date Posted</span>
        <div class="chips" id="dateChips">
          <button class="chip active" data-val="">All dates</button>
          <button class="chip" data-val="1">Today</button>
          <button class="chip" data-val="3">Last 3 days</button>
          <button class="chip" data-val="7">Last 7 days</button>
        </div>
      </div>

      <div class="filter-group">
        <span class="filter-label">Region</span>
        <div class="chips" id="regionChips">
          <button class="chip active" data-val="">All</button>
          <button class="chip" data-val="usa">🇺🇸 USA</button>
          <button class="chip" data-val="india">🇮🇳 India</button>
          <button class="chip" data-val="singapore">🇸🇬 Singapore</button>
          <button class="chip" data-val="uae">🇦🇪 UAE</button>
          <button class="chip" data-val="uk">🇬🇧 UK</button>
          <button class="chip" data-val="canada">🇨🇦 Canada</button>
          <button class="chip" data-val="remote">🌐 Remote</button>
        </div>
      </div>

      <div class="filter-group">
        <span class="filter-label">City</span>
        <input type="text" class="city-input" id="cityFilter"
               placeholder="e.g. Mumbai" oninput="filterJobs()"/>
      </div>

      <div class="filter-group">
        <span class="filter-label">Title Level</span>
        <div class="chips" id="levelChips">
          <button class="chip active" data-val="">All</button>
          <button class="chip" data-val="pm">PM</button>
          <button class="chip" data-val="senior">Senior PM</button>
          <button class="chip" data-val="principal">Principal PM</button>
          <button class="chip" data-val="group">Group PM</button>
          <button class="chip" data-val="director">Director+</button>
        </div>
      </div>

      <div class="filter-group">
        <span class="filter-label">Source</span>
        <div class="chips" id="sourceChips">
          <button class="chip active" data-val="">All</button>
          <button class="chip" data-val="linkedin">LinkedIn</button>
        </div>
      </div>

      <div class="filter-group">
        <span class="filter-label">Relevance</span>
        <div class="chips" id="relevanceChips">
          <button class="chip active" data-val="">All</button>
          <button class="chip" data-val="75">Strong match (75+)</button>
          <button class="chip" data-val="55">Good match (55+)</button>
        </div>
      </div>

      <div class="filter-group">
        <span class="filter-label">Easy Apply</span>
        <div class="chips" id="easyChips">
          <button class="chip active" data-val="">All</button>
          <button class="chip" data-val="true">⚡ Easy Apply only</button>
          <button class="chip" data-val="false">Regular only</button>
        </div>
      </div>

    </div><!-- filter-row -->
  </div><!-- filter-bar -->
  <div class="count-label" id="jobCount">{len(jobs)} jobs shown</div>
  <div class="table-wrap">
    <table id="jobTable">
      <thead>
        <tr>
          <th onclick="sortTable(0)">Title <span class="arr">⇅</span></th>
          <th onclick="sortTable(1)">Company <span class="arr">⇅</span></th>
          <th onclick="sortTable(2)">Location <span class="arr">⇅</span></th>
          <th onclick="sortTable(3)">Posted <span class="arr">⇅</span></th>
          <th onclick="sortTable(4)">Score <span class="arr">⇅</span></th>
          <th class="no-sort server-only">Details</th>
        </tr>
      </thead>
      <tbody id="jobTbody">
{job_rows}
      </tbody>
    </table>
  </div>
</div>

<!-- ═══════════════════ SECTION 2: FEED POSTS ════════════════════════ -->
<div class="section">
  <h2>LinkedIn Feed Posts (Hiring Announcements)</h2>
  {posts_notice}
  <div class="controls">
    <label>Search:</label>
    <input type="text" class="medium" id="postSearch" placeholder="Keyword in post text…" oninput="filterPosts()"/>
  </div>
  <div class="count-label" id="postCount">{len(posts)} posts shown</div>
  <div class="posts-grid" id="postsGrid">{post_cards}</div>
</div>

<!-- ═══════════════════ DETAILS MODAL ════════════════════════════════ -->
<div id="detailsModal" class="modal-overlay hidden" onclick="closeModalOutside(event)">
  <div class="modal-box">
    <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    <div id="modalHeader" class="modal-header"></div>
    <div class="modal-tabs">
      <button class="tab-btn active" data-tab="hr"       onclick="switchTab('hr',this)">&#128101; HR Contacts</button>
      <button class="tab-btn"        data-tab="product"  onclick="switchTab('product',this)">&#129504; Product Team</button>
      <button class="tab-btn"        data-tab="email"    onclick="switchTab('email',this)">&#9993;&#65039; Email Template</button>
      <button class="tab-btn"        data-tab="linkedin" onclick="switchTab('linkedin',this)">&#128188; LinkedIn Message</button>
    </div>
    <div id="tab-hr"       class="tab-pane"></div>
    <div id="tab-product"  class="tab-pane hidden"></div>
    <div id="tab-email"    class="tab-pane hidden"></div>
    <div id="tab-linkedin" class="tab-pane hidden"></div>
  </div>
</div>

<script>
// ── Server mode detection ─────────────────────────────────────────────────────
const SERVER_MODE = window.location.protocol === 'http:';
const EXPERIENCE_YEARS = {os.getenv("LINKEDIN_EXPERIENCE_YEARS", "4")};

// ── In-memory session caches ──────────────────────────────────────────────────
const contactCache  = new Map();  // key: "company:type"
const templateCache = new Map();  // key: jobUrl

// Show server-only elements when in server mode
if (SERVER_MODE) {{
  document.querySelectorAll('.server-only').forEach(el => el.style.display = '');
}}

// ── Date helpers ──────────────────────────────────────────────────────────────
function daysAgo(n) {{
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}}

// ── Chip helpers ──────────────────────────────────────────────────────────────
function chipVal(groupId) {{
  const a = document.querySelector('#' + groupId + ' .chip.active');
  return a ? a.dataset.val : '';
}}

function setupChipGroup(groupId) {{
  document.querySelectorAll('#' + groupId + ' .chip').forEach(chip => {{
    chip.addEventListener('click', () => {{
      document.querySelectorAll('#' + groupId + ' .chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      filterJobs();
    }});
  }});
}}

// ── Visited jobs (localStorage) ───────────────────────────────────────────────
let visitedJobs = new Set(JSON.parse(localStorage.getItem('visitedJobs') || '[]'));

function markVisited(url) {{
  if (!url) return;
  visitedJobs.add(url);
  localStorage.setItem('visitedJobs', JSON.stringify([...visitedJobs]));
  applyVisited();
}}

function applyVisited() {{
  document.querySelectorAll('#jobTbody tr[data-url]').forEach(r => {{
    r.classList.toggle('visited', visitedJobs.has(r.dataset.url));
  }});
}}

// ── Job filtering ─────────────────────────────────────────────────────────────
function filterJobs() {{
  const q         = document.getElementById('jobSearch').value.toLowerCase().trim();
  const days      = chipVal('dateChips');
  const region    = chipVal('regionChips');
  const city      = document.getElementById('cityFilter').value.toLowerCase().trim();
  const level     = chipVal('levelChips');
  const source    = chipVal('sourceChips');
  const easy      = chipVal('easyChips');
  const relevance = chipVal('relevanceChips');
  const cutoff    = days ? daysAgo(parseInt(days)) : null;
  const minScore  = relevance ? parseInt(relevance) : 0;

  const rows = document.querySelectorAll('#jobTbody tr');
  let shown = 0;
  rows.forEach(r => {{
    const okText      = !q         || r.textContent.toLowerCase().includes(q);
    const okDate      = !cutoff    || (r.dataset.date && r.dataset.date >= cutoff);
    const okRegion    = !region    || r.dataset.region === region;
    const okCity      = !city      || (r.dataset.loc && r.dataset.loc.includes(city));
    const okLevel     = !level     || r.dataset.level  === level;
    const okSource    = !source    || r.dataset.site   === source;
    const okEasy      = !easy      || r.dataset.easy   === easy;
    const okRelevance = !minScore  || parseInt(r.dataset.score || '0') >= minScore;
    const vis = okText && okDate && okRegion && okCity && okLevel && okSource && okEasy && okRelevance;
    r.classList.toggle('hidden', !vis);
    if (vis) shown++;
  }});
  document.getElementById('jobCount').textContent = shown + ' jobs shown';
  applyVisited();
}}

function clearAllFilters() {{
  document.getElementById('jobSearch').value = '';
  document.getElementById('cityFilter').value = '';
  ['dateChips','regionChips','levelChips','sourceChips','easyChips','relevanceChips'].forEach(id => {{
    const chips = document.querySelectorAll('#' + id + ' .chip');
    chips.forEach(c => c.classList.remove('active'));
    if (chips[0]) chips[0].classList.add('active');
  }});
  filterJobs();
}}

// ── Table sorting ──────────────────────────────────────────────────────────────
let sortCol = -1, sortAsc = true;
function sortTable(col) {{
  const tbody = document.getElementById('jobTbody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  if (sortCol === col) {{ sortAsc = !sortAsc; }} else {{ sortCol = col; sortAsc = true; }}
  rows.sort((a, b) => {{
    const at = (a.cells[col]?.textContent || '').trim().toLowerCase();
    const bt = (b.cells[col]?.textContent || '').trim().toLowerCase();
    return sortAsc ? at.localeCompare(bt) : bt.localeCompare(at);
  }});
  document.querySelectorAll('th .arr').forEach(el => el.textContent = '⇅');
  const th = document.querySelectorAll('th')[col];
  if (th) th.querySelector('.arr').textContent = sortAsc ? '↑' : '↓';
  rows.forEach(r => tbody.appendChild(r));
}}

// ── Post filtering ────────────────────────────────────────────────────────────
function filterPosts() {{
  const q = document.getElementById('postSearch').value.toLowerCase();
  const cards = document.querySelectorAll('.post-card');
  let shown = 0;
  cards.forEach(c => {{
    const vis = (c.dataset.text || '').includes(q) || c.textContent.toLowerCase().includes(q);
    c.classList.toggle('hidden', !vis);
    if (vis) shown++;
  }});
  document.getElementById('postCount').textContent = shown + ' posts shown';
}}

// ── Init chip groups ──────────────────────────────────────────────────────────
['dateChips','regionChips','levelChips','sourceChips','easyChips','relevanceChips'].forEach(setupChipGroup);

// Apply visited state on initial page load
applyVisited();


// ── HTML escape helper ────────────────────────────────────────────────────────
function escHtml(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

// ── Details modal ─────────────────────────────────────────────────────────────
function openDetails(btn) {{
  if (!SERVER_MODE) return;
  const url     = btn.dataset.url     || '';
  const title   = btn.dataset.title   || '';
  const company = btn.dataset.company || '';

  // Header
  document.getElementById('modalHeader').innerHTML =
    `<h2>${{escHtml(title)}}</h2>
     <div class="mh-sub">${{escHtml(company)}}</div>
     ${{url ? `<a class="mh-link" href="${{escHtml(url)}}" target="_blank" rel="noopener">View full JD on LinkedIn &#8594;</a>` : ''}}`;

  // Reset tabs to HR active
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', i===0));
  document.querySelectorAll('.tab-pane').forEach((p,i) => p.classList.toggle('hidden', i!==0));

  // Loading state for contact tabs
  document.getElementById('tab-hr').innerHTML      = '<div class="contact-loading">Searching HR contacts&#8230;</div>';
  document.getElementById('tab-product').innerHTML = '<div class="contact-loading">Searching product team&#8230;</div>';

  // Templates — lazy: generated only when the tab is first clicked
  const unloadedHtml = `<div class="contact-loading" style="display:flex;align-items:center;gap:8px">
  <span>&#128161; Click this tab to generate a personalized template using your CV + the JD</span>
</div>`;
  document.getElementById('tab-email').innerHTML    = unloadedHtml;
  document.getElementById('tab-linkedin').innerHTML = unloadedHtml;

  // Store context for lazy fetch (on tab click)
  const modal = document.getElementById('detailsModal');
  modal.dataset.jobUrl  = url;
  modal.dataset.title   = title;
  modal.dataset.company = company;
  modal.dataset.tplDone = '';   // cleared = not yet fetched

  modal.classList.remove('hidden');

  // Fetch both contact types in parallel
  fetchContacts(company, 'hr',      'tab-hr',      title);
  fetchContacts(company, 'product', 'tab-product', title);
}}

async function fetchContacts(company, type, paneId, jobTitle) {{
  try {{
    const cacheKey = company + ':' + type;
    let d = contactCache.get(cacheKey);
    if (!d) {{
      const res = await fetch('/contacts', {{
        method:  'POST',
        headers: {{'Content-Type': 'application/json'}},
        body:    JSON.stringify({{company, type}})
      }});
      d = await res.json();
      if (d.contacts && d.contacts.length) contactCache.set(cacheKey, d);
    }}
    const pane = document.getElementById(paneId);
    if (!pane) return;
    const contacts = d.contacts || [];
    if (d.error && !contacts.length) {{
      pane.innerHTML = `<span style="color:#ef4444">${{escHtml(d.error)}}</span>`; return;
    }}
    if (!contacts.length) {{
      pane.innerHTML = '<span class="contact-loading">No contacts found.</span>'; return;
    }}
    let html = `<div class="tab-export-bar">
      <button class="export-row-btn"
        onclick="exportContacts('${{encodeURIComponent(company)}}','${{type}}')"
        >&#11123; Export to Excel</button>
    </div><div class="contact-grid">`;
    contacts.forEach(c => {{
      const emailRow = c.email
        ? `<div class="contact-email-row">&#9993; ${{escHtml(c.email)}}
             <button class="copy-btn" onclick="copyText('${{c.email.replace(/'/g,"\\'")}}',this)">Copy</button></div>` : '';
      const isConnected   = c.distance === 'F';
      const requestSent   = !!c.request_sent;
      const contactUrn    = escHtml(c.linkedin_url ? c.linkedin_url.split('/in/').pop().replace(/\\/$/,'') : '');
      const connectBtn    = isConnected
        ? `<span style="color:#16a34a;font-size:11px">&#10003; Connected</span>`
        : requestSent
          ? `<span style="color:#d97706;font-size:11px">&#9203; Request sent</span>`
          : `<button class="copy-btn connect-btn"
               data-urn="${{contactUrn}}"
               onclick="sendConnect(this,'${{(jobTitle||'').replace(/'/g,"\\'")}}','${{(company||'').replace(/'/g,"\\'")}}')">
               &#129309; Connect
             </button>`;
      html += `<div class="contact-card">
        <div class="contact-name">${{c.linkedin_url
          ? `<a href="${{escHtml(c.linkedin_url)}}" target="_blank" rel="noopener">${{escHtml(c.name||'Unknown')}}</a>`
          : escHtml(c.name||'Unknown')}}</div>
        <div class="contact-pos">${{escHtml(c.title||'')}}</div>
        ${{emailRow}}
        <div class="contact-card-actions">
          <button class="copy-btn" onclick="copyText('${{(c.linkedin_url||'').replace(/'/g,"\\'")}}',this)">&#128279; URL</button>
          ${{connectBtn}}
        </div>
      </div>`;
    }});
    html += '</div>';
    pane.innerHTML = html;
  }} catch(e) {{
    const pane = document.getElementById(paneId);
    if (pane) pane.innerHTML = '<span style="color:#ef4444">Request failed.</span>';
  }}
}}

function closeModal() {{ document.getElementById('detailsModal').classList.add('hidden'); }}
function closeModalOutside(e) {{ if (e.target.id === 'detailsModal') closeModal(); }}
function switchTab(name, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.add('hidden'));
  document.getElementById('tab-'+name).classList.remove('hidden');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  // Lazy-load templates on first click of email or linkedin tab
  if (name === 'email' || name === 'linkedin') {{
    const modal = document.getElementById('detailsModal');
    if (!modal.dataset.tplDone) {{
      modal.dataset.tplDone = '1';
      fetchTemplates(
        modal.dataset.jobUrl  || '',
        modal.dataset.title   || '',
        modal.dataset.company || ''
      );
    }}
  }}
}}

async function fetchTemplates(jobUrl, title, company) {{
  // Check cache first
  const cached = templateCache.get(jobUrl || (title + ':' + company));
  if (cached) {{
    renderTemplatePane('tab-email',    cached.email    || '');
    renderTemplatePane('tab-linkedin', cached.linkedin || '');
    return;
  }}

  const loadingHtml = `<div class="tab-export-bar">
      <button class="copy-btn" disabled>&#128203; Copy</button>
    </div>
    <div class="contact-loading">Generating personalized template&#8230;</div>`;
  document.getElementById('tab-email').innerHTML    = loadingHtml;
  document.getElementById('tab-linkedin').innerHTML = loadingHtml;

  try {{
    const res = await fetch('/template', {{
      method:  'POST',
      headers: {{'Content-Type': 'application/json'}},
      body:    JSON.stringify({{job_url: jobUrl, title, company}})
    }});
    const d = await res.json();

    if (d.error) {{
      ['tab-email','tab-linkedin'].forEach(id => {{
        const p = document.getElementById(id);
        if (p) p.innerHTML = `<span style="color:#ef4444">${{escHtml(d.error)}}</span>`;
      }});
      return;
    }}
    // Store in cache
    templateCache.set(jobUrl || (title + ':' + company), {{email: d.email || '', linkedin: d.linkedin || ''}});
    renderTemplatePane('tab-email',    d.email    || '');
    renderTemplatePane('tab-linkedin', d.linkedin || '');
  }} catch(e) {{
    ['tab-email','tab-linkedin'].forEach(id => {{
      const p = document.getElementById(id);
      if (p) p.innerHTML = '<span style="color:#ef4444">Template request failed.</span>';
    }});
  }}
}}

function renderTemplatePane(paneId, text) {{
  const pane = document.getElementById(paneId);
  if (!pane) return;
  if (!text) {{
    pane.innerHTML = '<span style="color:#ef4444">Could not generate template.</span>';
    return;
  }}
  pane.innerHTML = `<div class="tab-export-bar">
    <button class="copy-btn"
      onclick="copyText(this.closest('.tab-pane').querySelector('.template-box').textContent,this)">
      &#128203; Copy</button>
  </div>
  <div class="template-box">${{escHtml(text)}}</div>`;
}}

// ── Connect button ────────────────────────────────────────────────────────────
async function sendConnect(btn, jobTitle, company) {{
  const urn = btn.dataset.urn;
  if (!urn) return;
  if (btn.dataset.confirmed) return;

  const yr = EXPERIENCE_YEARS || '4';
  const defaultNote = `Hi, I\'m applying for the ${{jobTitle}} role at ${{company}} and would love to connect! I have ${{yr}} years of PM experience. Thanks!`;
  const noteBox = document.createElement('div');
  noteBox.style.cssText = 'margin-top:6px';
  noteBox.innerHTML = `<textarea class="connect-note" maxlength="300"
    style="width:100%;font-size:11px;border:1px solid #cbd5e1;border-radius:4px;padding:4px;resize:vertical;height:60px"
    >${{escHtml(defaultNote)}}</textarea>
    <button class="copy-btn" style="margin-top:4px;background:#0a66c2;color:#fff;border:none"
      onclick="confirmConnect(this)">Send request</button>`;
  btn.parentNode.appendChild(noteBox);
  btn.dataset.confirmed = '1';
  btn.style.display = 'none';
}}

async function confirmConnect(sendBtn) {{
  const noteBox  = sendBtn.previousElementSibling;
  const note     = noteBox ? noteBox.value.trim() : '';
  const card     = sendBtn.closest('.contact-card');
  const urnBtn   = card ? card.querySelector('.connect-btn') : null;
  const urn      = urnBtn ? urnBtn.dataset.urn : '';
  sendBtn.disabled = true;
  sendBtn.textContent = 'Sending\u2026';
  try {{
    const res = await fetch('/connect', {{
      method:  'POST',
      headers: {{'Content-Type': 'application/json'}},
      body:    JSON.stringify({{urn, note}})
    }});
    const d = await res.json();
    if (d.status === 'ok') {{
      sendBtn.parentNode.innerHTML = '<span style="color:#16a34a;font-size:11px">&#10003; Request sent</span>';
    }} else {{
      sendBtn.textContent = '&#x2717; Failed';
      sendBtn.title = d.error || '';
    }}
  }} catch(e) {{
    sendBtn.textContent = '&#x2717; Error';
  }}
}}

// ── Shared helpers ────────────────────────────────────────────────────────────
function copyText(text, btn) {{
  navigator.clipboard.writeText(text).then(() => {{
    const orig = btn.textContent;
    btn.textContent = '&#10003; Copied';
    setTimeout(() => btn.textContent = orig, 1500);
  }});
}}

function exportContacts(encodedCompany, type) {{
  window.location.href = '/export-contacts?company=' + encodedCompany + '&type=' + (type||'hr');
}}

// ── Refresh button (server mode only) ─────────────────────────────────────────
let _pollTimer = null;

function setupRefresh() {{
  const btn = document.getElementById('refreshBtn');
  if (!SERVER_MODE) {{ btn.style.display = 'none'; }}
}}

async function triggerRefresh() {{
  const btn = document.getElementById('refreshBtn');
  const statusEl = document.getElementById('refreshStatus');
  btn.disabled = true;
  btn.textContent = '⏳ Refreshing…';
  statusEl.textContent = 'Scraping jobs and posts (~5 min)…';
  try {{ await fetch('/refresh', {{ method: 'POST' }}); }} catch(e) {{}}
  _pollTimer = setInterval(async () => {{
    try {{
      const r = await fetch('/refresh-status');
      const data = await r.json();
      if (!data.running) {{
        clearInterval(_pollTimer);
        statusEl.textContent = 'Done! Reloading…';
        setTimeout(() => location.reload(), 1200);
      }}
    }} catch(e) {{}}
  }}, 5000);
}}

setupRefresh();
</script>
</body>
</html>"""


# ── JD-CV Relevance Scoring ───────────────────────────────────────────────────
def score_jobs_relevance(jobs: List[Dict]) -> None:
    """Score each job for CV fit using Claude haiku. Mutates jobs in-place."""
    import os
    try:
        import anthropic
        import pdfplumber
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except ImportError as e:
        logger.warning(f"score_jobs_relevance: missing dependency ({e}) — skipping")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.info("score_jobs_relevance: ANTHROPIC_API_KEY not set — skipping")
        return

    # Load CV text
    cv_text = ""
    cv_path = os.getenv("LINKEDIN_RESUME_PATH", "")
    if cv_path and Path(cv_path).exists():
        try:
            with pdfplumber.open(cv_path) as pdf:
                cv_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            logger.warning(f"Could not read CV for scoring: {e}")

    # Load cache
    data_dir   = BASE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    cache_path = data_dir / "relevance_scores.json"
    cache: Dict[str, Dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            pass

    # Apply cached scores and collect unscored jobs
    unscored = []
    for j in jobs:
        url = j.get("job_url", "")
        if url and url in cache:
            j["relevance_score"] = cache[url].get("score", 0)
            j["relevance_label"] = cache[url].get("label", "")
        else:
            j.setdefault("relevance_score", None)
            j.setdefault("relevance_label", "")
            if url:
                unscored.append(j)

    if not unscored:
        logger.info("score_jobs_relevance: all jobs already cached")
        return

    logger.info(f"score_jobs_relevance: scoring {len(unscored)} jobs in batches of 10…")
    client = anthropic.Anthropic(api_key=api_key)

    def _label(score: int) -> str:
        if score >= 75: return "Strong match"
        if score >= 55: return "Good match"
        if score >= 35: return "Partial match"
        return "Weak match"

    BATCH = 10
    for i in range(0, len(unscored), BATCH):
        batch = unscored[i:i + BATCH]
        jobs_text = ""
        for idx, j in enumerate(batch, 1):
            desc = (j.get("description") or "")[:400]
            jobs_text += f"{idx}. Title: {j.get('title','')} | Company: {j.get('company','')} | Description: {desc}\n"

        prompt = f"""You score job listings for Vishvesh Pandya — PM, 4 yrs exp, Noon/Zomato/IIT KGP.
Specialties: consumer apps, quick-commerce, AI features, growth metrics.

CV SUMMARY: {cv_text[:1500]}

Rate each job 0-100 for CV fit. Return ONLY a JSON array, one object per job, in order:
[{{"score": N, "label": "Strong match|Good match|Partial match|Weak match"}}, ...]

Jobs:
{jobs_text}"""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            # Extract JSON array from response
            start = raw.find("[")
            end   = raw.rfind("]") + 1
            results = json.loads(raw[start:end]) if start != -1 else []
            for idx, j in enumerate(batch):
                if idx < len(results):
                    score = int(results[idx].get("score", 0))
                    label = _label(score)
                    j["relevance_score"] = score
                    j["relevance_label"] = label
                    url = j.get("job_url", "")
                    if url:
                        cache[url] = {"score": score, "label": label}
        except Exception as e:
            logger.warning(f"score_jobs_relevance batch {i//BATCH + 1} failed: {e}")

        # Save cache after each batch (survive crash)
        try:
            cache_path.write_text(json.dumps(cache, indent=2))
        except Exception as e:
            logger.warning(f"Could not save relevance cache: {e}")

    scored = sum(1 for j in jobs if j.get("relevance_score") is not None)
    strong = sum(1 for j in jobs if (j.get("relevance_score") or 0) >= 75)
    logger.info(f"score_jobs_relevance done: {scored} scored, {strong} strong matches")


# ── Scrape + write ────────────────────────────────────────────────────────────
def run_scrape(open_browser: bool = False) -> dict:
    """Run both scrapers, build HTML, write report. Returns summary dict."""
    global _refresh_state
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info("=" * 60)
    logger.info(f"PM Job Tracker starting — {generated_at}")
    logger.info("=" * 60)

    jobs: List[Dict] = []
    job_status: Dict[str, str] = {}
    try:
        from search_jobs import scrape_job_listings
        jobs, job_status = scrape_job_listings()
        logger.info(f"Job listings done: {len(jobs)} results, status={job_status}")
    except Exception as e:
        logger.error(f"search_jobs module failed: {e}", exc_info=True)
        job_status = {"linkedin": "error"}

    posts: List[Dict] = []
    post_status = "error"
    try:
        from search_posts import scrape_feed_posts
        posts, post_status = scrape_feed_posts()
        logger.info(f"Feed posts done: {len(posts)} results, status={post_status}")
    except Exception as e:
        logger.error(f"search_posts module failed: {e}", exc_info=True)
        post_status = "error"

    score_jobs_relevance(jobs)

    html = build_html(jobs, job_status, posts, post_status, generated_at)

    date_str    = datetime.now().strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"jobs_{date_str}.html"
    latest_path = REPORTS_DIR / "jobs_latest.html"
    report_path.write_text(html, encoding="utf-8")
    latest_path.write_text(html, encoding="utf-8")
    logger.info(f"Report written: {report_path}")

    if open_browser:
        webbrowser.open(latest_path.as_uri())

    summary = {
        "last_updated": generated_at,
        "job_count":    len(jobs),
        "post_count":   len(posts),
    }
    with _refresh_lock:
        _refresh_state.update({"running": False, **summary})
    return summary


# ── Local HTTP server ─────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access logs

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path in ("/", "/index.html"):
            latest = REPORTS_DIR / "jobs_latest.html"
            if latest.exists():
                data = latest.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(200, {"error": "No report yet. Click Refresh."})

        elif path == "/refresh-status":
            with _refresh_lock:
                self._json(200, _refresh_state)

        elif path == "/apply-status":
            with _apply_lock_:
                self._json(200, dict(_apply_status))

        elif path == "/export-contacts":
            qs      = parse_qs(parsed.query)
            company = qs.get("company", [""])[0]
            ctype   = qs.get("type", ["hr"])[0]
            self._handle_export_contacts(company, ctype)

        elif path == "/export-all-connections":
            self._handle_export_connections()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/refresh":
            with _refresh_lock:
                if _refresh_state["running"]:
                    self._json(200, {"status": "already_running"})
                    return
                _refresh_state["running"] = True

            def _bg():
                try:
                    run_scrape(open_browser=False)
                except Exception as e:
                    logger.error(f"Background refresh failed: {e}", exc_info=True)
                    with _refresh_lock:
                        _refresh_state["running"] = False

            threading.Thread(target=_bg, daemon=True).start()
            self._json(202, {"status": "started"})

        elif path == "/apply":
            body    = self._read_body()
            job_url = body.get("job_url", "")
            if not job_url:
                self._json(400, {"status": "error", "message": "job_url required"})
                return

            if not _apply_lock.acquire(blocking=False):
                self._json(200, {"status": "busy", "message": "Another apply session is already running"})
                return

            def _apply_bg():
                global _apply_status
                try:
                    with _apply_lock_:
                        _apply_status = {"state": "opening", "message": "Opening browser…"}
                    from apply_jobs import run_easy_apply
                    with _apply_lock_:
                        _apply_status = {"state": "filling", "message": "Filling form…"}
                    result = run_easy_apply(job_url)
                    logger.info(f"Easy Apply result for {job_url}: {result}")
                    if result.get("status") == "review_ready":
                        with _apply_lock_:
                            _apply_status = {"state": "review_ready", "message": "Ready — check your browser to review & submit"}
                    else:
                        with _apply_lock_:
                            _apply_status = {"state": "error", "message": result.get("message", "Unknown error")}
                except Exception as e:
                    logger.error(f"Easy Apply failed: {e}", exc_info=True)
                    with _apply_lock_:
                        _apply_status = {"state": "error", "message": str(e)}
                finally:
                    _apply_lock.release()

            with _apply_lock_:
                _apply_status = {"state": "starting", "message": "Starting…"}
            threading.Thread(target=_apply_bg, daemon=True).start()
            self._json(202, {"status": "started"})

        elif path == "/contacts":
            body    = self._read_body()
            company = body.get("company", "").strip()
            ctype   = body.get("type", "hr").strip()
            if ctype not in ("hr", "product", "all"):
                ctype = "hr"
            if not company:
                self._json(400, {"error": "company required"})
                return
            self._handle_contacts(company, ctype)

        elif path == "/connect":
            body = self._read_body()
            urn  = body.get("urn",  "").strip()
            note = body.get("note", "").strip()[:300]
            if not urn:
                self._json(400, {"error": "urn required"})
                return
            self._handle_connect(urn, note)

        elif path == "/template":
            body    = self._read_body()
            job_url = body.get("job_url", "").strip()
            title   = body.get("title",   "").strip()
            company = body.get("company", "").strip()
            if not title or not company:
                self._json(400, {"error": "title and company required"})
                return
            self._handle_template(job_url, title, company)

        else:
            self.send_response(404)
            self.end_headers()

    def _handle_contacts(self, company: str, contact_type: str = "hr"):
        global _contacts_cache
        try:
            from extract_contacts import search_company_contacts, make_api
            from concurrent.futures import ThreadPoolExecutor, as_completed

            cache_key = f"{company}:{contact_type}"
            with _contacts_cache_lock:
                if cache_key in _contacts_cache:
                    self._json(200, {"contacts": _contacts_cache[cache_key]})
                    return

            api = make_api()

            if contact_type == "all":
                # Parallelise HR and Product searches
                def _fetch_hr():
                    return search_company_contacts(company, "hr", api)
                def _fetch_product():
                    return search_company_contacts(company, "product", api)

                with ThreadPoolExecutor(max_workers=2) as ex:
                    fut_hr  = ex.submit(_fetch_hr)
                    fut_prd = ex.submit(_fetch_product)
                    hr_contacts      = fut_hr.result()
                    product_contacts = fut_prd.result()

                seen = set()
                contacts = []
                for c in hr_contacts + product_contacts:
                    key = c.get("linkedin_url") or c.get("name", "")
                    if key and key not in seen:
                        seen.add(key)
                        contacts.append(c)
            else:
                contacts = search_company_contacts(company, contact_type, api)

            # Cross-reference sent_requests.json
            sent_path = BASE_DIR / "data" / "sent_requests.json"
            sent_urns = {}
            if sent_path.exists():
                try:
                    sent_urns = json.loads(sent_path.read_text())
                except Exception:
                    pass

            for c in contacts:
                urn = (c.get("linkedin_url") or "").rstrip("/").split("/in/")[-1].rstrip("/")
                c["request_sent"] = urn in sent_urns

            with _contacts_cache_lock:
                _contacts_cache[cache_key] = contacts

            self._json(200, {"contacts": contacts})
        except Exception as e:
            logger.error(f"Contact search failed: {e}", exc_info=True)
            self._json(200, {"error": str(e), "contacts": []})

    def _handle_connect(self, urn: str, note: str):
        try:
            from extract_contacts import make_api
            api = make_api()

            # The urn extracted from contact URLs is always the miniProfile ID
            # (e.g. ACoAABcd…), not a vanity slug — so profile_urn always applies.
            # profile_public_id is a required positional arg; pass "" when using profile_urn.
            # Try with message first (fails silently on non-premium) → retry without.
            sent       = False
            last_error = ""
            for msg in ([note, ""] if note else [""]):
                try:
                    result = api.add_connection(profile_public_id="", profile_urn=urn, message=msg)
                    # add_connection returns True on error, False/None on success
                    if not result:
                        sent = True
                        break
                    last_error = "LinkedIn rejected the request (may be rate-limited or not connectable)"
                except Exception as exc:
                    last_error = str(exc)
                    break  # real exception — don't retry with same urn

            if not sent:
                self._json(200, {"status": "error", "error": last_error})
                return

            # Persist sent request
            data_dir  = BASE_DIR / "data"
            data_dir.mkdir(exist_ok=True)
            sent_path = data_dir / "sent_requests.json"
            sent = {}
            if sent_path.exists():
                try:
                    sent = json.loads(sent_path.read_text())
                except Exception:
                    pass
            sent[urn] = datetime.utcnow().isoformat()
            sent_path.write_text(json.dumps(sent, indent=2))

            # Invalidate any cached contacts so request_sent refreshes
            with _contacts_cache_lock:
                keys_to_drop = [k for k in _contacts_cache if True]  # drop all (URN is across companies)
                for k in keys_to_drop:
                    del _contacts_cache[k]

            self._json(200, {"status": "ok"})
        except Exception as e:
            logger.error(f"Connection request failed: {e}", exc_info=True)
            self._json(200, {"status": "error", "error": str(e)})

    def _handle_export_contacts(self, company: str, ctype: str):
        try:
            import os
            from extract_contacts import search_company_contacts, export_to_excel, make_api
            from dotenv import load_dotenv
            load_dotenv(BASE_DIR / ".env")
            export_dir = os.getenv("CONTACTS_EXPORT_DIR", str(Path.home() / "Downloads"))

            cache_key = f"{company}:{ctype}"
            with _contacts_cache_lock:
                contacts = _contacts_cache.get(cache_key)

            if contacts is None:
                api      = make_api()
                contacts = search_company_contacts(company, ctype, api)
                with _contacts_cache_lock:
                    _contacts_cache[cache_key] = contacts

            safe     = "".join(c if c.isalnum() or c in " _-" else "_" for c in company)
            path     = Path(export_dir) / f"contacts_{safe}.xlsx"
            export_to_excel(contacts, str(path))

            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             f'attachment; filename="contacts_{safe}.xlsx"')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            logger.error(f"Export contacts failed: {e}", exc_info=True)
            self._json(500, {"error": str(e)})

    def _handle_export_connections(self):
        try:
            import os
            from extract_contacts import get_connections, export_to_excel, make_api
            from dotenv import load_dotenv
            load_dotenv(BASE_DIR / ".env")
            export_dir = os.getenv("CONTACTS_EXPORT_DIR", str(Path.home() / "Downloads"))
            api         = make_api()
            connections = get_connections(api)
            path        = Path(export_dir) / "linkedin_connections.xlsx"
            export_to_excel(connections, str(path))

            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             'attachment; filename="linkedin_connections.xlsx"')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            logger.error(f"Export connections failed: {e}", exc_info=True)
            self._json(500, {"error": str(e)})

    def _handle_template(self, job_url: str, title: str, company: str):
        try:
            import anthropic
            import pdfplumber
            from dotenv import load_dotenv
            load_dotenv(BASE_DIR / ".env")

            # 1. Fetch JD via linkedin-api get_job()
            jd_text = ""
            try:
                from extract_contacts import make_api
                api = make_api()
                job_id = job_url.rstrip("/").split("/")[-1]
                if job_id.isdigit():
                    job = api.get_job(job_id) or {}
                    desc = job.get("description", {})
                    jd_text = desc.get("text", "") if isinstance(desc, dict) else str(desc)
            except Exception as e:
                logger.warning(f"Could not fetch JD: {e}")

            # 2. Read CV PDF
            cv_text = ""
            cv_path = os.getenv("LINKEDIN_RESUME_PATH", "")
            if cv_path and Path(cv_path).exists():
                try:
                    with pdfplumber.open(cv_path) as pdf:
                        cv_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                except Exception as e:
                    logger.warning(f"Could not read CV: {e}")

            # 3. Call Claude
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            if not api_key:
                self._json(200, {"error": "ANTHROPIC_API_KEY not set in .env"})
                return

            client = anthropic.Anthropic(api_key=api_key)
            prompt = f"""You are helping Vishvesh Pandya, a Product Manager, write personalized job outreach messages.

CANDIDATE PROFILE (fixed — do not change these facts):
- Name: Vishvesh Pandya
- Experience: 4 years in Product Management
- Current role: Leading consumer product charter at Noon Minutes (largest quick-commerce in UAE)
- Background: Ex-Zomato, IIT KGP
- Key achievements to reference when relevant to JD:
  * AI shopping assistant → 90% faster cart creation
  * +39% conversion for dormant users through time-sensitive offers
  * +5% AOV through cart recommendations

TARGET JOB:
Title: {title}
Company: {company}

JOB DESCRIPTION:
{jd_text[:4000] if jd_text else "(not available)"}

CANDIDATE CV:
{cv_text[:3000] if cv_text else "(not available)"}

TASK — Generate two messages:

1. EMAIL (cold outreach to HR/recruiter/PM leader):
   Style reference (match this voice exactly):
   ---
   Hey [Name],

   Hope you're doing well!

   I'm Vishvesh - a PM with 4 years of experience, currently leading [relevant context from JD match].

   Recently shipped things relevant to your role:
   [Pick 2–3 achievements from the candidate profile that DIRECTLY match JD requirements — be specific]

   I came across [specific thing about this role/company from JD] and am genuinely excited about [specific problem they are solving].

   Sharing my CV here - would really appreciate the chance to connect.
   ---
   Rules:
   - Subject line: short, specific, no buzzwords
   - Body under 150 words
   - Reference specific JD requirements by name (not generic "your role")
   - Match each achievement to a JD need — don't list achievements that don't connect
   - End naturally (no "Best regards", no formal sign-off)
   - Use [Name] placeholder

2. LINKEDIN MESSAGE (sent to already-connected people as a DM — not a connection request):
   - No character limit — treat it like a short message, not a note
   - 3–5 sentences: who Vishvesh is, why THIS specific role/company is interesting, 1–2 relevant achievements, ask for a quick chat
   - Warm and conversational — like a message to a colleague
   - Reference something specific from the JD so it doesn't feel templated
   - No filler openers ("Hope this finds you well", "I came across your profile")

Return ONLY valid JSON:
{{"email": "Subject: ...\\n\\nHey [Name],\\n\\n...", "linkedin": "..."}}"""

            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = message.content[0].text.strip()
            import re
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                self._json(200, {"email": data.get("email", ""), "linkedin": data.get("linkedin", "")})
            else:
                self._json(200, {"error": "Could not parse AI response", "raw": raw})
        except Exception as e:
            logger.error(f"Template generation failed: {e}", exc_info=True)
            self._json(200, {"error": str(e)})

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def serve_dashboard():
    """Start local HTTP server and open browser."""
    server = ThreadingHTTPServer(("", PORT), _Handler)
    url = f"http://localhost:{PORT}"
    logger.info(f"Dashboard server running at {url}  (Ctrl+C to stop)")
    # Run scrape in background so the server is immediately responsive
    threading.Thread(target=run_scrape, kwargs={"open_browser": False}, daemon=True).start()
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PM Job Tracker")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local web server with a Refresh button (recommended for interactive use)"
    )
    args = parser.parse_args()

    if args.serve:
        serve_dashboard()
    else:
        run_scrape(open_browser=True)


if __name__ == "__main__":
    main()
