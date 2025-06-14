import os
import asyncio
import json
import time # For uptime calculation
from datetime import datetime, timedelta # For temporary offline mode
import re # For better keyword matching and regex escaping

from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat, MessageMediaPhoto, MessageMediaDocument, InputPhoto, InputDocument
from telethon.errors import ChatIdInvalidError, PeerIdInvalidError, RPCError, PhotoInvalidError, DocumentInvalidError

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
custom_commands = {} # {"trigger": {"type": "text", "content": "reply_message"}} or {"type": "media", "content": {"id": ..., "access_hash": ..., "file_reference": ...}, "caption": "optional_caption", "is_photo": True/False}
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
                
                # Custom loading for custom_commands to reconstruct InputPhoto/InputDocument
                loaded_commands = state.get("custom_commands", {})
                reconstructed_commands = {}
                for trigger, details in loaded_commands.items():
                    if details.get("type") == "media" and "content" in details:
                        media_data = details["content"]
                        if details.get("is_photo"):
                            if all(k in media_data for k in ["id", "access_hash", "file_reference"]):
                                try:
                                    # Decode file_reference from base64 if it was encoded for JSON
                                    file_reference_bytes = bytes.fromhex(media_data["file_reference"])
                                    reconstructed_commands[trigger] = {
                                        "type": "media",
                                        "content": InputPhoto(
                                            id=media_data["id"],
                                            access_hash=media_data["access_hash"],
                                            file_reference=file_reference_bytes # Ensure bytes
                                        ),
                                        "caption": details.get("caption", ""),
                                        "is_photo": True
                                    }
                                except (TypeError, ValueError) as e:
                                    print(f"Warning: Could not reconstruct InputPhoto for '{trigger}' due to bad file_reference: {e}")
                                    # Fallback or skip if reconstruction fails
                                    reconstructed_commands[trigger] = {"type": "text", "content": "Error: Media asset unavailable."}
                            else:
                                print(f"Warning: Missing photo components for '{trigger}'.")
                                reconstructed_commands[trigger] = {"type": "text", "content": "Error: Media asset unavailable."}
                        else: # Assume Document
                            if all(k in media_data for k in ["id", "access_hash", "file_reference"]):
                                try:
                                    file_reference_bytes = bytes.fromhex(media_data["file_reference"])
                                    reconstructed_commands[trigger] = {
                                        "type": "media",
                                        "content": InputDocument(
                                            id=media_data["id"],
                                            access_hash=media_data["access_hash"],
                                            file_reference=file_reference_bytes # Ensure bytes
                                        ),
                                        "caption": details.get("caption", ""),
                                        "is_photo": False
                                    }
                                except (TypeError, ValueError) as e:
                                    print(f"Warning: Could not reconstruct InputDocument for '{trigger}' due to bad file_reference: {e}")
                                    reconstructed_commands[trigger] = {"type": "text", "content": "Error: Media asset unavailable."}
                            else:
                                print(f"Warning: Missing document components for '{trigger}'.")
                                reconstructed_commands[trigger] = {"type": "text", "content": "Error: Media asset unavailable."}
                    else:
                        reconstructed_commands[trigger] = details # Keep text commands as is
                custom_commands = reconstructed_commands

                is_case_sensitive_commands = state.get("is_case_sensitive_commands", False)
            print(f"Bot state loaded from {STORAGE_FILE}")
        else:
            print(f"No state file found at {STORAGE_FILE}. Starting fresh.")
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error loading state: {e}. Starting fresh.")
    if not isinstance(dnd_chats, set): dnd_chats = set()
    if not isinstance(specific_autoreplies, dict): specific_autoreplies = {}
    if not isinstance(custom_commands, dict): custom_commands = {}
    if not isinstance(is_case_sensitive_commands, bool): is_case_sensitive_commands = False


