#!/bin/bash

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║        PM Job Tracker — Setup            ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Step 1: LinkedIn li_at cookie ─────────────────────────────────────────
echo "STEP 1: LinkedIn Cookie"
echo "────────────────────────"
echo "To get your li_at cookie:"
echo "  1. Open Chrome and log into linkedin.com"
echo "  2. Press F12 (or Cmd+Option+I on Mac) to open DevTools"
echo "  3. Go to: Application → Cookies → https://www.linkedin.com"
echo "  4. Find 'li_at' and copy its Value"
echo ""
read -p "Paste your li_at cookie here: " LI_AT
echo ""

# ── Step 2: Anthropic API Key ─────────────────────────────────────────────
echo "STEP 2: Anthropic API Key (for AI scoring & outreach templates)"
echo "────────────────────────────────────────────────────────────────"
echo "Get your key at: https://console.anthropic.com"
echo "(Press Enter to skip — scoring and templates will be disabled)"
echo ""
read -p "Paste your Anthropic API key: " ANTHROPIC_KEY
echo ""

# ── Step 3: Resume PDF ────────────────────────────────────────────────────
echo "STEP 3: Your Resume PDF"
echo "────────────────────────"
echo "Drag and drop your resume PDF into this terminal window, then press Enter."
echo "(Press Enter to skip — AI scoring will be disabled)"
echo ""
read -p "Resume path: " RESUME_PATH
# Strip quotes if user drags file in (macOS adds quotes for paths with spaces)
RESUME_PATH="${RESUME_PATH//\'/}"
RESUME_PATH="${RESUME_PATH//\"/}"
RESUME_PATH=$(echo "$RESUME_PATH" | xargs)  # trim whitespace
echo ""

# ── Step 4: Years of experience ──────────────────────────────────────────
read -p "How many years of PM experience do you have? (default: 4): " EXP_YEARS
EXP_YEARS="${EXP_YEARS:-4}"
echo ""

# ── Write .env file ───────────────────────────────────────────────────────
cat > .env <<EOF
LINKEDIN_LI_AT=${LI_AT}
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
LINKEDIN_RESUME_PATH=/resume/resume.pdf
LINKEDIN_EXPERIENCE_YEARS=${EXP_YEARS}
CONTACTS_EXPORT_DIR=/app/exports
EOF

echo "✓ Config saved to .env"
echo ""

# ── Build resume mount flag ───────────────────────────────────────────────
RESUME_MOUNT=""
if [ -n "$RESUME_PATH" ] && [ -f "$RESUME_PATH" ]; then
    RESUME_MOUNT="-v \"${RESUME_PATH}:/resume/resume.pdf\""
    echo "✓ Resume found: $RESUME_PATH"
else
    echo "⚠ No resume provided — AI scoring will be limited"
fi
echo ""

# ── Pull and run Docker image ─────────────────────────────────────────────
echo "Starting PM Job Tracker..."
echo ""

DOCKER_CMD="docker run -d \
  --name pm-job-tracker \
  -p 5050:5050 \
  --env-file .env"

if [ -n "$RESUME_PATH" ] && [ -f "$RESUME_PATH" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"${RESUME_PATH}:/resume/resume.pdf\""
fi

DOCKER_CMD="$DOCKER_CMD vishvesh245/pm-job-tracker"

eval $DOCKER_CMD

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  ✓ PM Job Tracker is running!            ║"
echo "║  Open: http://localhost:5050             ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "To stop:   docker stop pm-job-tracker"
echo "To restart: docker start pm-job-tracker"
echo ""
