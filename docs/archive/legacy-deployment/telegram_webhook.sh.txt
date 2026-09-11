#!/bin/bash
# =============================================================================
# Telegram Webhook Registration
# =============================================================================
# Registers the Telegram webhook to point to your Fly.io deployment.
# =============================================================================

set -euo pipefail

APP_NAME="${MYLOWARE_APP_NAME:-myloware-api}"

# Get bot token from .env or environment
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    echo "Set it in .env or export TELEGRAM_BOT_TOKEN=your_token"
    exit 1
fi

WEBHOOK_URL="https://${APP_NAME}.fly.dev/v1/telegram/webhook"

echo "=== Telegram Webhook Registration ==="
echo "Webhook URL: $WEBHOOK_URL"
echo ""

# Set webhook
echo "Setting webhook..."
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}" | jq .

# Verify webhook
echo ""
echo "Verifying webhook..."
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | jq .

echo ""
echo "=== Webhook Registration Complete ==="
