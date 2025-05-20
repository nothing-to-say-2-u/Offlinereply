import json
from fastapi import FastAPI
from telethon import TelegramClient, events
import os
import asyncio

# ====== Config ======
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION")  # Exported string session
BOT_OWNER_ID = int(os.getenv("OWNER_ID"))  # Your user ID

# ====== JSON Handling ======
DATA_FILE = "data.json"

def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ====== Userbot Setup ======
client = TelegramClient(SESSION, API_ID, API_HASH)
app = FastAPI()

@app.get("/")
def root():
    return {"status": "Userbot is alive!"}

# ====== Bot Logic ======
@client.on(events.NewMessage(from_users=BOT_OWNER_ID, pattern=r"/offline (.+)"))
async def set_offline(event):
    msg = event.pattern_match.group(1)
    data = load_data()
    data["offline"] = True
    data["message"] = msg
    save_data(data)
    await event.reply("Offline mode activated.")

@client.on(events.NewMessage(from_users=BOT_OWNER_ID, pattern=r"/online"))
async def set_online(event):
    data = load_data()
    data["offline"] = False
    save_data(data)
    await event.reply("Back online!")

@client.on(events.NewMessage(from_users=BOT_OWNER_ID, pattern=r"/getmentions"))
async def send_logs(event):
    data = load_data()
    logs = data.get("logs", [])
    if not logs:
        await event.reply("No mentions or messages found.")
    else:
        text = "\n\n".join([f"From: {l['from']}\nText: {l['text']}" for l in logs[-10:]])
        await event.reply(f"Last {len(logs[-10:])} logs:\n\n{text}")

@client.on(events.NewMessage())
async def auto_reply(event):
    data = load_data()
    if not data["offline"]:
        return

    # Avoid replying to yourself
    if event.sender_id == BOT_OWNER_ID:
        return

    should_reply = False

    # Check for DM
    if event.is_private:
        should_reply = True

    # Check for reply to you
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg.sender_id == BOT_OWNER_ID:
            should_reply = True

    # Check for mention
    if event.message.mentioned:
        should_reply = True

    if should_reply:
        await event.reply(data["message"])
        # Log it
        data["logs"].append({
            "from": str(event.sender_id),
            "text": event.raw_text
        })
        save_data(data)

# ====== Run Loop ======
def start_bot():
    loop = asyncio.get_event_loop()
    loop.create_task(client.start())
    loop.run_until_complete(client.connect())

    if not client.is_connected():
        loop.run_until_complete(client.connect())

    loop.create_task(client.run_until_disconnected())

start_bot()
