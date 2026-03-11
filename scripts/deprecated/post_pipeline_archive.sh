#!/usr/bin/env bash
# Post-pipeline archive script.
# Waits for the pipeline process to finish, then:
#   1. Archives all results + logs
#   2. Commits and pushes results to GitHub
#   3. Copies archive to production server
#   4. Prepares server for safe shutdown
#
# Usage: nohup bash scripts/post_pipeline_archive.sh <PIPELINE_PID> > /tmp/archive.log 2>&1 &

set -euo pipefail
cd ~/TRADERAGENT

PIPELINE_PID="${1:?Usage: $0 <pipeline_pid>}"
PROD_SERVER="ai-agent@185.233.200.13"
ARCHIVE_NAME="pipeline_results_$(date +%Y%m%d_%H%M%S).tar.gz"
ARCHIVE_PATH="/tmp/${ARCHIVE_NAME}"
RESULTS_DIR="data/backtest_results"

# ---------- Telegram ----------
source_env() {
    if [ -f .env ]; then
        export $(grep -v '^#' .env | xargs)
    fi
}
source_env

tg_send() {
    local msg="$1"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="${msg}" \
            -d parse_mode=Markdown > /dev/null 2>&1 || true
    fi
    echo "[TG] ${msg}"
}

# ---------- 1. Wait for pipeline ----------
echo "$(date '+%H:%M:%S') Waiting for pipeline PID ${PIPELINE_PID} to finish..."
tg_send "⏳ Archive script started. Waiting for pipeline PID ${PIPELINE_PID}..."

while kill -0 "$PIPELINE_PID" 2>/dev/null; do
    sleep 30
done

echo "$(date '+%H:%M:%S') Pipeline finished!"
tg_send "✅ Pipeline finished! Starting archive process..."

# ---------- 2. Collect files to archive ----------
echo "$(date '+%H:%M:%S') Collecting files..."

FILES_TO_ARCHIVE=""

# Results JSONs
for f in ${RESULTS_DIR}/phase*.json ${RESULTS_DIR}/final_report.json \
         ${RESULTS_DIR}/regime_routing_table.json ${RESULTS_DIR}/pipeline_errors.json; do
    [ -f "$f" ] && FILES_TO_ARCHIVE="${FILES_TO_ARCHIVE} $f"
done

# Pipeline logs
for f in ${RESULTS_DIR}/pipeline_*.log; do
    [ -f "$f" ] && FILES_TO_ARCHIVE="${FILES_TO_ARCHIVE} $f"
done

# stdout log
[ -f /tmp/pipeline_stdout.log ] && cp /tmp/pipeline_stdout.log ${RESULTS_DIR}/pipeline_stdout.log
FILES_TO_ARCHIVE="${FILES_TO_ARCHIVE} ${RESULTS_DIR}/pipeline_stdout.log"

# Pipeline script (for reference)
FILES_TO_ARCHIVE="${FILES_TO_ARCHIVE} scripts/run_dca_tf_smc_pipeline.py"

echo "Files to archive:"
echo "${FILES_TO_ARCHIVE}" | tr ' ' '\n' | grep -v '^$'

# ---------- 3. Create archive ----------
echo "$(date '+%H:%M:%S') Creating archive: ${ARCHIVE_PATH}"
tar czf "${ARCHIVE_PATH}" ${FILES_TO_ARCHIVE}
ARCHIVE_SIZE=$(du -h "${ARCHIVE_PATH}" | cut -f1)
echo "Archive size: ${ARCHIVE_SIZE}"

# ---------- 4. Commit and push to GitHub ----------
echo "$(date '+%H:%M:%S') Committing results to GitHub..."
git add ${RESULTS_DIR}/*.json ${RESULTS_DIR}/pipeline_stdout.log 2>/dev/null || true
git add ${RESULTS_DIR}/pipeline_*.log 2>/dev/null || true

if git diff --cached --quiet 2>/dev/null; then
    echo "No new files to commit."
else
    git commit -m "data: add pipeline Phase 2-5 results and logs

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
    git push origin main
    echo "Pushed to GitHub."
fi
tg_send "📦 Results committed to GitHub."

# ---------- 5. Copy to production server ----------
echo "$(date '+%H:%M:%S') Copying archive to production server..."
scp -o ConnectTimeout=10 "${ARCHIVE_PATH}" "${PROD_SERVER}:/tmp/${ARCHIVE_NAME}"

# Extract on production server
ssh -o ConnectTimeout=10 "${PROD_SERVER}" "
    mkdir -p ~/TRADERAGENT/data/backtest_results_yandex
    cd ~/TRADERAGENT
    tar xzf /tmp/${ARCHIVE_NAME} -C data/backtest_results_yandex/ --strip-components=0
    echo 'Files extracted:'
    ls -lh data/backtest_results_yandex/data/backtest_results/ 2>/dev/null || ls -lh data/backtest_results_yandex/ 2>/dev/null
    rm -f /tmp/${ARCHIVE_NAME}
"

echo "$(date '+%H:%M:%S') Copied to production server."
tg_send "📤 Results copied to production server (185.233.200.13)."

# ---------- 6. Summary ----------
echo ""
echo "============================================================"
echo "  ARCHIVE COMPLETE"
echo "============================================================"
echo "  Archive: ${ARCHIVE_PATH} (${ARCHIVE_SIZE})"
echo "  GitHub:  pushed to main"
echo "  Prod:    ~/TRADERAGENT/data/backtest_results_yandex/"
echo ""
echo "  Results files:"
ls -lh ${RESULTS_DIR}/*.json 2>/dev/null
echo ""
echo "  Server is safe to shut down."
echo "============================================================"

tg_send "🏁 *Archive complete!*
📦 Archive: ${ARCHIVE_SIZE}
📂 GitHub: pushed
📤 Prod server: copied
🔒 Server safe to shut down for 1 hour."

echo "$(date '+%H:%M:%S') Done. Server can be safely shut down."
