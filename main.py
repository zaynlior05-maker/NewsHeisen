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
BOT_PEER_ID = None
USER_PEER_ID = None
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
def is_target_bot(msg) -> bool:
    if msg.from_user and msg.from_user.id == TARGET_BOT_ID: return True
    if msg.sender_chat and msg.sender_chat.id == TARGET_BOT_ID: return True
    if msg.forward_from and msg.forward_from.id == TARGET_BOT_ID: return True
    if msg.forward_from_chat and msg.forward_from_chat.id == TARGET_BOT_ID: return True
        
    raw_content = msg.text or msg.caption or ""
    if "@HeisenNewsBot" in raw_content: return True
    return False

def is_excluded(text: str) -> bool:
    if not text: return False
    return any(phrase.lower() in text.lower() for phrase in EXCLUDED_PHRASES)

def clean_and_brand_text(text_html: str) -> str:
    if not text_html: return ""
    
    cleaned = re.sub(r'<a[^>]*href="https://t\.me/[^>]*>[^<]*</a>', '', text_html)
    cleaned = re.sub(r'\B@[\w_]+', '', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned).strip()
    
    if MY_CHANNEL or MY_ADMIN:
        signature_parts = []
        if MY_CHANNEL: signature_parts.append(MY_CHANNEL)
        if MY_ADMIN: signature_parts.append(MY_ADMIN)
        signature = "\n\n" + " / ".join(signature_parts)
        cleaned += signature
        
    return cleaned

