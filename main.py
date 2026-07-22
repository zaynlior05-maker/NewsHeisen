import os
import re
import json
import asyncio
import traceback
import urllib.request
import urllib.error
from pyrogram import Client, filters, enums, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument

# 1. CREDENTIALS & CHAT IDs
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
MY_BOT_TOKEN = os.environ["MY_BOT_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
SOURCE_GROUP_ID = int(os.environ["SOURCE_GROUP_ID"])
DESTINATION_CHAT_ID = int(os.environ["DESTINATION_CHAT_ID"])
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"])
MY_CHANNEL = os.environ.get("MY_CHANNEL", "")
MY_ADMIN = os.environ.get("MY_ADMIN", "")

# 2. GLOBALS
BOT_PEER_ID = None
PROCESSED_SOURCE_ALBUMS = set()

# Album re-assembly state, guarded by a lock to avoid the race condition
# that was causing lost / duplicated / silently-dropped albums.
BOT_INBOX_ALBUMS = {}          # group_id -> {"messages": [...], "dirty": bool}
BOT_INBOX_LOCKS = {}           # group_id -> asyncio.Lock (per-group lock)
BOT_INBOX_LOCKS_GUARD = asyncio.Lock()  # protects creation of the per-group lock itself

EXCLUDED_PHRASES = ["( PAID AD )", "The giveaway has officially ended.", "Giveaway Entries"]
SCRIPT_VERSION = "relay-v8-full-tracing-2026-07-22"

# How long to wait after the LAST piece of an album arrives before flushing it.
ALBUM_DEBOUNCE_SECONDS = 2.5

# 3. CLIENTS
user_app = Client("user_account", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)
bot_app = Client("my_bot", bot_token=MY_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# 3b. RAW BOT API (HTTPS) — used ONLY for sending to DESTINATION_CHAT_ID.
# This completely bypasses Pyrogram's MTProto peer-cache requirement, because
# the classic Bot API resolves chat_id server-side (the bot just needs to be
# a current member — no local access_hash needed). This is what actually
# fixes "Peer id invalid" for good, since that error is purely a client-side
# MTProto limitation that invite links and forwards can't reliably solve for
# bot accounts (bots can't even call checkChatInvite — BOT_METHOD_INVALID).
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{MY_BOT_TOKEN}"

def _bot_api_call_sync(method: str, payload: dict):
    url = f"{TELEGRAM_API_BASE}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError(f"HTTP {e.code} calling {method}: {body}")

async def bot_api_call(method: str, payload: dict):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _bot_api_call_sync, method, payload)
    if not result.get("ok"):
        raise RuntimeError(f"Bot API error in {method}: {result}")
    return result.get("result")

async def bot_api_send_text(chat_id, text):
    return await bot_api_call("sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML"
    })

async def bot_api_copy_message(chat_id, from_chat_id, message_id, caption=None):
    payload = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    if caption is not None:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    return await bot_api_call("copyMessage", payload)

async def bot_api_send_media_group(chat_id, media_items):
    return await bot_api_call("sendMediaGroup", {"chat_id": chat_id, "media": media_items})

# 4. HELPERS
def is_target_bot(msg) -> bool:
    if msg.from_user and msg.from_user.id == TARGET_BOT_ID: return True
    if msg.sender_chat and msg.sender_chat.id == TARGET_BOT_ID: return True
    if msg.forward_from and msg.forward_from.id == TARGET_BOT_ID: return True
    if msg.forward_from_chat and msg.forward_from_chat.id == TARGET_BOT_ID: return True
    if "@HeisenNewsBot" in (msg.text or msg.caption or ""): return True
    return False

def is_excluded(text: str) -> bool:
    if not text: return False
    return any(phrase.lower() in text.lower() for phrase in EXCLUDED_PHRASES)

def clean_and_brand_text(text_html: str) -> str:
    if not text_html: return ""
    cleaned = re.sub(r'<a[^>]*href="https://t\.me/[^>]*>[^<]*</a>', '', text_html)
    cleaned = re.sub(r'\B@[\w_]+', '', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned).strip()

    signature_parts = []
    if MY_CHANNEL: signature_parts.append(MY_CHANNEL)
    if MY_ADMIN: signature_parts.append(MY_ADMIN)
    if signature_parts: cleaned += "\n\n" + " / ".join(signature_parts)
    return cleaned

def _build_media_item(msg, caption):
    """Build a Bot API media-group item dict from a Pyrogram message."""
    item = None
    if msg.photo:
        item = {"type": "photo", "media": msg.photo.file_id}
    elif msg.video:
        item = {"type": "video", "media": msg.video.file_id}
    elif msg.document:
        item = {"type": "document", "media": msg.document.file_id}
    if item is not None and caption:
        item["caption"] = caption
        item["parse_mode"] = "HTML"
    return item

