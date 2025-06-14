import os
import asyncio
import json
import time # For uptime calculation
from datetime import datetime, timedelta # For temporary offline mode
import re # For better keyword matching and regex escaping

from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat, MessageMediaPhoto, MessageMediaDocument
from telethon.errors import ChatIdInvalidError, PeerIdInvalidError, RPCError

# --- Environment Variables ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION")
OWNER_ID = int(os.getenv("OWNER_ID"))
TARGET_CHAT_ID_ENV = os.getenv("TARGET_CHAT_ID") # Keep as string initially

# Define storage file
STORAGE_FILE = os.getenv("STORAGE_FILE", "bot_state.json")

# --- Global State ---
app = FastAPI()
client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

is_offline = False
offline_message = "I'm currently offline. Will reply soon!"
offline_until_timestamp = None # Stores datetime object for when offline mode ends

# Persistence data structures
# Use sets for dnd_chats for efficient lookups and to avoid duplicates
dnd_chats = set()
specific_autoreplies = {} # {chat_id: "message"}
custom_commands = {} # {"trigger": {"type": "text", "content": "reply_message"}} or {"type": "media", "content": "media_id", "caption": "optional_caption"}
is_case_sensitive_commands = False # Default to case-insensitive

bot_start_time = datetime.now() # To track bot uptime

# --- Target Chat ID for Offline Notifications ---
TARGET_CHAT_ID = "me" # Default to Saved Messages
if TARGET_CHAT_ID_ENV:
    try:
        TARGET_CHAT_ID = int(TARGET_CHAT_ID_ENV)
    except ValueError:
        print(f"Warning: TARGET_CHAT_ID environment variable '{TARGET_CHAT_ID_ENV}' is not a valid integer. Defaulting to 'me'.")
else:
    print("Info: TARGET_CHAT_ID environment variable not set. Offline messages will be forwarded to 'me' (Saved Messages).")

# --- Persistence Functions ---
def load_state():
    global dnd_chats, specific_autoreplies, custom_commands, is_case_sensitive_commands
    try:
        if os.path.exists(STORAGE_FILE):
            with open(STORAGE_FILE, "r") as f:
                state = json.load(f)
                dnd_chats = set(state.get("dnd_chats", []))
                specific_autoreplies = state.get("specific_autoreplies", {})
                custom_commands = state.get("custom_commands", {})
                is_case_sensitive_commands = state.get("is_case_sensitive_commands", False)
            print(f"Bot state loaded from {STORAGE_FILE}")
        else:
            print(f"No state file found at {STORAGE_FILE}. Starting fresh.")
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error loading state: {e}. Starting fresh.")
    # Ensure all required keys exist even if file was empty or corrupted
    if not isinstance(dnd_chats, set): dnd_chats = set()
    if not isinstance(specific_autoreplies, dict): specific_autoreplies = {}
    if not isinstance(custom_commands, dict): custom_commands = {}
    if not isinstance(is_case_sensitive_commands, bool): is_case_sensitive_commands = False

def save_state():
    state = {
        "dnd_chats": list(dnd_chats), # Convert set to list for JSON serialization
        "specific_autoreplies": specific_autoreplies,
        "custom_commands": custom_commands,
        "is_case_sensitive_commands": is_case_sensitive_commands,
    }
    try:
        with open(STORAGE_FILE, "w") as f:
            json.dump(state, f, indent=4)
        print(f"Bot state saved to {STORAGE_FILE}")
    except IOError as e:
        print(f"Error saving state: {e}")

# --- Helper Function to Get Chat Entity ---
async def get_chat_entity_from_arg(arg, event):
    """
    Tries to resolve a chat_id or username string to a chat entity.
    Returns (entity, chat_id, error_message)
    """
    if not arg:
        return None, None, "No chat ID or username provided."

    try:
        # Try to interpret as an integer ID first
        chat_id = int(arg)
        entity = await client.get_entity(chat_id)
        return entity, chat_id, None
    except ValueError:
        # Not an integer, try as a username
        try:
            entity = await client.get_entity(arg)
            # Use the resolved entity's ID for consistency
            resolved_id = entity.id
            if isinstance(entity, (Channel, Chat)) and entity.megagroup:
                # For supergroups and channels, the ID might be negative in some contexts.
                # Use event.chat_id for reliable target when sending replies/forwards within the handler context.
                # However, for storage, entity.id is generally what you want.
                pass
            return entity, resolved_id, None
        except (ValueError, ChatIdInvalidError, PeerIdInvalidError) as e:
            return None, None, f"Could not find chat/user for '{arg}': {e}"
    except (ChatIdInvalidError, PeerIdInvalidError, RPCError) as e:
        return None, None, f"Telegram API error for '{arg}': {e}"

