import os
import asyncio
import json # For potential future persistence, good to import
from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat

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

# --- NEW: Dictionary to store custom commands ---
# Format: {"trigger_phrase": "reply_message"}
custom_commands = {}
# For persistence, you might load this from a file at startup:
# try:
#     with open("custom_commands.json", "r") as f:
#         custom_commands = json.load(f)
# except (FileNotFoundError, json.JSONDecodeError):
#     custom_commands = {}
# -----------------------------------------------

@app.on_event("startup")
async def startup():
    print("Starting Telegram client...")
    await client.start()
    print("Telegram client started.")

    @client.on(events.NewMessage)
    async def handle_message(event):
        global is_offline, offline_message, custom_commands # Add custom_commands to global

        # Get sender information for common use
        sender = await event.get_sender()
        is_owner = event.sender_id == OWNER_ID
        is_bot = isinstance(sender, User) and sender.bot

        # --- Command Handling for Owner ---
        if is_owner:
            cmd_text = event.raw_text.lower()

            # Offline/Online commands
            if cmd_text.startswith("/offline"):
                parts = event.raw_text.split(" ", 1)
                offline_message = parts[1] if len(parts) > 1 else "I'm currently offline."
                is_offline = True
                await event.reply(f"Offline mode enabled.\nMessage: {offline_message}")
                return # Don't process further commands
            elif cmd_text.startswith("/online"):
                is_offline = False
                await event.reply("Online mode enabled. You're now online.")
                return # Don't process further commands

            # --- NEW: /set_command ---
            if cmd_text.startswith("/set_command "):
                # Expecting format: /set_command trigger | reply_message
                parts = event.raw_text[len("/set_command "):].split("|", 1)
                if len(parts) == 2:
                    trigger = parts[0].strip()
                    reply = parts[1].strip()
                    if trigger: # Ensure trigger is not empty
                        custom_commands[trigger.lower()] = reply # Store in lowercase for case-insensitivity
                        await event.reply(f"Custom command set!\nTrigger: `{trigger}`\nReply: `{reply}`")
                        # For persistence, save to file here:
                        # with open("custom_commands.json", "w") as f:
                        #     json.dump(custom_commands, f)
                    else:
                        await event.reply("Invalid format. Usage: `/set_command trigger | reply` (trigger cannot be empty)")
                else:
                    await event.reply("Invalid format. Usage: `/set_command trigger | reply`")
                return # Don't process further commands

            # --- NEW: /del_command ---
            if cmd_text.startswith("/del_command "):
                trigger_to_delete = event.raw_text[len("/del_command "):].strip().lower()
                if trigger_to_delete in custom_commands:
                    del custom_commands[trigger_to_delete]
                    await event.reply(f"Custom command `{trigger_to_delete}` deleted.")
                    # For persistence, save to file here:
                    # with open("custom_commands.json", "w") as f:
                    #     json.dump(custom_commands, f)
                else:
                    await event.reply(f"Custom command `{trigger_to_delete}` not found.")
                return # Don't process further commands

            # --- NEW: /list_commands ---
            if cmd_text == "/list_commands":
                if custom_commands:
                    response = "Current Custom Commands:\n"
                    for trigger, reply in custom_commands.items():
                        response += f"`{trigger}` -> `{reply}`\n"
                else:
                    response = "No custom commands set yet."
                await event.reply(response)
                return # Don't process further commands

        # --- Auto-reply logic (for non-owner, and when online) ---
        # Prioritize offline auto-reply if in offline mode
        if is_offline and (event.is_private or event.mentioned) and not is_owner and not is_bot:
            print(f"DEBUG: Replying with offline message to {sender.first_name} (ID: {sender.id})")
            await event.reply(offline_message)
            # Log to Saved Messages
            await event.forward_to("me")
            sender_name = sender.first_name or "Unknown"
            username = f"@{sender.username}" if sender.username else "No username"
            await client.send_message(
                "me",
                f"↖️ Message above was from {sender_name} ({username}) while you were offline."
            )
            return # Crucially, return after handling offline message

        # --- Handle custom commands when ONLINE and conditions met ---
        # Only process custom commands if bot is ONLINE, sender is not owner, and not a bot
        if not is_offline and not is_owner and not is_bot:
            message_text = event.raw_text.lower()
            
            # Check for direct message or mention in group
            is_relevant_chat = event.is_private or event.mentioned

            if is_relevant_chat:
                for trigger, reply in custom_commands.items():
                    # Simple check: if trigger is in message text
                    if trigger in message_text:
                        print(f"DEBUG: Found custom command trigger '{trigger}'. Replying to {sender.first_name}.")
                        await event.reply(reply)
                        return # Reply once and stop
        # -----------------------------------------------------------

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

