#!/usr/bin/env bash
# One-time Telegram setup: asks for the bot token (hidden), finds the chat ids of
# everyone who pressed Start in the bot, saves both as GitHub secrets, sends a test.
set -euo pipefail
REPO="KhaledMohamed08/translate-job-finder"
export GH_TOKEN="$(gh auth token --user KhaledMohamed08)"

read -rsp "Paste the bot token from @BotFather (hidden): " TOKEN; echo
[[ "$TOKEN" == *:* ]] || { echo "That doesn't look like a bot token."; exit 1; }

UPDATES="$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getUpdates")" || { echo "Telegram rejected the token."; exit 1; }
CHATS="$(python3 -c '
import json, sys
d = json.loads(sys.argv[1])
seen = {}
for u in d.get("result", []):
    m = u.get("message") or u.get("my_chat_member") or {}
    c = m.get("chat") or {}
    if c.get("id"):
        seen[c["id"]] = c.get("first_name") or c.get("title") or ""
for i, n in seen.items():
    print(f"{i}\t{n}")
' "$UPDATES")"

if [[ -z "$CHATS" ]]; then
  echo "No chats found. Each person must open the bot in Telegram and press Start (or send any message), then run this again."
  exit 1
fi
echo "Found these chats:"; echo "$CHATS" | sed 's/^/  /'
IDS="$(echo "$CHATS" | cut -f1 | paste -sd, -)"

printf '%s' "$TOKEN" | gh secret set TELEGRAM_BOT_TOKEN -R "$REPO"
printf '%s' "$IDS"   | gh secret set TELEGRAM_CHAT_ID   -R "$REPO"
echo "Saved to GitHub."

for id in ${IDS//,/ }; do
  curl -fsS -o /dev/null "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${id}" \
    --data-urlencode "text=✅ Weam Job Radar is connected. New job matches will arrive here every morning.
📱 Dashboard: https://khaledmohamed08.github.io/translate-job-finder/" && echo "Test message sent to ${id}"
done
