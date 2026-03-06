#!/bin/bash
# Trigger a refresh if it hasn't already run today and it's 8 AM or later.
# Copy this file to ~/job-tracker/scripts/maybe_refresh.sh and chmod +x it.

INSTALL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SENTINEL="$INSTALL_DIR/data/last_refresh_date.txt"
TODAY=$(date +%Y-%m-%d)
HOUR=$(date +%H)

# Don't run before 8 AM (handles early-morning logins)
if [ "$HOUR" -lt 8 ]; then
    exit 0
fi

# Skip if already refreshed today
if [ -f "$SENTINEL" ] && [ "$(cat "$SENTINEL")" = "$TODAY" ]; then
    exit 0
fi

# Wait up to 30s for the server to be ready (it may still be starting)
for i in $(seq 1 6); do
    if /usr/bin/curl -sf http://localhost:5050/refresh-status > /dev/null 2>&1; then
        break
    fi
    sleep 5
done

# Trigger refresh and record today's date
mkdir -p "$INSTALL_DIR/data"
echo "$TODAY" > "$SENTINEL"
/usr/bin/curl -s -X POST http://localhost:5050/refresh \
    >> "$INSTALL_DIR/logs/cron.log" 2>&1

echo "$(date): refresh triggered" >> "$INSTALL_DIR/logs/cron.log"