def save_state():
    state = {
        "dnd_chats": list(dnd_chats),
        "specific_autoreplies": specific_autoreplies,
        "is_case_sensitive_commands": is_case_sensitive_commands,
    }

    # Custom saving for custom_commands to handle InputPhoto/InputDocument
    serializable_commands = {}
    for trigger, details in custom_commands.items():
        if details.get("type") == "media" and isinstance(details.get("content"), (InputPhoto, InputDocument)):
            media_obj = details["content"]
            # Convert bytes (file_reference) to hex string for JSON serialization
            serializable_commands[trigger] = {
                "type": "media",
                "content": {
                    "id": media_obj.id,
                    "access_hash": media_obj.access_hash,
                    "file_reference": media_obj.file_reference.hex() # Convert bytes to hex string
                },
                "caption": details.get("caption", ""),
                "is_photo": isinstance(media_obj, InputPhoto)
            }
        else:
            serializable_commands[trigger] = details
    state["custom_commands"] = serializable_commands

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
            resolved_id = entity.id
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

    @client.on(events.NewMessage)
    async def handle_message(event):
        global is_offline, offline_message, offline_until_timestamp, \
               dnd_chats, specific_autoreplies, custom_commands, is_case_sensitive_commands

        sender = await event.get_sender()
        sender_id = event.sender_id
        is_owner = sender_id == OWNER_ID
        is_bot = isinstance(sender, User) and sender.bot
        chat_id = event.chat_id

        message_text_for_commands = event.raw_text
        if not is_case_sensitive_commands:
            message_text_for_commands = message_text_for_commands.lower()

        if chat_id in dnd_chats:
            return

        # --- Owner Commands ---
        if is_owner:
            cmd_text_raw = event.raw_text # Use raw text for command parsing
            cmd_text_lower = cmd_text_raw.lower()

            # --- Offline/Online commands ---
            # IMPORTANT: Check for /offline_for first (more specific)
            if cmd_text_lower.startswith("/offline_for "):
                clean_cmd_text = cmd_text_raw.strip()

                first_space_idx = clean_cmd_text.find(" ")
                second_space_idx = -1
                if first_space_idx != -1:
                    second_space_idx = clean_cmd_text.find(" ", first_space_idx + 1)
                
                third_space_idx = -1
                if second_space_idx != -1:
                    third_space_idx = clean_cmd_text.find(" ", second_space_idx + 1)

                parts = []
                offline_message_content = ""

                if third_space_idx == -1: # No third space means no custom message after unit
                    parts = clean_cmd_text.split(" ", 2) # Only command, num, unit
                    if len(parts) < 3: # Not enough parts for even num and unit
                        await event.reply("Invalid usage. Usage: `/offline_for <number> <unit> [message]`")
                        return
                    offline_message_content = "I'm temporarily offline."
                else: # Third space found, meaning there's a custom message
                    parts = clean_cmd_text.split(" ", 3) # Command, num, unit, message
                    if len(parts) < 4: # Should not happen if third_space_idx is found, but for safety
                         await event.reply("Error parsing command. Usage: `/offline_for <number> <unit> [message]`")
                         return
                    offline_message_content = parts[3].strip()


                if len(parts) >= 3:
                    try:
                        duration_val = int(parts[1])
                        unit_raw = parts[2].lower()
                        
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

                        offline_until_timestamp = datetime.now() + time_delta
                        is_offline = True
                        await event.reply(f"Offline mode enabled until {offline_until_timestamp.strftime('%Y-%m-%d %H:%M:%S')}.\nMessage: {offline_message_content}")
                        save_state()
                        return
                    except ValueError:
                        await event.reply("Invalid duration format. Usage: `/offline_for <number> <unit> [message]`")
                        return
                else: # Fallback if initial parsing was really off, or if only "/offline_for" was sent
                    await event.reply("Invalid usage. Usage: `/offline_for <number> <unit> [message]`")
                return

            # THEN check for /offline (less specific)
            elif cmd_text_lower.startswith("/offline"):
                # Ensure it's exactly "/offline" or "/offline " to avoid matching "/offline_for" or similar
                if cmd_text_lower == "/offline" or cmd_text_lower.startswith("/offline "):
                    parts = cmd_text_raw.split(" ", 1) # Splits into ["/offline", "message"]
                    offline_message = parts[1] if len(parts) > 1 else "I'm currently offline."
                    is_offline = True
                    offline_until_timestamp = None
                    await event.reply(f"Offline mode enabled.\nMessage: {offline_message}")
                    save_state()
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
                        specific_autoreplies[str(resolved_id)] = message
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
                            entity = await client.get_entity(int(chat_id_str))
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
                if event.is_reply:
                    replied_msg = await event.get_reply_message()
                    if replied_msg and replied_msg.media:
                        media_object = None
                        is_photo_media = False
                        
                        if isinstance(replied_msg.media, MessageMediaPhoto) and replied_msg.media.photo:
                            media_object = replied_msg.media.photo
                            is_photo_media = True
                        elif isinstance(replied_msg.media, MessageMediaDocument) and replied_msg.media.document:
                            media_object = replied_msg.media.document
                            is_photo_media = False
                        
                        if media_object:
                            parts = cmd_text_raw[len("/set_command_media "):].split("|", 1)
                            if len(parts) >= 1:
                                trigger = parts[0].strip()
                                caption = parts[1].strip() if len(parts) == 2 else ""

                                if trigger:
                                    key_trigger = trigger if is_case_sensitive_commands else trigger.lower()
                                    
                                    custom_commands[key_trigger] = {
                                        "type": "media",
                                        "content": {
                                            "id": media_object.id,
                                            "access_hash": media_object.access_hash,
                                            "file_reference": media_object.file_reference.hex()
                                        },
                                        "caption": caption,
                                        "is_photo": is_photo_media
                                    }
                                    await event.reply(f"Custom media command set!\nTrigger: `{trigger}`\nMedia Type: {'Photo' if is_photo_media else 'Document'}\nCaption: `{caption}`")
                                    save_state()
                                else:
                                    await event.reply("Invalid format. Trigger cannot be empty. Usage: `/set_command_media trigger | [caption]` (reply to media)")
                            else:
                                await event.reply("Invalid format. Usage: `/set_command_media trigger | [caption]` (reply to media)")
                        else:
                            await event.reply("The replied message does not contain a usable photo or document media.")
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
                            media_content_repr = "Media Object (reconstructable)" if isinstance(details.get("content"), (InputPhoto, InputDocument)) else "Media (broken/N/A)"
                            caption = details.get("caption", "")
                            response += f"`{trigger}` -> {media_content_repr} (Caption: `{caption}`) (Media)\n"
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
                return

        # --- END Owner Commands ---

        # --- Check Temporary Offline Mode expiration ---
        if is_offline and offline_until_timestamp and datetime.now() >= offline_until_timestamp:
            print("Timed offline mode expired. Switching to online.")
            is_offline = False
            offline_until_timestamp = None
            try:
                await client.send_message(OWNER_ID, "Timed offline mode has expired. I am now online.")
            except Exception as e:
                print(f"Could not send online notification to owner: {e}")
            save_state()

        # --- Auto-reply logic (for non-owner, and when online/offline conditions met) ---
        is_private_or_mentioned = event.is_private or event.mentioned
        
        current_auto_reply_message = None
        if str(chat_id) in specific_autoreplies:
            current_auto_reply_message = specific_autoreplies[str(chat_id)]
        elif is_offline:
            current_auto_reply_message = offline_message

        if is_offline and is_private_or_mentioned and not is_owner and not is_bot:
            if current_auto_reply_message:
                print(f"DEBUG: Replying with offline message to {sender.first_name} (ID: {sender_id}) in chat {chat_id}")
                await event.reply(current_auto_reply_message)
                
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
                return

        # --- Custom Command execution ---
        if not is_offline and not is_owner and not is_bot and is_private_or_mentioned:
            sorted_triggers = sorted(custom_commands.keys(), key=len, reverse=True)
            
            for trigger_key in sorted_triggers:
                if re.search(r'\b' + re.escape(trigger_key) + r'\b', message_text_for_commands):
                    command_details = custom_commands[trigger_key]
                    cmd_type = command_details.get("type", "text")
                    content_to_send = command_details.get("content")

                    if cmd_type == "text":
                        print(f"DEBUG: Found custom text command trigger '{trigger_key}'. Replying to {sender.first_name}.")
                        await event.reply(content_to_send)
                        return
                    elif cmd_type == "media" and content_to_send:
                        caption = command_details.get("caption", "")
                        print(f"DEBUG: Found custom media command trigger '{trigger_key}'. Replying with media to {sender.first_name}.")
                        try:
                            await client.send_file(event.chat_id, content_to_send, caption=caption, reply_to=event.id)
                            return
                        except (PhotoInvalidError, DocumentInvalidError, RPCError) as e:
                            print(f"Error sending media for command '{trigger_key}' (ID/Hash/Ref invalid?): {e}")
                            await event.reply(f"Sorry, I had an issue sending the media for that command ({e}). The media might be expired or invalid. Please notify my owner.")
                            return
                        except Exception as e:
                            print(f"General error sending media for command '{trigger_key}': {e}")
                            await event.reply(f"Sorry, a general error occurred while sending the media for that command ({e}). Please notify my owner.")
                            return

        # --- General Help Command (for non-owners) ---
        if not is_owner and event.raw_text.lower() == "/help":
            help_message = """
Hi! I'm an auto-reply bot.

If I'm online, I can respond to specific keywords:
- Type `/list_commands` to see currently available custom commands.

If I'm offline, I'll send an automatic reply.
"""
            await event.reply(help_message)
            return

    asyncio.create_task(client.run_until_disconnected())

# FastAPI endpoints (for Render) - remain mostly unchanged
@app.get("/")
async def root():
    status_msg = "Online" if not is_offline else "Offline"
    if is_offline and offline_until_timestamp:
        status_msg += f" (until {offline_until_timestamp.strftime('%Y-%m-%d %H:%M:%S')})"
    return {"status": status_msg, "offline_mode": is_offline}

@app.head("/")
async def head_root():
    return {"status": "Online"}

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
