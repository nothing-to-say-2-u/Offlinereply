# Telegram Offline Userbot

A Telegram userbot to auto-reply with a custom offline message when you're mentioned, replied to, or messaged in DMs.

## Features
- `/offline <msg>` — Set offline mode with a custom message
- `/online` — Go back online (stop auto-replies)
- `/getmentions` — Get last 10 logged mentions/messages

## Deploy on Render
1. Create a new **Web Service** on [Render](https://render.com/)
2. Use this repo or upload files
3. Add environment variables:
   - `API_ID`
   - `API_HASH`
   - `SESSION` (your exported string session)
   - `OWNER_ID` (your numeric user ID)

4. Set the **Start Command** to:
```bash
uvicorn main:app --host 0.0.0.0 --port 10000
