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
    """Removes all @usernames/links and appends your own."""
    if not text_html:
        return ""
    
    # Remove Telegram links and raw @usernames
    cleaned = re.sub(r'<a[^>]*href="https://t\.me/[^>]*>[^<]*</a>', '', text_html)
    cleaned = re.sub(r'\B@[\w_]+', '', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned).strip()
    
    # Append your branding
    if MY_CHANNEL or MY_ADMIN:
        signature = "\n\n"
        if MY_CHANNEL:
            signature += f"📢 **Channel:** {MY_CHANNEL}\n"
        if MY_ADMIN:
            signature += f"👨‍💻 **Admin:** {MY_ADMIN}"
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
        new_caption = clean_and_brand_text(message.caption.html) if message.caption else clean_and_brand_text(" ")
        
        print(f"Downloading media for message {message.id}...")
        file_path = await message.download()
        
        print(f"Uploading media via Bot...")
        try:
            if message.photo:
                await bot_app.send_photo(DESTINATION_CHAT_ID, photo=file_path, caption=new_caption, parse_mode=enums.ParseMode.HTML)
            elif message.video:
                await bot_app.send_video(DESTINATION_CHAT_ID, video=file_path, caption=new_caption, parse_mode=enums.ParseMode.HTML)
            elif message.document:
                await bot_app.send_document(DESTINATION_CHAT_ID, document=file_path, caption=new_caption, parse_mode=enums.ParseMode.HTML)
            else:
                await bot_app.send_document(DESTINATION_CHAT_ID, document=file_path)
        finally:
            # Delete local file to free up Railway space
            if os.path.exists(file_path):
                os.remove(file_path)
                
        print(f"Bot sent media from {message.chat.id}")

# ==========================================
# 8. LIVE MESSAGE EVENT HANDLER
# ==========================================
@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    # Filter group messages down to only the Target Bot
    if message.chat.id == SOURCE_GROUP_ID:
        if not message.from_user or message.from_user.id != TARGET_BOT_ID:
            return  
    
    await process_and_send_message(message)

# ==========================================
# 9. STARTUP & FETCH LAST MESSAGE
# ==========================================
async def main():
    await user_app.start()
    await bot_app.start()
    print("Both Userbot and Target Bot are running!")
    
    # Cache Warmup
    print("Rebuilding group cache to fix Peer ID errors... Please wait.")
    try:
        async for dialog in user_app.get_dialogs(limit=200):
            pass
    except Exception as e:
        print(f"Userbot cache note: {e}")
        
    try:
        await bot_app.get_chat(DESTINATION_CHAT_ID)
    except Exception as e:
        print(f"Bot cache note: {e}")

    print("Cache successfully rebuilt! Ready to mirror messages.")
    
    # NEW: Fetch the last message on startup
    print("Fetching the LAST message sent by the target bot...")
    try:
        last_message_found = False
        # Look through the last 50 messages in the group
        async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=50):
            if msg.from_user and msg.from_user.id == TARGET_BOT_ID:
                print(f"Found the most recent bot message (ID: {msg.id})! Forwarding it now...")
                await process_and_send_message(msg)
                last_message_found = True
                break # Stop searching once we find the first one
                
        if not last_message_found:
            print("No recent messages found from the bot in the group history.")
    except Exception as e:
        print(f"Could not fetch last message: {e}")

    # Keep script running continuously
    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