# --- Bot Startup and Message Handler ---
@app.on_event("startup")
async def startup():
    print("Loading bot state...")
    load_state()
    print("Starting Telegram client...")
    await client.start()
    print("Telegram client started.")

    # Main message handler
    @client.on(events.NewMessage)
    async def handle_message(event):
        global is_offline, offline_message, offline_until_timestamp, \
               dnd_chats, specific_autoreplies, custom_commands, is_case_sensitive_commands

        sender = await event.get_sender()
        sender_id = event.sender_id
        is_owner = sender_id == OWNER_ID
        is_bot = isinstance(sender, User) and sender.bot
        chat_id = event.chat_id # The ID of the chat where the message originated

        # Convert message text based on case sensitivity setting
        message_text_for_commands = event.raw_text
        if not is_case_sensitive_commands:
            message_text_for_commands = message_text_for_commands.lower()

        # --- DND Check (applies to all incoming messages for auto-reply/forward) ---
        if chat_id in dnd_chats:
            # print(f"DEBUG: Message from DND chat {chat_id}. Ignoring auto-reply/forward.")
            return # Do not process messages from DND chats for auto-reply/forward

        # --- Owner Commands ---
        if is_owner:
            cmd_text_raw = event.raw_text # Use raw text for command parsing
            cmd_text_lower = cmd_text_raw.lower()

            # --- Offline/Online commands ---
            if cmd_text_lower.startswith("/offline"):
                parts = cmd_text_raw.split(" ", 1)
                offline_message = parts[1] if len(parts) > 1 else "I'm currently offline."
                is_offline = True
                offline_until_timestamp = None # Clear any timed offline
                await event.reply(f"Offline mode enabled.\nMessage: {offline_message}")
                save_state()
                return
            elif cmd_text_lower.startswith("/offline_for "):
                parts = cmd_text_raw.split(" ", 3) # Split into at most 4 parts: /offline_for, num, unit, message
                if len(parts) >= 3: # Need at least "/offline_for", "num", "unit"
                    try:
                        duration_val = int(parts[1])
                        unit_raw = parts[2].lower() # e.g., "h", "m", "d"
                        
                        time_delta = timedelta()
                        if unit_raw in ["minutes", "minute", "m"]:
                            time_delta = timedelta(minutes=duration_val)
                        elif unit_raw in ["hours", "hour", "h"]:
                            time_delta = timedelta(hours=duration_val)
                        elif unit_raw in ["days", "day", "d"]:
                            time_delta = timedelta(days=duration_val)
                        else:
                            await event.reply("Invalid time unit. Use: `m` (minutes), `h` (hours), `d` (days).")
                            return

                        # Extract the message: it's the 4th part if it exists
                        if len(parts) == 4:
                            offline_message = parts[3].strip()
                        else:
                            offline_message = "I'm temporarily offline." # Default if no message provided

                        offline_until_timestamp = datetime.now() + time_delta
                        is_offline = True
                        await event.reply(f"Offline mode enabled until {offline_until_timestamp.strftime('%Y-%m-%d %H:%M:%S')}.\nMessage: {offline_message}")
                        save_state()
                        return
                    except ValueError:
                        await event.reply("Invalid duration format. Usage: `/offline_for <number> <unit> [message]`")
                        return
                else:
                    await event.reply("Invalid usage. Usage: `/offline_for <number> <unit> [message]`")
                return

            elif cmd_text_lower.startswith("/online"):
                is_offline = False
                offline_until_timestamp = None
                await event.reply("Online mode enabled. You're now online.")
                save_state()
                return

            # --- DND Commands ---
            elif cmd_text_lower.startswith("/dnd "):
                arg = cmd_text_raw[len("/dnd "):].strip()
                entity, resolved_id, error = await get_chat_entity_from_arg(arg, event)
                if entity:
                    dnd_chats.add(resolved_id)
                    await event.reply(f"Added {entity.title if hasattr(entity, 'title') else entity.first_name} (ID: `{resolved_id}`) to DND list.")
                    save_state()
                else:
                    await event.reply(f"Error adding to DND: {error}")
                return
            elif cmd_text_lower.startswith("/undnd "):
                arg = cmd_text_raw[len("/undnd "):].strip()
                entity, resolved_id, error = await get_chat_entity_from_arg(arg, event)
                if entity:
                    if resolved_id in dnd_chats:
                        dnd_chats.remove(resolved_id)
                        await event.reply(f"Removed {entity.title if hasattr(entity, 'title') else entity.first_name} (ID: `{resolved_id}`) from DND list.")
                        save_state()
                    else:
                        await event.reply(f"Chat {entity.title if hasattr(entity, 'title') else entity.first_name} (ID: `{resolved_id}`) was not in DND list.")
                else:
                    await event.reply(f"Error removing from DND: {error}")
                return
            elif cmd_text_lower == "/list_dnd":
                if dnd_chats:
                    response = "DND Chats:\n"
                    for chat_id_dnd in dnd_chats:
                        try:
                            entity = await client.get_entity(chat_id_dnd)
                            name = entity.title if hasattr(entity, 'title') else entity.first_name
                            response += f"- `{name}` (ID: `{chat_id_dnd}`)\n"
                        except (ChatIdInvalidError, PeerIdInvalidError, RPCError):
                            response += f"- Unknown Chat (ID: `{chat_id_dnd}` - possibly left/deleted)\n"
                    await event.reply(response)
                else:
                    await event.reply("No chats currently in DND mode.")
                return

            # --- Specific Auto-reply Commands ---
            elif cmd_text_lower.startswith("/set_autoreply "):
                parts = cmd_text_raw[len("/set_autoreply "):].split("|", 1)
                if len(parts) == 2:
                    arg = parts[0].strip()
                    message = parts[1].strip()
                    entity, resolved_id, error = await get_chat_entity_from_arg(arg, event)
                    if entity and message:
                        specific_autoreplies[str(resolved_id)] = message # Store ID as string for JSON key safety
                        await event.reply(f"Specific auto-reply set for {entity.title if hasattr(entity, 'title') else entity.first_name} (ID: `{resolved_id}`):\n`{message}`")
                        save_state()
                    else:
                        await event.reply(f"Error setting auto-reply: {error or 'Message cannot be empty.'}")
                else:
                    await event.reply("Invalid format. Usage: `/set_autoreply <chat_id/username> | <message>`")
                return
            elif cmd_text_lower.startswith("/del_autoreply "):
                arg = cmd_text_raw[len("/del_autoreply "):].strip()
                entity, resolved_id, error = await get_chat_entity_from_arg(arg, event)
                if entity:
                    if str(resolved_id) in specific_autoreplies:
                        del specific_autoreplies[str(resolved_id)]
                        await event.reply(f"Specific auto-reply for {entity.title if hasattr(entity, 'title') else entity.first_name} (ID: `{resolved_id}`) deleted.")
                        save_state()
                    else:
                        await event.reply(f"No specific auto-reply found for {entity.title if hasattr(entity, 'title') else entity.first_name} (ID: `{resolved_id}`).")
                else:
                    await event.reply(f"Error deleting auto-reply: {error}")
                return
            elif cmd_text_lower == "/list_autoreplies":
                if specific_autoreplies:
                    response = "Specific Auto-replies:\n"
                    for chat_id_str, msg in specific_autoreplies.items():
                        try:
                            entity = await client.get_entity(int(chat_id_str)) # Convert back to int for get_entity
                            name = entity.title if hasattr(entity, 'title') else entity.first_name
                            response += f"- `{name}` (ID: `{chat_id_str}`): `{msg}`\n"
                        except (ChatIdInvalidError, PeerIdInvalidError, RPCError):
                            response += f"- Unknown Chat (ID: `{chat_id_str}` - possibly left/deleted): `{msg}`\n"
                    await event.reply(response)
                else:
                    await event.reply("No specific auto-replies set.")
                return

            # --- Custom Command Management ---
            elif cmd_text_lower.startswith("/set_command "):
                # Expecting format: /set_command trigger | reply_message
                parts = cmd_text_raw[len("/set_command "):].split("|", 1)
                if len(parts) == 2:
                    trigger = parts[0].strip()
                    reply = parts[1].strip()
                    if trigger and reply:
                        key_trigger = trigger if is_case_sensitive_commands else trigger.lower()
                        custom_commands[key_trigger] = {"type": "text", "content": reply}
                        await event.reply(f"Custom text command set!\nTrigger: `{trigger}`\nReply: `{reply}`")
                        save_state()
                    else:
                        await event.reply("Invalid format. Trigger and reply cannot be empty. Usage: `/set_command trigger | reply`")
                else:
                    await event.reply("Invalid format. Usage: `/set_command trigger | reply`")
                return
            elif cmd_text_lower.startswith("/set_command_media "):
                # Command must be a reply to the media message!
                if event.is_reply:
                    replied_msg = await event.get_reply_message()
                    if replied_msg and replied_msg.media: # Check if media exists
                        media_file_id = None
                        if replied_msg.photo:
                            media_file_id = replied_msg.photo.file_id
                        elif replied_msg.document: # Covers videos, files, stickers, GIFs
                            media_file_id = replied_msg.document.file_id
                        
                        if media_file_id is None:
                            await event.reply("The replied message does not contain a usable photo or document media.")
                            return

                        parts = cmd_text_raw[len("/set_command_media "):].split("|", 1)
                        if len(parts) >= 1: # Caption is optional
                            trigger = parts[0].strip()
                            caption = parts[1].strip() if len(parts) == 2 else ""

                            if trigger:
                                key_trigger = trigger if is_case_sensitive_commands else trigger.lower()
                                custom_commands[key_trigger] = {
                                    "type": "media",
                                    "content": media_file_id, # Store file_id
                                    "caption": caption
                                }
                                await event.reply(f"Custom media command set!\nTrigger: `{trigger}`\nMedia File ID: `{media_file_id}`\nCaption: `{caption}`")
                                save_state()
                            else:
                                await event.reply("Invalid format. Trigger cannot be empty. Usage: `/set_command_media trigger | [caption]` (reply to media)")
                        else:
                            await event.reply("Invalid format. Usage: `/set_command_media trigger | [caption]` (reply to media)")
                    else:
                        await event.reply("You must reply to a photo or document message to use this command.")
                else:
                    await event.reply("This command must be a reply to the media message you want to set as a response.")
                return

            elif cmd_text_lower.startswith("/del_command "):
                trigger_to_delete = cmd_text_raw[len("/del_command "):].strip()
                key_trigger = trigger_to_delete if is_case_sensitive_commands else trigger_to_delete.lower()
                
                if key_trigger in custom_commands:
                    del custom_commands[key_trigger]
                    await event.reply(f"Custom command `{trigger_to_delete}` deleted.")
                    save_state()
                else:
                    await event.reply(f"Custom command `{trigger_to_delete}` not found.")
                return

            elif cmd_text_lower == "/list_commands":
                if custom_commands:
                    response = "Current Custom Commands:\n"
                    for trigger, details in custom_commands.items():
                        cmd_type = details.get("type", "text")
                        if cmd_type == "text":
                            content = details.get("content", "N/A")
                            response += f"`{trigger}` -> `{content}` (Text)\n"
                        elif cmd_type == "media":
                            media_id = details.get("content", "N/A")
                            caption = details.get("caption", "")
                            response += f"`{trigger}` -> Media ID: `{media_id}` (Caption: `{caption}`) (Media)\n"
                    await event.reply(response)
                else:
                    await event.reply("No custom commands set yet.")
                return

            elif cmd_text_lower.startswith("/set_case_sensitive "):
                arg = cmd_text_lower[len("/set_case_sensitive "):].strip()
                if arg == "on":
                    is_case_sensitive_commands = True
                    await event.reply("Custom commands are now case-sensitive.")
                elif arg == "off":
                    is_case_sensitive_commands = False
                    await event.reply("Custom commands are now case-insensitive.")
                    # Re-normalize existing command keys if switching to insensitive
                    new_commands = {}
                    for trigger, details in custom_commands.items():
                        new_commands[trigger.lower()] = details
                    custom_commands.clear()
                    custom_commands.update(new_commands)
                else:
                    await event.reply("Invalid argument. Use `/set_case_sensitive on` or `/set_case_sensitive off`.")
                save_state()
                return

            # --- General Utility Commands for Owner ---
            elif cmd_text_lower == "/status":
                uptime_seconds = (datetime.now() - bot_start_time).total_seconds()
                days, remainder = divmod(uptime_seconds, 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, seconds = divmod(remainder, 60)
                
                status_msg = f"Bot Status: {'Offline' if is_offline else 'Online'}\n"
                if is_offline and offline_until_timestamp:
                    status_msg += f"Offline until: {offline_until_timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                status_msg += f"Uptime: {int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s\n"
                status_msg += f"DND Chats: {len(dnd_chats)}\n"
                status_msg += f"Specific Auto-replies: {len(specific_autoreplies)}\n"
                status_msg += f"Custom Commands: {len(custom_commands)} (Case-sensitive: {is_case_sensitive_commands})\n"
                await event.reply(status_msg)
                return

            elif cmd_text_lower == "/help_owner":
                help_message = """
Owner Commands:
**Offline/Online Mode:**
- `/offline [message]`: Go offline with an optional message.
- `/offline_for <number> <unit> [message]`: Go offline for a duration (e.g., `2h`, `30m`).
- `/online`: Go online.

**Do Not Disturb (DND):**
- `/dnd <chat_id/username>`: Add chat to DND list (no auto-replies).
- `/undnd <chat_id/username>`: Remove chat from DND list.
- `/list_dnd`: List all DND chats.

**Specific Auto-Replies:**
- `/set_autoreply <chat_id/username> | <message>`: Set a custom auto-reply for a chat.
- `/del_autoreply <chat_id/username>`: Delete a custom auto-reply.
- `/list_autoreplies`: List all specific auto-replies.

**Custom Commands:**
- `/set_command <trigger> | <reply>`: Set a text-based custom command.
- `/set_command_media <trigger> | [caption]`: **Reply to a photo/document** to set it as a media command.
- `/del_command <trigger>`: Delete a custom command.
- `/list_commands`: List all custom commands.
- `/set_case_sensitive <on/off>`: Toggle case sensitivity for custom commands.

**Utilities:**
- `/status`: Show bot uptime and current state.
- `/help_owner`: Show this help message.
"""
                await event.reply(help_message)
                return # Crucial: Return after owner command is handled

        # --- END Owner Commands ---

        # --- Check Temporary Offline Mode expiration ---
        if is_offline and offline_until_timestamp and datetime.now() >= offline_until_timestamp:
            print("Timed offline mode expired. Switching to online.")
            is_offline = False
            offline_until_timestamp = None
            # Optionally, notify owner
            try:
                await client.send_message(OWNER_ID, "Timed offline mode has expired. I am now online.")
            except Exception as e:
                print(f"Could not send online notification to owner: {e}")
            save_state() # Save state after going online

        # --- Auto-reply logic (for non-owner, and when online/offline conditions met) ---
        is_private_or_mentioned = event.is_private or event.mentioned
        
        # Determine the auto-reply message based on priority: Specific > Global
        current_auto_reply_message = None
        if str(chat_id) in specific_autoreplies:
            current_auto_reply_message = specific_autoreplies[str(chat_id)]
        elif is_offline: # Only use global offline_message if not using specific AND is in offline mode
            current_auto_reply_message = offline_message

        # Prioritize offline auto-reply if in offline mode and relevant chat
        if is_offline and is_private_or_mentioned and not is_owner and not is_bot:
            if current_auto_reply_message: # Ensure there's a message to send
                print(f"DEBUG: Replying with offline message to {sender.first_name} (ID: {sender_id}) in chat {chat_id}")
                await event.reply(current_auto_reply_message)
                
                # Log to TARGET_CHAT_ID (owner's saved messages or specific chat)
                try:
                    await event.forward_to(TARGET_CHAT_ID)
                    sender_name = sender.first_name or "Unknown"
                    username = f"@{sender.username}" if sender.username else "No username"
                    await client.send_message(
                        TARGET_CHAT_ID,
                        f"↖️ Message above was from {sender_name} ({username}) (ID: `{sender_id}`) while you were offline."
                    )
                except Exception as e:
                    print(f"Error forwarding message or sending notification to TARGET_CHAT_ID {TARGET_CHAT_ID}: {e}")
                return # Crucially, return after handling offline message

        # --- Handle custom commands when ONLINE and conditions met ---
        # Only process custom commands if bot is ONLINE, sender is not owner, not a bot, and relevant chat
        if not is_offline and not is_owner and not is_bot and is_private_or_mentioned:
            # Sort triggers by length (longest first) to ensure more specific phrases are matched before shorter ones
            sorted_triggers = sorted(custom_commands.keys(), key=len, reverse=True)
            
            for trigger_key in sorted_triggers:
                # Use word boundaries for more precise matching
                # re.escape is used to handle special regex characters in the trigger itself
                if re.search(r'\b' + re.escape(trigger_key) + r'\b', message_text_for_commands):
                    command_details = custom_commands[trigger_key]
                    cmd_type = command_details.get("type", "text")
                    content = command_details.get("content")
                    
                    if cmd_type == "text":
                        print(f"DEBUG: Found custom text command trigger '{trigger_key}'. Replying to {sender.first_name}.")
                        await event.reply(content)
                        return # Reply once and stop
                    elif cmd_type == "media" and content:
                        caption = command_details.get("caption", "")
                        print(f"DEBUG: Found custom media command trigger '{trigger_key}'. Replying with media to {sender.first_name}.")
                        try:
                            # Use client.send_file to send by file_id
                            await client.send_file(event.chat_id, content, caption=caption, reply_to=event.id)
                            return # Reply once and stop
                        except Exception as e:
                            print(f"Error sending media for command '{trigger_key}': {e}")
                            await event.reply(f"Sorry, I had an issue sending the media for that command ({e}). Please notify my owner.")
                            return # Return here if the media command failed

        # --- General Help Command (for non-owners) ---
        # This block should be *outside* the `if is_owner:` block
        # and should only process if the message hasn't been handled by other auto-replies/commands
        if not is_owner and event.raw_text.lower() == "/help": # Use raw_text to specifically match /help
            help_message = """
Hi! I'm an auto-reply bot.

If I'm online, I can respond to specific keywords:
- Type `/list_commands` to see currently available custom commands.

If I'm offline, I'll send an automatic reply.
"""
            await event.reply(help_message)
            return # Return after handling public /help command

    # Keep client running in background
    asyncio.create_task(client.run_until_disconnected())

# FastAPI endpoints (for Render) - remain mostly unchanged, interacting with global state
@app.get("/")
async def root():
    status_msg = "Online" if not is_offline else "Offline"
    if is_offline and offline_until_timestamp:
        status_msg += f" (until {offline_until_timestamp.strftime('%Y-%m-%d %H:%M:%S')})"
    return {"status": status_msg, "offline_mode": is_offline}

@app.head("/")
async def head_root():
    return {"status": "Online"} # Content here is ignored for HEAD

@app.post("/offline")
async def go_offline_api(data: dict):
    global is_offline, offline_message, offline_until_timestamp
    offline_message = data.get("message", "I'm currently offline.")
    is_offline = True
    offline_until_timestamp = None
    save_state()
    return {"status": "Offline", "message": offline_message}

@app.post("/online")
async def go_online_api():
    global is_offline, offline_until_timestamp
    is_offline = False
    offline_until_timestamp = None
    save_state()
    return {"status": "Online mode enabled"}
