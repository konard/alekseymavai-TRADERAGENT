#!/bin/bash
# ============================================================
# Phase 7.3: Start Bybit Demo Trading Bot
# ============================================================
# Usage:
#   ./scripts/start_demo.sh              # Foreground mode
#   ./scripts/start_demo.sh --background # Background mode
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="configs/phase7_demo.yaml"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/demo.pid"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "============================================================"
echo "  TRADERAGENT Phase 7.3 — Bybit Demo Trading"
echo "============================================================"
echo ""

# Change to project directory
cd "$PROJECT_DIR"

# Source .env if exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo -e "${GREEN}[OK]${NC} Loaded .env"
else
    echo -e "${RED}[ERROR]${NC} .env file not found!"
    exit 1
fi

# Check required environment variables
if [ -z "$DATABASE_URL" ]; then
    echo -e "${RED}[ERROR]${NC} DATABASE_URL not set"
    exit 1
fi

if [ -z "$ENCRYPTION_KEY" ]; then
    echo -e "${RED}[ERROR]${NC} ENCRYPTION_KEY not set"
    exit 1
fi

echo -e "${GREEN}[OK]${NC} Environment variables loaded"

# Check config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}[ERROR]${NC} Config file not found: $CONFIG_FILE"
    exit 1
fi
echo -e "${GREEN}[OK]${NC} Config file: $CONFIG_FILE"

# Create log directory
mkdir -p "$LOG_DIR"

# Run validation
echo ""
echo "Running pre-deployment validation..."
echo "------------------------------------------------------------"
python scripts/validate_demo.py --config "$CONFIG_FILE"
VALIDATION_EXIT=$?

if [ $VALIDATION_EXIT -ne 0 ]; then
    echo ""
    echo -e "${RED}Validation failed! Fix issues before starting.${NC}"
    exit 1
fi

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo ""
        echo -e "${YELLOW}[WARN]${NC} Bot already running with PID $OLD_PID"
        echo "Stop it first with: kill $OLD_PID"
        exit 1
    else
        rm -f "$PID_FILE"
    fi
fi

# Set config path
export CONFIG_PATH="$CONFIG_FILE"

# Generate log filename
LOG_FILE="$LOG_DIR/demo_$(date +%Y%m%d_%H%M%S).log"

echo ""
echo "============================================================"
echo "  Starting bot..."
echo "  Config: $CONFIG_FILE"
echo "  Log:    $LOG_FILE"
echo "============================================================"
echo ""

# Start bot
if [ "$1" = "--background" ]; then
    nohup python -m bot.main > "$LOG_FILE" 2>&1 &
    BOT_PID=$!
    echo "$BOT_PID" > "$PID_FILE"
    echo -e "${GREEN}Bot started in background${NC}"
    echo "  PID:  $BOT_PID"
    echo "  Log:  $LOG_FILE"
    echo "  Stop: kill $BOT_PID"
    echo ""
    echo "Monitor with: tail -f $LOG_FILE"
else
    echo "Starting in foreground (Ctrl+C to stop)..."
    echo ""
    python -m bot.main 2>&1 | tee "$LOG_FILE"
fi
