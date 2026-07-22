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
# 4. GLOBALS & EXCLUDED PHRASES
# ==========================================
BOT_INFO = None

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
@bot_app.on_message(filters.private)
async def bot_inbox_handler(client, message):
    """If ANY media drops into the Bot's inbox, it instantly copies it to the destination."""
    
    # We ignore text commands like /start
    if not message.media:
        return
        
    print("⚡️ BOT INBOX CATCH: Received relayed media from Userbot!")
    try:
        # Instantly post to destination
        await message.copy(chat_id=DESTINATION_CHAT_ID)
        print("✅ SUCCESS: Bot instantly relayed the media to the Destination!")
        
        # Clean up the inbox
        await message.delete()
    except Exception as e:
        print(f"❌ ERROR: Bot failed to post relayed media: {e}")

# ==========================================
# 8. CORE MESSAGE PROCESSOR (USERBOT SIDE)
# ==========================================
async def process_and_send_message(message):
    raw_content = message.text or message.caption or ""

    if is_excluded(raw_content):
        print(f"Skipped message {message.id} (matched excluded phrase)")
        return

    # Process Text Messages Normally
    if message.text:
        new_text = clean_and_brand_text(message.text.html)
        await bot_app.send_message(
            chat_id=DESTINATION_CHAT_ID, 
            text=new_text,
            parse_mode=enums.ParseMode.HTML
        )
        print(f"Bot directly sent text message from {message.chat.id}")

    # Process Media (Zero Download/Upload Relay)
    elif message.media:
        caption_source = message.caption.html if message.caption else ""
        new_caption = clean_and_brand_text(caption_source) if caption_source else clean_and_brand_text(" ")
        
        print(f"Relaying media for message {message.id} to Bot Inbox...")
        try:
            # Userbot silently copies the media to the Bot's DMs and attaches the new caption
            await user_app.copy_message(
                chat_id=BOT_INFO.username,
                from_chat_id=message.chat.id,
                message_id=message.id,
                caption=new_caption,
                parse_mode=enums.ParseMode.HTML
            )
            
            # Tiny 2-second pause to let the Bot Inbox Catch print its success log cleanly
            await asyncio.sleep(2)
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
    
    if message.media_group_id:
        await asyncio.sleep(1.0)
        
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
    
    # Send a quick start ping to ensure DMs are open
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
        # Make the bot aware of the chat ID
        await bot_app.get_chat(DESTINATION_CHAT_ID)
        print("Cache sync complete!")
    except Exception as e:
        print(f"⚠️ Cache sync note: {e}")

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