async def _get_group_lock(group_id):
    """Lazily create (once) a lock dedicated to this media_group_id."""
    async with BOT_INBOX_LOCKS_GUARD:
        if group_id not in BOT_INBOX_LOCKS:
            BOT_INBOX_LOCKS[group_id] = asyncio.Lock()
        return BOT_INBOX_LOCKS[group_id]

# 5. BOT INBOX LISTENER (ALBUM RE-ASSEMBLY)
async def process_bot_album(group_id):
    """
    Debounced flush: waits until ALBUM_DEBOUNCE_SECONDS has passed since the
    LAST piece of this group arrived, then sends the whole album via the raw
    Bot API (HTTPS) — no Pyrogram MTProto peer resolution needed for the
    destination chat. Only one instance of this ever runs per group_id.
    """
    lock = await _get_group_lock(group_id)
    try:
        while True:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
            async with lock:
                bucket = BOT_INBOX_ALBUMS.get(group_id)
                if bucket is None:
                    return
                if bucket.get("dirty"):
                    bucket["dirty"] = False
                    continue
                messages = bucket["messages"]
                del BOT_INBOX_ALBUMS[group_id]
                break

        if not messages:
            return

        print(f"🛠️ BOT INBOX: Processing Album Group {group_id} with {len(messages)} items...")
        messages.sort(key=lambda x: x.id)

        media_list = []
        has_set_caption = False

        for msg in messages:
            caption = ""
            if not has_set_caption:
                raw_caption = msg.caption.html if msg.caption else ""
                caption = clean_and_brand_text(raw_caption) if raw_caption else clean_and_brand_text(" ")
                has_set_caption = True

            item = _build_media_item(msg, caption)
            if item:
                media_list.append(item)
            # anything else (audio/voice/sticker) can't be grouped — skipped,
            # but doesn't swallow the rest of the album.

        if not media_list:
            print(f"⚠️ Album {group_id}: no groupable media found, nothing sent.")
        elif len(media_list) == 1:
            # Telegram rejects media groups with a single item — send it plainly.
            single = messages[0]
            await bot_api_copy_message(
                chat_id=DESTINATION_CHAT_ID,
                from_chat_id=single.chat.id,
                message_id=single.id,
                caption=media_list[0].get("caption"),
            )
            print("✅ SUCCESS: Bot relayed single leftover item to destination!")
        else:
            await bot_api_send_media_group(DESTINATION_CHAT_ID, media_list)
            print("✅ SUCCESS: Bot relayed ALBUM to destination!")

        for msg in messages:
            try:
                await msg.delete()
            except Exception:
                pass

    except Exception:
        print(f"❌ CRITICAL ERROR IN ALBUM TASK for group {group_id}:\n{traceback.format_exc()}")
        BOT_INBOX_ALBUMS.pop(group_id, None)
    finally:
        BOT_INBOX_LOCKS.pop(group_id, None)

@bot_app.on_message(filters.private)
async def bot_inbox_handler(client, message):
    try:
        print(f"📥 Bot inbox received message #{message.id} "
              f"(media_group_id={message.media_group_id}, has_media={bool(message.media)}) from chat {message.chat.id}")

        if not message.media:
            print(f"ℹ️ Message #{message.id} has no media — ignoring.")
            return

        if message.media_group_id:
            group_id = message.media_group_id
            lock = await _get_group_lock(group_id)
            async with lock:
                bucket = BOT_INBOX_ALBUMS.get(group_id)
                if bucket is None:
                    bucket = {"messages": [], "dirty": False}
                    BOT_INBOX_ALBUMS[group_id] = bucket
                    asyncio.create_task(process_bot_album(group_id))
                else:
                    bucket["dirty"] = True
                bucket["messages"].append(message)
            return

        # Single media item — relay via raw Bot API, then clean up the inbox copy.
        caption_source = message.caption.html if message.caption else ""
        caption = clean_and_brand_text(caption_source) if caption_source else None
        await bot_api_copy_message(
            chat_id=DESTINATION_CHAT_ID,
            from_chat_id=message.chat.id,
            message_id=message.id,
            caption=caption,
        )
        print(f"✅ SUCCESS: Bot relayed single media #{message.id} to destination!")
        await message.delete()
    except Exception:
        print(f"❌ ERROR in bot_inbox_handler:\n{traceback.format_exc()}")

