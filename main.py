import os
import re
import asyncio
import traceback
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
BOT_INBOX_ALBUMS = {}          # group_id -> list[Message]
BOT_INBOX_LOCKS = {}           # group_id -> asyncio.Lock (per-group lock)
BOT_INBOX_LOCKS_GUARD = asyncio.Lock()  # protects creation of the per-group lock itself

EXCLUDED_PHRASES = ["( PAID AD )", "The giveaway has officially ended.", "Giveaway Entries"]
DESTINATION_INVITE_LINK = os.environ.get("DESTINATION_INVITE_LINK", "").strip()
SCRIPT_VERSION = "relay-v5-invite-link-2026-07-22"

# How long to wait after the LAST piece of an album arrives before flushing it.
# (debounce, not a fixed "wait once" sleep — resets every time a new piece arrives)
ALBUM_DEBOUNCE_SECONDS = 2.5

# 3. CLIENTS
user_app = Client("user_account", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)
bot_app = Client("my_bot", bot_token=MY_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

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
    LAST piece of this group arrived (not a fixed one-shot sleep), then sends
    the whole album. Only one instance of this ever runs per group_id because
    it's only spawned once, under the per-group lock, in bot_inbox_handler.
    """
    lock = await _get_group_lock(group_id)
    try:
        # Keep waiting as long as new pieces keep arriving.
        while True:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
            async with lock:
                bucket = BOT_INBOX_ALBUMS.get(group_id)
                if bucket is None:
                    return  # already flushed by someone else / nothing to do
                if bucket.get("dirty"):
                    # a new message arrived while we were sleeping — wait again
                    bucket["dirty"] = False
                    continue
                # No new arrivals during the debounce window -> flush now.
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

            if msg.photo:
                media_list.append(InputMediaPhoto(media=msg.photo.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
            elif msg.video:
                media_list.append(InputMediaVideo(media=msg.video.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
            elif msg.document:
                media_list.append(InputMediaDocument(media=msg.document.file_id, caption=caption, parse_mode=enums.ParseMode.HTML))
            # anything else (audio/voice/sticker) can't be grouped — skip it,
            # but don't let it silently swallow the whole album.

        if not media_list:
            print(f"⚠️ Album {group_id}: no groupable media found, nothing sent.")
        elif len(media_list) == 1:
            # Telegram rejects media groups with a single item — send it plainly.
            single = messages[0]
            await bot_app.copy_message(
                chat_id=DESTINATION_CHAT_ID,
                from_chat_id=single.chat.id,
                message_id=single.id,
                caption=media_list[0].caption,
                parse_mode=enums.ParseMode.HTML,
            )
            print("✅ SUCCESS: Bot relayed single leftover item to destination!")
        else:
            await bot_app.send_media_group(DESTINATION_CHAT_ID, media_list)
            print("✅ SUCCESS: Bot relayed ALBUM to destination!")

        for msg in messages:
            try:
                await msg.delete()
            except Exception:
                pass

    except Exception:
        print(f"❌ CRITICAL ERROR IN ALBUM TASK for group {group_id}:\n{traceback.format_exc()}")
        # make sure we don't leave a stuck entry behind
        BOT_INBOX_ALBUMS.pop(group_id, None)
    finally:
        BOT_INBOX_LOCKS.pop(group_id, None)

@bot_app.on_message(filters.private)
async def bot_inbox_handler(client, message):
    try:
        # Ignore/clean up the one-off "priming" forward used at startup to
        # cache the bot's peer for the destination chat — never relay it.
        if message.forward_from_chat and message.forward_from_chat.id == DESTINATION_CHAT_ID:
            try:
                await message.delete()
            except Exception:
                pass
            return

        if not message.media:
            return

        if message.media_group_id:
            group_id = message.media_group_id
            lock = await _get_group_lock(group_id)
            async with lock:
                bucket = BOT_INBOX_ALBUMS.get(group_id)
                if bucket is None:
                    bucket = {"messages": [], "dirty": False}
                    BOT_INBOX_ALBUMS[group_id] = bucket
                    # Spawn the flush task exactly once per group, atomically
                    # with creating the bucket, so there's no window where
                    # two tasks can be created for the same album.
                    asyncio.create_task(process_bot_album(group_id))
                else:
                    bucket["dirty"] = True  # tell the flush loop to keep waiting
                bucket["messages"].append(message)
            return

        await message.copy(chat_id=DESTINATION_CHAT_ID)
        await message.delete()
    except Exception:
        print(f"❌ ERROR in bot_inbox_handler:\n{traceback.format_exc()}")

# 6. USERBOT INTERCEPTOR
async def process_and_send_message(message):
    global BOT_PEER_ID
    if message.media_group_id:
        if message.media_group_id in PROCESSED_SOURCE_ALBUMS:
            return
        PROCESSED_SOURCE_ALBUMS.add(message.media_group_id)
        await asyncio.sleep(1.0)
        try:
            group_msgs = await user_app.get_media_group(message.chat.id, message.id)
            for g_msg in group_msgs:
                if is_excluded(g_msg.text or g_msg.caption or ""):
                    return
            await user_app.copy_media_group(BOT_PEER_ID, message.chat.id, message.id)
        except Exception:
            print(f"❌ Error passing ALBUM to Bot Inbox:\n{traceback.format_exc()}")
        return

    raw_content = message.text or message.caption or ""
    if is_excluded(raw_content):
        return

    if message.text:
        new_text = clean_and_brand_text(message.text.html)
        await bot_app.send_message(chat_id=DESTINATION_CHAT_ID, text=new_text, parse_mode=enums.ParseMode.HTML)
    elif message.media:
        caption_source = message.caption.html if message.caption else ""
        new_caption = clean_and_brand_text(caption_source) if caption_source else clean_and_brand_text(" ")
        try:
            await user_app.copy_message(chat_id=BOT_PEER_ID, from_chat_id=message.chat.id, message_id=message.id, caption=new_caption, parse_mode=enums.ParseMode.HTML)
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

    # IMPORTANT: sync the userbot's own dialog list FIRST. Pyrogram only caches
    # peer access_hashes for chats it has actually seen — a fresh/short-lived
    # session may not have DESTINATION_CHAT_ID cached yet even if the account
    # is genuinely a member. get_dialogs() forces a full sync of every chat
    # the userbot is in, which populates that cache.
    try:
        dialog_count = 0
        async for dialog in user_app.get_dialogs(limit=200):
            dialog_count += 1
        print(f"🔄 Synced {dialog_count} userbot dialogs.")
    except Exception:
        print(f"⚠️ Dialog sync failed:\n{traceback.format_exc()}")

    # Make sure the USERBOT itself can resolve the destination chat first —
    # export_chat_invite_link below depends on this working.
    try:
        await user_app.get_chat(DESTINATION_CHAT_ID)
    except Exception:
        print(f"⚠️ Userbot still can't see destination chat after dialog sync. "
              f"Double-check the userbot account is actually a member of {DESTINATION_CHAT_ID}.")

    # Make sure the BOT has a cached peer (access_hash) for the destination chat.
    # Bots can't resolve a raw numeric chat ID until they've seen a full Chat
    # object for it at least once. Preferred method: a real invite link for the
    # chat (set DESTINATION_INVITE_LINK) — this works for admins/members
    # without needing special export permissions. Fallback: forward-priming.
    try:
        await bot_app.get_chat(DESTINATION_CHAT_ID)
        print("✅ Bot already has destination chat cached.")
    except Exception:
        resolved = False

        if DESTINATION_INVITE_LINK:
            print("⚠️ Bot has no cached peer for destination chat — resolving via provided invite link...")
            try:
                chat = await bot_app.get_chat(DESTINATION_INVITE_LINK)
                print(f"✅ Peer cached for bot via invite link: {chat.id}")
                resolved = True
            except Exception as e:
                print(f"❌ Invite link resolution failed: {e}")
        else:
            print("ℹ️ No DESTINATION_INVITE_LINK set — skipping invite-link resolution.")

        if not resolved:
            print("⚠️ Falling back to forward-priming...")
            try:
                history = []
                async for m in user_app.get_chat_history(DESTINATION_CHAT_ID, limit=1):
                    history.append(m)
                if not history:
                    print("❌ Destination chat has no messages to forward for priming.")
                else:
                    await user_app.forward_messages(BOT_PEER_ID, DESTINATION_CHAT_ID, history[0].id)
                    await asyncio.sleep(2.0)
                    resolved_chat = await bot_app.get_chat(DESTINATION_CHAT_ID)
                    print(f"✅ Peer cached for bot via forward: {resolved_chat.id}")
                    resolved = True
            except Exception as inner_e:
                print(f"❌ Forward-priming also failed: {inner_e}")

        if not resolved:
            print("❌❌❌ COULD NOT RESOLVE DESTINATION PEER BY ANY METHOD. "
                  "Set DESTINATION_INVITE_LINK to a real invite link for this chat "
                  "(get it from the Telegram app: group info → Invite Link) and redeploy.")

    try:
        async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=50):
            if is_target_bot(msg):
                await process_and_send_message(msg)
                break
    except Exception:
        pass

    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
