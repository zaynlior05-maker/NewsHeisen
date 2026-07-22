import os
import re
import asyncio
from pyrogram import Client, filters, enums, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument

# ==========================================
# 1. CREDENTIALS & TOKENS
# ==========================================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"] 
MY_BOT_TOKEN = os.environ["MY_BOT_TOKEN"]

# ==========================================
# 2. CHAT IDs
# ==========================================
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
SOURCE_GROUP_ID = int(os.environ["SOURCE_GROUP_ID"])
DESTINATION_CHAT_ID = int(os.environ["DESTINATION_CHAT_ID"])
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"])

# ==========================================
# 3. BRANDING (Loaded from Railway)
# ==========================================
MY_CHANNEL = os.environ.get("MY_CHANNEL", "")
MY_ADMIN = os.environ.get("MY_ADMIN", "")

# ==========================================
# 4. GLOBALS & EXCLUDED PHRASES
# ==========================================
BOT_INFO = None

# Track albums so we don't process the same group multiple times
PROCESSED_SOURCE_ALBUMS = set()
BOT_INBOX_ALBUMS = {}

EXCLUDED_PHRASES = [
    "( PAID AD )",
    "The giveaway has officially ended.",
    "Giveaway Entries"
]

# ==========================================
# 5. INITIALIZE CLIENTS
# ==========================================
user_app = Client("user_account", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)
bot_app = Client("my_bot", bot_token=MY_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ==========================================
# 6. HELPER FUNCTIONS
# ==========================================
def is_excluded(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(phrase.lower() in text_lower for phrase in EXCLUDED_PHRASES)

def clean_and_brand_text(text_html: str) -> str:
    if not text_html:
        return ""
    
    cleaned = re.sub(r'<a[^>]*href="https://t\.me/[^>]*>[^<]*</a>', '', text_html)
    cleaned = re.sub(r'\B@[\w_]+', '', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned).strip()
    
    if MY_CHANNEL or MY_ADMIN:
        signature_parts = []
        if MY_CHANNEL:
            signature_parts.append(MY_CHANNEL)
        if MY_ADMIN:
            signature_parts.append(MY_ADMIN)
            
        signature = "\n\n" + " / ".join(signature_parts)
        cleaned += signature
        
    return cleaned

# ==========================================
# 7. BOT INBOX LISTENER (THE AGGRESSIVE CATCHER)
# ==========================================
async def process_bot_album(group_id):
    """Waits for all album parts to land in DMs, cleans the first caption, and groups them side-by-side."""
    await asyncio.sleep(2.5) 
    messages = BOT_INBOX_ALBUMS.pop(group_id, [])
    if not messages:
        return

    # Sort to preserve the exact original image order
    messages.sort(key=lambda x: x.id)
    
    media_list = []
    has_set_caption = False
    
    for msg in messages:
        caption = ""
        # Only attach your clean text to the VERY FIRST image in the album
        if not has_set_caption:
            raw_caption = msg.caption.html if msg.caption else ""
            caption = clean_and_brand_text(raw_caption) if raw_caption else clean_and_brand_text(" ")
            has_set_caption = True
            
        # Add the zero-upload files to the media group payload
        if msg.photo:
            media_list.append(InputMediaPhoto(media=msg.photo.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
        elif msg.video:
            media_list.append(InputMediaVideo(media=msg.video.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
        elif msg.document:
            media_list.append(InputMediaDocument(media=msg.document.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))

    if media_list:
        try:
            # Post the full grouped album to destination instantly
            await bot_app.send_media_group(DESTINATION_CHAT_ID, media_list)
            print("✅ SUCCESS: Bot perfectly grouped and relayed the ALBUM!")
        except Exception as e:
            print(f"❌ ERROR: Bot failed to post ALBUM: {e}")

    # Clean up Inbox
    for msg in messages:
        try:
            await msg.delete()
        except:
            pass

@bot_app.on_message(filters.private)
async def bot_inbox_handler(client, message):
    """Catches files dropped in the Inbox. Handles both single items and albums."""
    if not message.media:
        return

    # If it's part of an Album, route it to the Album Manager
    if message.media_group_id:
        if message.media_group_id not in BOT_INBOX_ALBUMS:
            BOT_INBOX_ALBUMS[message.media_group_id] = []
            asyncio.create_task(process_bot_album(message.media_group_id))
        BOT_INBOX_ALBUMS[message.media_group_id].append(message)
        return
        
    # If it's a single image, process immediately
    print("⚡️ BOT INBOX CATCH: Received single relayed media!")
    try:
        await message.copy(chat_id=DESTINATION_CHAT_ID)
        print("✅ SUCCESS: Bot instantly relayed the single media!")
        await message.delete()
    except Exception as e:
        print(f"❌ ERROR: Bot failed to post relayed media: {e}")

# ==========================================
# 8. CORE MESSAGE PROCESSOR (USERBOT SIDE)
# ==========================================
async def process_and_send_message(message):
    
    # --- ALBUM LOGIC ---
    if message.media_group_id:
        if message.media_group_id in PROCESSED_SOURCE_ALBUMS:
            return # We only need to trigger on the first piece
        
        PROCESSED_SOURCE_ALBUMS.add(message.media_group_id)
        
        # Wait 1 sec to ensure the full album exists in the source chat
        await asyncio.sleep(1.0)
        
        try:
            # Fetch the whole group to check for excluded phrases
            group_msgs = await user_app.get_media_group(message.chat.id, message.id)
            for g_msg in group_msgs:
                if is_excluded(g_msg.text or g_msg.caption or ""):
                    print(f"Skipped album {message.media_group_id} (matched excluded phrase)")
                    return
            
            print(f"Relaying ALBUM {message.media_group_id} to Bot Inbox...")
            # Silently copy the whole group into the bot's DMs 
            await user_app.copy_media_group(BOT_INFO.username, message.chat.id, message.id)
        except Exception as e:
            print(f"❌ Error passing ALBUM to Bot Inbox: {e}")
        return

    # --- SINGLE MEDIA/TEXT LOGIC ---
    raw_content = message.text or message.caption or ""
    if is_excluded(raw_content):
        print(f"Skipped message {message.id} (matched excluded phrase)")
        return

    if message.text:
        new_text = clean_and_brand_text(message.text.html)
        await bot_app.send_message(chat_id=DESTINATION_CHAT_ID, text=new_text, parse_mode=enums.ParseMode.HTML)
        print(f"Bot directly sent text message from {message.chat.id}")

    elif message.media:
        caption_source = message.caption.html if message.caption else ""
        new_caption = clean_and_brand_text(caption_source) if caption_source else clean_and_brand_text(" ")
        
        print(f"Relaying single media {message.id} to Bot Inbox...")
        try:
            await user_app.copy_message(
                chat_id=BOT_INFO.username,
                from_chat_id=message.chat.id,
                message_id=message.id,
                caption=new_caption,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            print(f"❌ Error passing media to Bot Inbox: {e}")

# ==========================================
# 9. LIVE MESSAGE EVENT HANDLER
# ==========================================
@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    if message.chat.id == SOURCE_GROUP_ID:
        if not message.from_user or message.from_user.id != TARGET_BOT_ID:
            return  
        
    await process_and_send_message(message)

# ==========================================
# 10. STARTUP, CACHE WARMUP & FETCH LAST MESSAGE
# ==========================================
async def main():
    global BOT_INFO
    await user_app.start()
    await bot_app.start()
    print("Both Userbot and Target Bot are running!")
    
    BOT_INFO = await bot_app.get_me()
    
    try:
        await user_app.send_message(BOT_INFO.username, "/start")
        await asyncio.sleep(1)
    except Exception:
        pass

    print("Step 1: Userbot is scanning recent chats to rebuild its own cache...")
    try:
        async for dialog in user_app.get_dialogs(limit=100):
            pass
    except Exception:
        pass

    print("Step 2: Syncing Bot Cache (Silent Ping Method)...")
    try:
        ping_msg = await user_app.send_message(DESTINATION_CHAT_ID, ".", disable_notification=True)
        await asyncio.sleep(1)
        await ping_msg.delete() 
        print("Cache sync complete!")
    except Exception as e:
        print(f"⚠️ Cache sync failed: {e}")

    print("Step 3: Fetching the LAST message sent by the target bot...")
    try:
        last_message_found = False
        async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=50):
            if msg.from_user and msg.from_user.id == TARGET_BOT_ID:
                print(f"Found the most recent bot message (ID: {msg.id})! Forwarding it now...")
                await process_and_send_message(msg)
                last_message_found = True
                break 
                
        if not last_message_found:
            print("No recent messages found from the bot in the group history.")
    except Exception as e:
        print(f"Could not fetch last message: {e}")

    print("Live monitoring is now active...")
    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
