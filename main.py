import os
import re
import json
import asyncio
import traceback
import urllib.request
import urllib.error
from pyrogram import Client, filters, enums, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from pyrogram.errors import FloodWait

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

# Album re-assembly state, guarded by a lock to avoid the race condition
# that was causing lost / duplicated / silently-dropped albums.
BOT_INBOX_ALBUMS = {}          # group_id -> {"messages": [...], "dirty": bool}
BOT_INBOX_LOCKS = {}           # group_id -> asyncio.Lock (per-group lock)
BOT_INBOX_LOCKS_GUARD = asyncio.Lock()  # protects creation of the per-group lock itself

EXCLUDED_PHRASES = ["( PAID AD )", "The giveaway has officially ended.", "Giveaway Entries"]
SCRIPT_VERSION = "relay-v11-periodic-safety-sweep-2026-07-23"

# workdir/session dir points at a persistent volume (mount one in Railway:
# Settings -> Volumes -> mount path /app/sessions) so .session files AND
# our state file both survive restarts.
SESSION_DIR = os.environ.get("SESSION_DIR", "/app/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

# --- Persistent state (survives restarts via the mounted volume) ---
# Tracks the last message ID we've successfully attempted per source chat,
# plus which album groups we've already handled. This lets startup backfill
# EVERYTHING missed while the container was down, not just the latest post.
STATE_FILE = os.path.join(SESSION_DIR, "relay_state.json")
STATE_LOCK = asyncio.Lock()
_IS_FIRST_RUN = not os.path.exists(STATE_FILE)

def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("last_group_id", 0)
    data.setdefault("last_channel_id", 0)
    data.setdefault("processed_albums", [])
    return data

def _save_state_sync(data):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)

STATE = _load_state()
PROCESSED_SOURCE_ALBUMS = set(STATE.get("processed_albums", []))

async def _mark_processed(chat_id, msg_id):
    """
    Persist that we've attempted this message, so a restart never re-scans
    (or worse, loses track of) anything. Called after EVERY message attempt,
    success or failure, so the backfill pointer always advances and the
    pipeline can't get permanently stuck on one bad message while silently
    skipping everything newer.
    """
    async with STATE_LOCK:
        if chat_id == SOURCE_GROUP_ID and msg_id > STATE.get("last_group_id", 0):
            STATE["last_group_id"] = msg_id
        elif chat_id == SOURCE_CHANNEL_ID and msg_id > STATE.get("last_channel_id", 0):
            STATE["last_channel_id"] = msg_id
        STATE["processed_albums"] = list(PROCESSED_SOURCE_ALBUMS)[-1000:]
        snapshot = dict(STATE)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _save_state_sync, snapshot)

# How long to wait after the LAST piece of an album arrives before flushing it.
ALBUM_DEBOUNCE_SECONDS = 2.5

BACKFILL_SCAN_LIMIT = 300      # how far back to look, in messages, per source
PERIODIC_SWEEP_SECONDS = 180   # defense-in-depth re-check interval while running

# 3. CLIENTS
# Without a persisted workdir, every redeploy forces a brand-new bot login,
# and enough of those in a row triggers Telegram's FloodWait on
# auth.ImportBotAuthorization (exactly what happened earlier).

user_app = Client("user_account", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH, workdir=SESSION_DIR)
bot_app = Client("my_bot", bot_token=MY_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH, workdir=SESSION_DIR)

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
    try:
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
    finally:
        # Always advance the persisted watermark, success or failure, so a
        # restart backfills forward from here rather than losing track or
        # getting stuck retrying the same message forever.
        await _mark_processed(message.chat.id, message.id)

@user_app.on_message(filters.chat([SOURCE_CHANNEL_ID, SOURCE_GROUP_ID]))
async def monitor_and_forward(client, message):
    if message.chat.id == SOURCE_GROUP_ID:
        if not is_target_bot(message):
            await _mark_processed(message.chat.id, message.id)
            return
    await process_and_send_message(message)

async def _collect_group_backfill():
    since_id = STATE.get("last_group_id", 0)
    found = []
    try:
        async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=BACKFILL_SCAN_LIMIT):
            if msg.id <= since_id:
                break
            if is_target_bot(msg):
                found.append(msg)
    except Exception:
        print(f"⚠️ Could not read SOURCE_GROUP_ID history:\n{traceback.format_exc()}")
    found.sort(key=lambda m: m.id)
    return found