# ==========================================
# 7. THE SMART POSTER (BOT INBOX HANDLER)
# ==========================================
async def process_bot_album(group_id):
    """Waits for all forwarded album parts, cleans caption, and posts cleanly."""
    try:
        await asyncio.sleep(3.0) 
        messages = BOT_INBOX_ALBUMS.pop(group_id, [])
        if not messages: return

        print(f"🛠️ BOT INBOX: Reconstructing Album with {len(messages)} items...")
        messages.sort(key=lambda x: x.id)
        
        media_list = []
        has_set_caption = False
        
        for msg in messages:
            caption = ""
            if not has_set_caption:
                raw_caption = msg.caption.html if msg.caption else ""
                caption = clean_and_brand_text(raw_caption) if raw_caption else clean_and_brand_text(" ")
                has_set_caption = True
                
            # Extract zero-upload file IDs from the forwarded messages
            if msg.photo: media_list.append(InputMediaPhoto(media=msg.photo.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
            elif msg.video: media_list.append(InputMediaVideo(media=msg.video.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
            elif msg.document: media_list.append(InputMediaDocument(media=msg.document.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))

        if media_list:
            await bot_app.send_media_group(DESTINATION_CHAT_ID, media_list)
            print("✅ SUCCESS: Bot posted the completely grouped ALBUM!")

        # Clean up Inbox
        for msg in messages:
            try: await msg.delete()
            except Exception: pass

    except Exception as e:
        print(f"❌ CRITICAL ERROR IN ALBUM TASK: {e}")

@bot_app.on_message()
async def bot_inbox_handler(client, message):
    global USER_PEER_ID
    try:
        # Ignore anything that doesn't come directly from your Userbot
        if USER_PEER_ID and message.chat.id != USER_PEER_ID: return

        # Catch the invisible startup ping and discard it safely
        if message.text == ".":
            print("✅ Bot received the Forward Ping and learned the Destination ID!")
            await message.delete()
            return

        if not message.media: return

        # ALBUM CATCHER
        if message.media_group_id:
            print(f"📦 BOT INBOX CATCH: Album Piece (ID: {message.id})")
            if message.media_group_id not in BOT_INBOX_ALBUMS:
                BOT_INBOX_ALBUMS[message.media_group_id] = []
                asyncio.create_task(process_bot_album(message.media_group_id))
            BOT_INBOX_ALBUMS[message.media_group_id].append(message)
            return
            
        # SINGLE MEDIA CATCHER
        print("⚡️ BOT INBOX CATCH: Received single relayed media!")
        raw_caption = message.caption.html if message.caption else ""
        new_caption = clean_and_brand_text(raw_caption) if raw_caption else clean_and_brand_text(" ")
        
        if message.photo: await bot_app.send_photo(DESTINATION_CHAT_ID, message.photo.file_id, caption=new_caption, parse_mode=enums.ParseMode.HTML)
        elif message.video: await bot_app.send_video(DESTINATION_CHAT_ID, message.video.file_id, caption=new_caption, parse_mode=enums.ParseMode.HTML)
        elif message.document: await bot_app.send_document(DESTINATION_CHAT_ID, message.document.file_id, caption=new_caption, parse_mode=enums.ParseMode.HTML)
        
        print("✅ SUCCESS: Bot instantly posted the single media!")
        await message.delete()
        
    except Exception as e:
        print(f"❌ ERROR in bot_inbox_handler: {e}")

# ==========================================
# 8. THE DUMB FORWARDER (USERBOT SIDE)
# ==========================================
async def process_and_send_message(message):
    global BOT_PEER_ID
    
    # --- ALBUM LOGIC ---
    if message.media_group_id:
        if message.media_group_id in PROCESSED_SOURCE_ALBUMS: return 
        PROCESSED_SOURCE_ALBUMS.add(message.media_group_id)
        await asyncio.sleep(1.0)
        
        try:
            group_msgs = await user_app.get_media_group(message.chat.id, message.id)
            for g_msg in group_msgs:
                if is_excluded(g_msg.text or g_msg.caption or ""): return
            
            print(f"Relaying ALBUM {message.media_group_id} to Bot Inbox...")
            
            # THE FIX: Forwarding preserves media_group_id perfectly
            msg_ids = [m.id for m in group_msgs]
            sent = await user_app.forward_messages(BOT_PEER_ID, message.chat.id, msg_ids)
            print(f"✅ Userbot successfully passed {len(sent)} album pieces to Bot.")
        except Exception as e:
            print(f"❌ Error passing ALBUM to Bot Inbox: {e}")
        return

    # --- SINGLE MEDIA/TEXT LOGIC ---
    raw_content = message.text or message.caption or ""
    if is_excluded(raw_content): return

    if message.text:
        new_text = clean_and_brand_text(message.text.html)
        await bot_app.send_message(chat_id=DESTINATION_CHAT_ID, text=new_text, parse_mode=enums.ParseMode.HTML)
        print(f"✅ Bot directly sent text message from {message.chat.id}")

    elif message.media:
        print(f"Relaying single media {message.id} to Bot Inbox...")
        try:
            # THE FIX: Forwarding to let the Bot extract the zero-upload file_id
            await user_app.forward_messages(BOT_PEER_ID, message.chat.id, message.id)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"❌ Error passing media to Bot Inbox: {e}")

# ==========================================
# 9. LIVE MESSAGE EVENT HANDLER
# ==========================================
@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    if message.chat.id == SOURCE_GROUP_ID:
        if not is_target_bot(message): return  
    await process_and_send_message(message)

# ==========================================
# 10. STARTUP & THE "FORWARD PING" TRICK
# ==========================================
async def main():
    global BOT_PEER_ID, USER_PEER_ID
    await user_app.start()
    await bot_app.start()
    print("Both Userbot and Target Bot are running!")
    
    user_info = await user_app.get_me()
    USER_PEER_ID = user_info.id
    bot_info = await bot_app.get_me()
    
    print("Step 1: The 'Forward Ping' Trick to teach the Bot the Destination ID...")
    try:
        bot_user = await user_app.get_users(bot_info.username)
        BOT_PEER_ID = bot_user.id
        await user_app.send_message(BOT_PEER_ID, "/start")
        
        # 1. Userbot drops a completely invisible dot in the destination
        ping_msg = await user_app.send_message(DESTINATION_CHAT_ID, ".", disable_notification=True)
        # 2. Userbot forcibly forwards it to the Bot's Inbox so the Bot's database permanently saves the Destination ID
        await user_app.forward_messages(BOT_PEER_ID, DESTINATION_CHAT_ID, ping_msg.id)
        # 3. Clean up
        await asyncio.sleep(1)
        await ping_msg.delete() 
        print("✅ Core routing IDs permanently injected into Bot cache!")
    except Exception as e:
        print(f"⚠️ Initialization note: {e}")

    print("Step 2: Scanning history...")
    try:
        async for dialog in user_app.get_dialogs(limit=50): pass
    except Exception: pass

    print("Step 3: Fetching the LAST message sent by the target bot...")
    try:
        last_message_found = False
        async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=50):
            if is_target_bot(msg):
                print(f"Found the most recent bot message (ID: {msg.id})! Forwarding it now...")
                await process_and_send_message(msg)
                last_message_found = True
                break 
    except Exception as e:
        print(f"Could not fetch last message: {e}")

    print("Live monitoring is now active...")
    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
