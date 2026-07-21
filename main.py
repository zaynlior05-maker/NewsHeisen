import os
import re
import asyncio
from pyrogram import Client, filters, enums, idle

# 1. Fetch credentials from Railway Variables
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"] 
MY_BOT_TOKEN = os.environ["MY_BOT_TOKEN"]

# 2. Fetch Chat IDs from Railway Variables
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
SOURCE_GROUP_ID = int(os.environ["SOURCE_GROUP_ID"])
DESTINATION_CHAT_ID = int(os.environ["DESTINATION_CHAT_ID"])
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"])

# 3. Your specific Usernames (Add these to Railway)
MY_CHANNEL = os.environ.get("MY_CHANNEL", "")
MY_ADMIN = os.environ.get("MY_ADMIN", "")

# 4. List of phrases to automatically ignore
EXCLUDED_PHRASES = [
    "( PAID AD )",
    "The giveaway has officially ended.",
    "Giveaway Entries"
]

# Initialize BOTH Clients
user_app = Client("user_account", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)
bot_app = Client("my_bot", bot_token=MY_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

def is_excluded(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(phrase.lower() in text_lower for phrase in EXCLUDED_PHRASES)

def clean_and_brand_text(text_html: str) -> str:
    """Removes all @usernames/links and appends your own."""
    if not text_html:
        return ""
    
    # Step 1: Remove any embedded Telegram links (e.g., t.me/...)
    cleaned = re.sub(r'<a[^>]*href="https://t\.me/[^>]*>[^<]*</a>', '', text_html)
    
    # Step 2: Remove any raw @usernames
    cleaned = re.sub(r'\B@[\w_]+', '', cleaned)
    
    # Step 3: Clean up any weird double spaces left behind by the deletions
    cleaned = re.sub(r' +', ' ', cleaned).strip()
    
    # Step 4: Append your own channel and admin to every message
    if MY_CHANNEL or MY_ADMIN:
        signature = "\n\n"
        if MY_CHANNEL:
            signature += f"📢 **Channel:** {MY_CHANNEL}\n"
        if MY_ADMIN:
            signature += f"👨‍💻 **Admin:** {MY_ADMIN}"
        cleaned += signature
        
    return cleaned

@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    
    # RULE: If from the GROUP, it MUST be from the specific bot
    if message.chat.id == SOURCE_GROUP_ID:
        if not message.from_user or message.from_user.id != TARGET_BOT_ID:
            return  

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
            if os.path.exists(file_path):
                os.remove(file_path)
                
        print(f"Bot sent media from {message.chat.id}")

# Run both clients simultaneously
async def main():
    await user_app.start()
    await bot_app.start()
    print("Both Userbot and Target Bot are running!")
    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
