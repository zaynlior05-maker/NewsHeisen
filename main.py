import os
import re
import asyncio
from pyrogram import Client, filters, enums, idle

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
# 4. EXCLUDED PHRASES
# ==========================================
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
    """Checks if the text contains any forbidden phrases."""
    if not text:
        return False
    text_lower = text.lower()
    return any(phrase.lower() in text_lower for phrase in EXCLUDED_PHRASES)

def clean_and_brand_text(text_html: str) -> str:
    """Removes all @usernames/links and appends your clean signature format."""
    if not text_html:
        return ""
    
    # Remove Telegram links and raw @usernames
    cleaned = re.sub(r'<a[^>]*href="https://t\.me/[^>]*>[^<]*</a>', '', text_html)
    cleaned = re.sub(r'\B@[\w_]+', '', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned).strip()
    
    # Append your clean branding format: @channel / @admin
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
# 7. CORE MESSAGE PROCESSOR
# ==========================================
async def process_and_send_message(message):
    """Handles the actual downloading, cleaning, and sending."""
    raw_content = message.text or message.caption or ""

    if is_excluded(raw_content):
        print(f"Skipped message {message.id} (matched excluded phrase)")
        return

    # Process Text Messages
    if message.text:
        new_text = clean_and_brand_text(message.text.html)
        await bot_app.send_message(
            chat_id=DESTINATION_CHAT_ID, 
            text=new_text,
            parse_mode=enums.ParseMode.HTML
        )
        print(f"Bot sent text message from {message.chat.id}")

    # Process Media (Photos, Videos, Documents)
    elif message.media:
        caption_source = message.caption.html if message.caption else ""
        new_caption = clean_and_brand_text(caption_source) if caption_source else clean_and_brand_text(" ")
        
        print(f"Downloading media for message {message.id} to RAM...")
        try:
            # NEW: Download directly into RAM (in_memory=True) to bypass Railway disk freezes
            file_data = await message.download(in_memory=True)
            
            # Telegram API requires a dummy filename to understand the bytes
            if message.photo:
                file_data.name = "image.jpg"
            elif message.video:
                file_data.name = "video.mp4"
            else:
                file_data.name = "document.file"

            print(f"Uploading media via Bot...")
            
            async def execute_upload():
                if message.photo:
                    await bot_app.send_photo(DESTINATION_CHAT_ID, photo=file_data, caption=new_caption, parse_mode=enums.ParseMode.HTML)
                elif message.video:
                    await bot_app.send_video(DESTINATION_CHAT_ID, video=file_data, caption=new_caption, parse_mode=enums.ParseMode.HTML)
                elif message.document:
                    await bot_app.send_document(DESTINATION_CHAT_ID, document=file_data, caption=new_caption, parse_mode=enums.ParseMode.HTML)
                else:
                    await bot_app.send_document(DESTINATION_CHAT_ID, document=file_data)

            # Timeout safeguard remains just in case Telegram servers are slow
            await asyncio.wait_for(execute_upload(), timeout=45.0)
            print(f"Bot successfully processed media from {message.chat.id}")
            
        except asyncio.TimeoutError:
            print(f"⚠️ TIMEOUT ERROR: The upload for message {message.id} took too long. Skipping.")
        except Exception as e:
            print(f"Error processing media message {message.id}: {e}")

# ==========================================
# 8. LIVE MESSAGE EVENT HANDLER
# ==========================================
@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    # Filter group messages down to only the Target Bot
    if message.chat.id == SOURCE_GROUP_ID:
        if not message.from_user or message.from_user.id != TARGET_BOT_ID:
            return  
    
    # Buffer delay to help organize multi-photo albums
    if message.media_group_id:
        await asyncio.sleep(1.0)
        
    await process_and_send_message(message)

# ==========================================
# 9. STARTUP, CACHE WARMUP & FETCH LAST MESSAGE
# ==========================================
async def main():
    await user_app.start()
    await bot_app.start()
    print("Both Userbot and Target Bot are running!")
    
    # Step 1: Userbot Scans Memory
    print("Step 1: Userbot is scanning recent chats to rebuild its own cache...")
    try:
        async for dialog in user_app.get_dialogs(limit=100):
            pass
    except Exception as e:
        print(f"Userbot dialog scan note: {e}")

    # Step 2: Ghost Ping Trick
    print("Step 2: Executing the ghost ping to sync the Destination Chat for the Bot...")
    try:
        ping_msg = await user_app.send_message(DESTINATION_CHAT_ID, "🔄 [System] Syncing bot cache...")
        await asyncio.sleep(3) 
        await ping_msg.delete()
        print("Cache sync complete! The Bot has successfully resolved the Destination Chat.")
    except Exception as e:
        print(f"⚠️ Ghost ping failed: {e}")

    # Step 3: Fetch Last Message
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
