import os
import asyncio
from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat # Import necessary types for robust checks

# Load environment variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION")
OWNER_ID = int(os.getenv("OWNER_ID"))

# FastAPI instance
app = FastAPI()

# Telethon client
client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

# Global state
is_offline = False
offline_message = "I'm currently offline. Will reply soon!"

@app.on_event("startup")
async def startup():
    print("Starting Telegram client...")
    await client.start()
    print("Telegram client started.")

    @client.on(events.NewMessage)
    async def handle_message(event):
        global is_offline, offline_message

        # Only allow owner to control commands
        if event.sender_id == OWNER_ID:
            cmd = event.raw_text.lower()

            if cmd.startswith("/offline"):
                parts = event.raw_text.split(" ", 1)
                offline_message = parts[1] if len(parts) > 1 else "I'm currently offline."
                is_offline = True
                await event.reply(f"Offline mode enabled.\nMessage: {offline_message}")
                return

            elif cmd.startswith("/online"):
                is_offline = False
                await event.reply("Online mode enabled. You're now online.")
                return

        # Handle auto-reply and message logging when offline
        should_reply = event.is_private or event.mentioned
        if is_offline and should_reply and event.sender_id != OWNER_ID:
            sender = await event.get_sender()

            # --- THE CRITICAL MODIFICATION START ---
            # Check if the sender is a User and specifically if that User is a Telegram bot
            if isinstance(sender, User) and sender.bot:
                print(f"DEBUG: Skipping reply to a Telegram bot. "
                      f"Sender Name: {sender.first_name} (ID: {sender.id}), "
                      f"Username: @{sender.username or 'N/A'}. "
                      f"IsBot: {sender.bot}")
                return # Crucially, exit the function to prevent reply

            # Optionally, you might want to skip replies to channels/groups too,
            # though `event.is_private` should largely handle this for direct replies.
            # If you want to explicitly avoid replying to channels/groups even if mentioned:
            # if isinstance(sender, (Channel, Chat)):
            #     print(f"DEBUG: Skipping reply to a channel/group: {sender.title} (ID: {sender.id})")
            #     return
            # --- THE CRITICAL MODIFICATION END ---

            # If it's not a bot (or if you removed the channel/group check), proceed to reply
            await event.reply(offline_message)

            # Get sender info for logging
            sender_name = sender.first_name or "Unknown"
            username = f"@{sender.username}" if sender.username else "No username"

            # Send to Saved Messages
            await event.forward_to("me")
            await client.send_message(
                "me",
                f"↖️ Message above was from {sender_name} ({username}) while you were offline."
            )

    asyncio.create_task(client.run_until_disconnected())

# FastAPI endpoints (for Render)
@app.get("/")
async def root():
    return {"status": "Online", "offline_mode": is_offline}

@app.post("/offline")
async def go_offline(data: dict):
    global is_offline, offline_message
    offline_message = data.get("message", "I'm currently offline.")
    is_offline = True
    return {"status": "Offline", "message": offline_message}

@app.post("/online")
async def go_online():
    global is_offline
    is_offline = False
    return {"status": "Online mode enabled"}
