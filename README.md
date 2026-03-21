# PM Job Tracker

A personal dashboard that scrapes fresh PM job listings from LinkedIn every morning, scores them against your CV using Claude AI, and lets you find and reach out to the right contacts — all from a local web UI.

![Dashboard](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Daily scrape** — pulls PM job listings from LinkedIn (configurable search terms + locations)
- **Easy Apply detection** — verifies Easy Apply status via LinkedIn Voyager API
- **JD-CV relevance scoring** — Claude AI scores each job 0–100 against your resume
- **Contact finder** — finds HR and Product team contacts at each company via LinkedIn
- **AI outreach templates** — generates a personalised cold email + LinkedIn DM for each role
- **One-click connect** — sends LinkedIn connection requests with a custom note
- **Visited job tracking** — grays out jobs you've already opened (persisted in localStorage)
- **Filter chips** — filter by region, seniority, Easy Apply, relevance score, date posted
- **Auto-refresh** — runs at 8 AM daily; also runs on Mac wake if the morning scrape was missed

## Stack

- Python 3.10+ / standard library HTTP server
- [python-jobspy](https://github.com/Bunsly/JobSpy) for LinkedIn scraping
- [linkedin-api](https://github.com/tomquirk/linkedin-api) for contact search + Easy Apply verification
- [Anthropic Claude](https://anthropic.com) for relevance scoring and outreach templates
- pdfplumber for CV parsing
- Vanilla JS + HTML (no frontend build step)

## Quick Start (Docker — recommended)

The easiest way to run this. No Python setup, no dependency headaches.

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop)

```bash
git clone https://github.com/vishvesh245/pm-job-tracker.git
cd pm-job-tracker
./setup.sh
```

The setup script walks you through everything step by step — LinkedIn cookie, Anthropic API key, and resume — then starts the dashboard automatically.

Open **http://localhost:5050** once it's running.

```bash
# Stop the tracker
docker stop pm-job-tracker

# Start it again later
docker start pm-job-tracker
```

---

## Manual Setup (without Docker)

### 1. Clone and install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/job-tracker.git
cd job-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials (see .env.example for details)
```

Required:
- `LINKEDIN_LI_AT` — your LinkedIn `li_at` session cookie ([how to get it](https://github.com/tomquirk/linkedin-api#how-do-i-get-a-li_at-cookie))

Optional but recommended:
- `ANTHROPIC_API_KEY` — enables JD-CV scoring and AI outreach templates
- `LINKEDIN_RESUME_PATH` — path to your resume PDF (needed for scoring)

### 3. Run

```bash
source .venv/bin/activate
python3 build_report.py --serve
```

Open **http://localhost:5050** in your browser. Click **Refresh Now** to run the first scrape.

## Auto-start on macOS (optional)

To have the server start automatically on login and scrape every morning at 8 AM:

```bash
# 1. Copy and personalise the launchd plists
cp setup/launchd/com.jobtracker.server.plist ~/Library/LaunchAgents/
cp setup/launchd/com.jobtracker.refresh.plist ~/Library/LaunchAgents/

# Edit both files — replace YOUR_USERNAME with your macOS username
sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Library/LaunchAgents/com.jobtracker.server.plist
sed -i '' "s/YOUR_USERNAME/$USER/g" ~/Library/LaunchAgents/com.jobtracker.refresh.plist

# 2. Copy the refresh script
mkdir -p scripts logs data
cp setup/launchd/maybe_refresh.sh scripts/maybe_refresh.sh
chmod +x scripts/maybe_refresh.sh

# 3. Load both services
launchctl load ~/Library/LaunchAgents/com.jobtracker.server.plist
launchctl load ~/Library/LaunchAgents/com.jobtracker.refresh.plist
```

The scraper runs once per day. If your Mac was sleeping at 8 AM it runs automatically on next wake.

## Project structure

```
job-tracker/
├── build_report.py      # Main entry point: server, HTML builder, AI scoring
├── search_jobs.py       # LinkedIn job scraper (python-jobspy + Voyager API)
├── search_posts.py      # LinkedIn feed scraper (hiring announcements)
├── extract_contacts.py  # Contact search + LinkedIn connection requests
├── apply_jobs.py        # Selenium-based Easy Apply automation
├── setup/launchd/       # macOS LaunchAgent templates for auto-start + cron
├── .env.example         # Environment variable reference
└── requirements.txt
```

## Notes

- **LinkedIn rate limits** — the scraper adds polite delays between requests. Don't reduce them.
- **li_at expiry** — LinkedIn session cookies expire periodically. Update `.env` when scraping stops working.
- **Cloud deployment** — not recommended. LinkedIn blocks most cloud provider IPs for scraping. Run locally.

## License

MIT