async def _collect_channel_backfill():
    since_id = STATE.get("last_channel_id", 0)
    found = []
    try:
        async for msg in user_app.get_chat_history(SOURCE_CHANNEL_ID, limit=BACKFILL_SCAN_LIMIT):
            if msg.id <= since_id:
                break
            found.append(msg)
    except Exception:
        print(f"⚠️ Could not read SOURCE_CHANNEL_ID history:\n{traceback.format_exc()}")
    found.sort(key=lambda m: m.id)
    return found

async def _run_backlog_sweep(label):
    """
    Shared by both the startup backfill and the periodic safety-net sweep.
    Only ever looks at messages newer than the persisted watermark (which
    live processing also advances via _mark_processed), so this can run
    repeatedly forever without ever double-posting anything.
    """
    group_backlog = await _collect_group_backfill()
    channel_backlog = await _collect_channel_backfill()
    all_backlog = sorted(group_backlog + channel_backlog, key=lambda m: m.date)
    if all_backlog:
        print(f"🔁 {label}: found {len(all_backlog)} missed post(s) — sending now.")
    for msg in all_backlog:
        print(f"🚚 {label}: sending message from chat {msg.chat.id}, msg #{msg.id}...")
        await process_and_send_message(msg)
        await asyncio.sleep(1.0)  # gentle pacing to avoid flooding on big backlogs
    return len(all_backlog)

async def periodic_safety_sweep_loop():
    """
    Defense-in-depth for while the container is continuously running: live
    updates (monitor_and_forward) should catch every new post in real time,
    but this independently re-checks both sources every PERIODIC_SWEEP_SECONDS
    and sends anything that somehow slipped through — e.g. a brief MTProto
    reconnect gap. Runs quietly and does nothing when there's nothing missed.
    """
    while True:
        await asyncio.sleep(PERIODIC_SWEEP_SECONDS)
        try:
            await _run_backlog_sweep("Periodic sweep")
        except Exception:
            print(f"⚠️ Periodic sweep failed:\n{traceback.format_exc()}")

async def _start_with_flood_wait_retry(client, label):
    """
    Start a Pyrogram client, and if Telegram returns a FloodWait, sleep the
    required duration INSIDE this same running process and retry — rather
    than crashing (which lets Railway restart the container and fire off yet
    another login attempt mid-wait, needlessly repeating/risking extending
    the block). The container stays "up" the whole time from Railway's POV.
    """
    while True:
        try:
            await client.start()
            print(f"✅ {label} started successfully.")
            return
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 60))
            print(f"⏳ {label}: FloodWait — sleeping {wait_s + 10}s before retrying login "
                  f"(container staying alive, NOT crashing/restarting)...")
            await asyncio.sleep(wait_s + 10)

# 7. STARTUP & CACHE WARMUP
async def main():
    global BOT_PEER_ID
    print(f"🚀 SCRIPT VERSION: {SCRIPT_VERSION}")
    await _start_with_flood_wait_retry(user_app, "Userbot")
    await _start_with_flood_wait_retry(bot_app, "Bot")

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

    # Backfill: on first-ever run (no state file yet), just record the
    # current latest message ID per source as a baseline — don't resend
    # existing old backlog. On every run AFTER that, send EVERYTHING newer
    # than the last persisted ID for each source, in chronological order,
    # so nothing posted while the container was down/restarting is lost.
    if _IS_FIRST_RUN:
        print("ℹ️ First-ever run detected (no persisted state) — recording a baseline, not resending old backlog.")
        try:
            async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=1):
                STATE["last_group_id"] = msg.id
            async for msg in user_app.get_chat_history(SOURCE_CHANNEL_ID, limit=1):
                STATE["last_channel_id"] = msg.id
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _save_state_sync, dict(STATE))
            print(f"✅ Baseline recorded: last_group_id={STATE['last_group_id']}, last_channel_id={STATE['last_channel_id']}")
        except Exception:
            print(f"⚠️ Could not record baseline:\n{traceback.format_exc()}")
    else:
        try:
            count = await _run_backlog_sweep("Startup backfill")
            if count == 0:
                print("ℹ️ Nothing missed — fully caught up.")
        except Exception:
            print(f"❌ Backfill step failed:\n{traceback.format_exc()}")

    # Keep re-checking for missed posts for as long as the container runs —
    # not just once at startup.
    asyncio.create_task(periodic_safety_sweep_loop())
    print(f"🛡️ Periodic safety-net sweep running every {PERIODIC_SWEEP_SECONDS}s alongside live monitoring.")

    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print(f"❌ FATAL STARTUP ERROR:\n{traceback.format_exc()}")