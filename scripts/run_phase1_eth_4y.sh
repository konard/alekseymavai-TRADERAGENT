#!/bin/bash
# Phase 1: ETH/USDT 4-year backtest with recovery + hardware monitoring
# Запуск: bash scripts/run_phase1_eth_4y.sh

set -e
cd "$(dirname "$0")/.."

SYMBOL="ETHUSDT"
MAX_BARS=420480    # 4 года × 365.25 × 24 × 12 баров/час
OUTPUT_DIR="results/phase1_eth_4y_recovery"
LOG_FILE="$OUTPUT_DIR/run.log"
HW_LOG="$OUTPUT_DIR/hw_monitor.log"

mkdir -p "$OUTPUT_DIR"

# Load Telegram credentials
source .env 2>/dev/null || true
TG_TOKEN="${TELEGRAM_BOT_TOKEN:-$TG_TOKEN}"
TG_CHAT="${TELEGRAM_CHAT_ID:-$TG_CHAT_ID}"

tg_send() {
    if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
            -d chat_id="$TG_CHAT" \
            -d parse_mode="HTML" \
            -d text="$1" > /dev/null 2>&1 || true
    fi
}

hw_status() {
    local cpu_pct=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}')
    local mem=$(free -h | awk '/Mem:/{printf "%s/%s (%.0f%%)", $3, $2, $3/$2*100}')
    local load=$(cat /proc/loadavg | awk '{print $1, $2, $3}')
    local disk=$(df -h / | awk 'NR==2{print $5 " used"}')
    local temp=""
    if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
        local t=$(cat /sys/class/thermal/thermal_zone0/temp)
        temp="🌡 CPU: $((t/1000))°C"
    fi
    echo "💻 CPU: ${cpu_pct}% | Load: ${load}
🧠 RAM: ${mem}
💾 Disk: ${disk} ${temp}"
}

# Kill any previous Phase 1 runs
pkill -f "run_backtest_v2.*ETHUSDT" 2>/dev/null || true
sleep 1

tg_send "🚀 <b>Phase 1 ETH/USDT 4Y + Recovery запускается</b>
Баров: ~${MAX_BARS}
Balance: \$1,000
Recovery: ✅ enabled
$(hw_status)"

echo "[$(date)] Starting Phase 1 ETH/USDT 4Y with recovery" | tee "$HW_LOG"

# Start backtest in background
.venv/bin/python scripts/run_backtest_v2.py \
    --mode single \
    --symbol "$SYMBOL" \
    --config configs/backtest_phase1_recovery.yaml \
    --data-dir data/historical \
    --max-bars "$MAX_BARS" \
    --initial-balance 1000 \
    --output-dir "$OUTPUT_DIR" \
    > "$LOG_FILE" 2>&1 &

BT_PID=$!
echo "[$(date)] Backtest PID=$BT_PID" | tee -a "$HW_LOG"

# Hardware monitor loop — every 5 minutes
INTERVAL=300
COUNTER=0
while kill -0 $BT_PID 2>/dev/null; do
    sleep $INTERVAL
    COUNTER=$((COUNTER + 1))
    ELAPSED_MIN=$((COUNTER * INTERVAL / 60))

    HW=$(hw_status)

    # Get latest progress from log
    LAST_PROGRESS=$(grep -o 'progress: [0-9.]*%.*' "$LOG_FILE" 2>/dev/null | tail -1)
    PORTFOLIO=$(grep -oP 'portfolio=\$[\d,.]+' "$LOG_FILE" 2>/dev/null | tail -1)

    MSG="⏱ <b>HW Monitor — ${ELAPSED_MIN}m</b>
${HW}
📈 ${LAST_PROGRESS:-waiting...}
${PORTFOLIO:-}"

    echo "[$(date)] $HW | $LAST_PROGRESS" >> "$HW_LOG"
    tg_send "$MSG"
done

# Backtest finished
wait $BT_PID
EXIT_CODE=$?

FINAL_PROGRESS=$(grep -o 'progress: [0-9.]*%.*' "$LOG_FILE" 2>/dev/null | tail -1)
FINAL_PORTFOLIO=$(grep -oP 'portfolio=\$[\d,.]+' "$LOG_FILE" 2>/dev/null | tail -1)

if [ $EXIT_CODE -eq 0 ]; then
    tg_send "✅ <b>Phase 1 ETH/USDT 4Y ЗАВЕРШЁН</b>
Exit code: $EXIT_CODE
${FINAL_PROGRESS:-done}
${FINAL_PORTFOLIO:-}
$(hw_status)
📁 Результаты: $OUTPUT_DIR/"
else
    LAST_ERR=$(tail -5 "$LOG_FILE" | head -3)
    tg_send "❌ <b>Phase 1 ETH/USDT 4Y ОШИБКА</b>
Exit code: $EXIT_CODE
<pre>$LAST_ERR</pre>
$(hw_status)"
fi

echo "[$(date)] Finished with exit code $EXIT_CODE" | tee -a "$HW_LOG"