# 6. USERBOT INTERCEPTOR
async def process_and_send_message(message):
    global BOT_PEER_ID
    if message.media_group_id:
        if message.media_group_id in PROCESSED_SOURCE_ALBUMS:
            print(f"ℹ️ Album group {message.media_group_id} already processed, skipping.")
            return
        PROCESSED_SOURCE_ALBUMS.add(message.media_group_id)
        await asyncio.sleep(1.0)
        try:
            group_msgs = await user_app.get_media_group(message.chat.id, message.id)
            print(f"ℹ️ Fetched {len(group_msgs)} messages for album group {message.media_group_id}.")
            for g_msg in group_msgs:
                if is_excluded(g_msg.text or g_msg.caption or ""):
                    print(f"🚫 Album {message.media_group_id} excluded by phrase filter.")
                    return
            await user_app.copy_media_group(BOT_PEER_ID, message.chat.id, message.id)
            print(f"✅ Userbot copied album {message.media_group_id} into bot's inbox — waiting for bot to relay it.")
        except Exception:
            print(f"❌ Error passing ALBUM to Bot Inbox:\n{traceback.format_exc()}")
        return

    raw_content = message.text or message.caption or ""
    if is_excluded(raw_content):
        print(f"🚫 Message #{message.id} excluded by phrase filter.")
        return

    if message.text:
        new_text = clean_and_brand_text(message.text.html)
        try:
            await bot_api_send_text(DESTINATION_CHAT_ID, new_text)
            print(f"✅ SUCCESS: Text message #{message.id} sent directly to destination via Bot API.")
        except Exception:
            print(f"❌ Error sending text to destination via Bot API:\n{traceback.format_exc()}")
    elif message.media:
        caption_source = message.caption.html if message.caption else ""
        new_caption = clean_and_brand_text(caption_source) if caption_source else clean_and_brand_text(" ")
        try:
            await user_app.copy_message(chat_id=BOT_PEER_ID, from_chat_id=message.chat.id, message_id=message.id, caption=new_caption, parse_mode=enums.ParseMode.HTML)
            print(f"✅ Userbot copied single media #{message.id} into bot's inbox — waiting for bot to relay it.")
            await asyncio.sleep(2)
        except Exception:
            print(f"❌ Error passing single media to Bot Inbox:\n{traceback.format_exc()}")

@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    if message.chat.id == SOURCE_GROUP_ID:
        if not is_target_bot(message):
            return
    await process_and_send_message(message)

# 7. STARTUP & CACHE WARMUP
async def main():
    global BOT_PEER_ID
    print(f"🚀 SCRIPT VERSION: {SCRIPT_VERSION}")
    await user_app.start()
    await bot_app.start()

    bot_info = await bot_app.get_me()
    try:
        bot_user = await user_app.get_users(bot_info.username)
        BOT_PEER_ID = bot_user.id
        await user_app.send_message(BOT_PEER_ID, "/start")
    except Exception as e:
        print(f"⚠️ Initialization note: {e}")

    try:
        dialog_count = 0
        async for dialog in user_app.get_dialogs(limit=200):
            dialog_count += 1
        print(f"🔄 Synced {dialog_count} userbot dialogs.")
    except Exception:
        print(f"⚠️ Dialog sync failed:\n{traceback.format_exc()}")

    # Quick sanity check only — no longer required for anything to work,
    # since destination sends now go through the raw Bot API (HTTPS), which
    # needs no local peer cache at all.
    try:
        info = await bot_api_call("getChat", {"chat_id": DESTINATION_CHAT_ID})
        print(f"✅ Bot API confirms access to destination chat: {info.get('title', info.get('id'))}")
    except Exception as e:
        print(f"⚠️ Bot API getChat check failed (bot may not actually be a member/admin there): {e}")

    # Catch-up: find the most recent qualifying message across BOTH the
    # source group (must pass is_target_bot) and the source channel (all
    # messages there are relayed, per monitor_and_forward's logic), and
    # send whichever one is actually newest. Verbose logging so we can see
    # exactly what was found instead of debugging blind.
    async def _find_latest_group_candidate():
        try:
            async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=50):
                if is_target_bot(msg):
                    return msg
        except Exception:
            print(f"⚠️ Could not read SOURCE_GROUP_ID history:\n{traceback.format_exc()}")
        return None

    async def _find_latest_channel_candidate():
        try:
            async for msg in user_app.get_chat_history(SOURCE_CHANNEL_ID, limit=1):
                return msg
        except Exception:
            print(f"⚠️ Could not read SOURCE_CHANNEL_ID history:\n{traceback.format_exc()}")
        return None

    try:
        group_msg = await _find_latest_group_candidate()
        channel_msg = await _find_latest_channel_candidate()

        print(f"ℹ️ Catch-up scan — group candidate: "
              f"{'msg #' + str(group_msg.id) + ' @ ' + str(group_msg.date) if group_msg else 'none found'}")
        print(f"ℹ️ Catch-up scan — channel candidate: "
              f"{'msg #' + str(channel_msg.id) + ' @ ' + str(channel_msg.date) if channel_msg else 'none found'}")

        candidates = [m for m in (group_msg, channel_msg) if m is not None]
        if not candidates:
            print("ℹ️ No catch-up candidate found in either source group or source channel.")
        else:
            latest = max(candidates, key=lambda m: m.date)
            print(f"🚚 Sending catch-up message from chat {latest.chat.id}, msg #{latest.id}...")
            await process_and_send_message(latest)
    except Exception:
        print(f"❌ Catch-up step failed:\n{traceback.format_exc()}")

    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
