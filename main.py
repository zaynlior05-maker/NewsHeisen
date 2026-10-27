import os
import re
import io
import json
import asyncio
import traceback
import http.client
import urllib.request
import urllib.error
from pyrogram import Client, filters, enums, idle
from pyrogram.errors import FloodWait

# 1. CREDENTIALS & CHAT IDs
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
if not SESSION_STRING:
    raise ValueError("❌ CRITICAL ERROR: SESSION_STRING environment variable is missing or empty in Railway!")

MY_BOT_TOKEN = os.environ["MY_BOT_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
SOURCE_GROUP_ID = int(os.environ["SOURCE_GROUP_ID"])
DESTINATION_CHAT_ID = int(os.environ["DESTINATION_CHAT_ID"])
TARGET_BOT_ID = int(os.environ["TARGET_BOT_ID"])
MY_CHANNEL = os.environ.get("MY_CHANNEL", "")
MY_ADMIN = os.environ.get("MY_ADMIN", "")

# 2. GLOBALS
EXCLUDED_PHRASES = ["( PAID AD )", "The giveaway has officially ended.", "Giveaway Entries"]
SCRIPT_VERSION = "relay-v18-pure-memory-userapp-2026-10-27"

SESSION_DIR = os.environ.get("SESSION_DIR", "/app/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

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
    async with STATE_LOCK:
        if chat_id == SOURCE_GROUP_ID and msg_id > STATE.get("last_group_id", 0):
            STATE["last_group_id"] = msg_id
        elif chat_id == SOURCE_CHANNEL_ID and msg_id > STATE.get("last_channel_id", 0):
            STATE["last_channel_id"] = msg_id
        STATE["processed_albums"] = list(PROCESSED_SOURCE_ALBUMS)[-1000:]
        snapshot = dict(STATE)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _save_state_sync, snapshot)

BACKFILL_SCAN_LIMIT = 300
PERIODIC_SWEEP_SECONDS = 180

# 3. CLIENTS
# FIXED: user_app uses pure string session in memory without any workdir/disk attachment
user_app = Client(
    "user_account",
    session_string=SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH,
    in_memory=True
)

bot_app = Client(
    "my_bot",
    bot_token=MY_BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
    workdir=SESSION_DIR,
)

# 3b. RAW BOT API (HTTPS)
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{MY_BOT_TOKEN}"

def _bot_api_call_sync(method: str, payload: dict):
    url = f"{TELEGRAM_API_BASE}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
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
        "chat_id": int(chat_id), "text": text, "parse_mode": "HTML"
    })

# 3c. MULTIPART UPLOAD via http.client
def _multipart_body(fields: dict, files: list) -> tuple:
    boundary = "Boundary" + os.urandom(12).hex()
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for field_name, filename, data, ctype in files:
        header = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n'
        ).encode()
        parts.append(header + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'

def _bot_api_multipart_sync(method: str, fields: dict, files: list):
    body, content_type = _multipart_body(fields, files)
    conn = http.client.HTTPSConnection("api.telegram.org", timeout=120)
    path = f"/bot{MY_BOT_TOKEN}/{method}"
    conn.request("POST", path, body=body, headers={"Content-Type": content_type})
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        result = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse Telegram API JSON response: {raw}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Bot API multipart error in {method}: {result}")
    return result.get("result")

async def bot_api_multipart(method: str, fields: dict, files: list):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _bot_api_multipart_sync, method, fields, files)

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

def _media_info(msg):
    if msg.photo:
        return ("sendPhoto", "photo", "photo.jpg", "image/jpeg")
    if msg.video:
        fn = (msg.video.file_name or "video.mp4")
        return ("sendVideo", "video", fn, msg.video.mime_type or "video/mp4")
    if msg.document:
        fn = (msg.document.file_name or "document")
        return ("sendDocument", "document", fn, msg.document.mime_type or "application/octet-stream")
    if msg.audio:
        fn = (msg.audio.file_name or "audio.mp3")
        return ("sendAudio", "audio", fn, msg.audio.mime_type or "audio/mpeg")
    if msg.voice:
        return ("sendVoice", "voice", "voice.ogg", "audio/ogg")
    if msg.animation:
        fn = (msg.animation.file_name or "animation.gif")
        return ("sendAnimation", "animation", fn, msg.animation.mime_type or "video/mp4")
    if msg.sticker:
        fn = "sticker.webp"
        return ("sendSticker", "sticker", fn, "image/webp")
    return None

def _album_media_type(msg):
    if msg.photo: return "photo"
    if msg.video: return "video"
    if msg.document: return "document"
    if msg.audio: return "audio"
    return None

async def _download_msg_bytes(msg) -> bytes | None:
    try:
        buf = await user_app.download_media(msg, in_memory=True)
        if buf is None:
            return None
        buf.seek(0)
        return buf.read()
    except Exception:
        print(f"❌ Failed to download media for msg #{msg.id}:\n{traceback.format_exc()}")
        return None

# 5. DIRECT MEDIA SEND
async def send_single_media_direct(msg, caption: str | None):
    info = _media_info(msg)
    if info is None:
        print(f"⚠️ msg #{msg.id} has no supported media type — skipping.")
        return

    method, field_name, filename, ctype = info
    data = await _download_msg_bytes(msg)
    if data is None:
        print(f"⚠️ Could not download media for msg #{msg.id} — skipping.")
        return

    fields = {"chat_id": int(DESTINATION_CHAT_ID)}
    if caption:
        fields["caption"] = caption
        fields["parse_mode"] = "HTML"

    await bot_api_multipart(method, fields, [(field_name, filename, data, ctype)])
    print(f"✅ SUCCESS: Media #{msg.id} sent directly to destination via upload!")

async def send_album_direct(messages: list, caption: str | None):
    messages.sort(key=lambda m: m.id)
    media_json_items = []
    upload_files = []
    has_set_caption = False

    for i, msg in enumerate(messages):
        mtype = _album_media_type(msg)
        if mtype is None:
            print(f"⚠️ Album item #{msg.id} has no groupable media type — skipping item.")
            continue

        data = await _download_msg_bytes(msg)
        if data is None:
            print(f"⚠️ Could not download album item #{msg.id} — skipping item.")
            continue

        attach_name = f"file{i}"
        info = _media_info(msg)
        filename = info[2] if info else f"file{i}"
        ctype = info[3] if info else "application/octet-stream"

        item: dict = {"type": mtype, "media": f"attach://{attach_name}"}
        if not has_set_caption and caption:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
            has_set_caption = True

        media_json_items.append(item)
        upload_files.append((attach_name, filename, data, ctype))

    if not media_json_items:
        print("⚠️ Album: no groupable media found, nothing sent.")
        return

    if len(media_json_items) == 1:
        method, field_name, filename, ctype = (
            f"send{media_json_items[0]['type'].capitalize()}",
            media_json_items[0]["type"],
            upload_files[0][1],
            upload_files[0][3],
        )
        fields = {"chat_id": int(DESTINATION_CHAT_ID)}
        if caption:
            fields["caption"] = caption
            fields["parse_mode"] = "HTML"
        await bot_api_multipart(method, fields, [(field_name, filename, upload_files[0][2], ctype)])
        print("✅ SUCCESS: Album single-item sent to destination via upload!")
        return

    fields = {
        "chat_id": int(DESTINATION_CHAT_ID),
        "media": json.dumps(media_json_items),
    }
    await bot_api_multipart("sendMediaGroup", fields, upload_files)
    print(f"✅ SUCCESS: Album ({len(media_json_items)} items) sent to destination via upload!")

# 6. USERBOT INTERCEPTOR
async def process_and_send_message(message):
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

                raw_caption = ""
                for g_msg in sorted(group_msgs, key=lambda m: m.id):
                    if g_msg.caption:
                        raw_caption = g_msg.caption.html
                        break
                caption = clean_and_brand_text(raw_caption) if raw_caption else None

                await send_album_direct(group_msgs, caption)
            except Exception:
                print(f"❌ Error sending album {message.media_group_id}:\n{traceback.format_exc()}")
            return

        raw_content = message.text or message.caption or ""
        if is_excluded(raw_content):
            print(f"🚫 Message #{message.id} excluded by phrase filter.")
            return

        if message.text:
            new_text = clean_and_brand_text(message.text.html)
            try:
                await bot_api_send_text(DESTINATION_CHAT_ID, new_text)
                print(f"✅ SUCCESS: Text message #{message.id} sent to destination via Bot API.")
            except Exception:
                print(f"❌ Error sending text #{message.id}:\n{traceback.format_exc()}")
        elif message.media:
            caption_source = message.caption.html if message.caption else ""
            new_caption = clean_and_brand_text(caption_source) if caption_source else clean_and_brand_text(" ")
            try:
                await send_single_media_direct(message, new_caption)
            except Exception:
                print(f"❌ Error sending media #{message.id}:\n{traceback.format_exc()}")

    finally:
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
    group_backlog = await _collect_group_backfill()
    channel_backlog = await _collect_channel_backfill()
    all_backlog = sorted(group_backlog + channel_backlog, key=lambda m: m.date)
    if all_backlog:
        print(f"🔁 {label}: found {len(all_backlog)} missed post(s) — sending now.")
    for msg in all_backlog:
        print(f"🚚 {label}: sending message from chat {msg.chat.id}, msg #{msg.id}...")
        await process_and_send_message(msg)
        await asyncio.sleep(1.0)
    return len(all_backlog)

async def periodic_safety_sweep_loop():
    while True:
        await asyncio.sleep(PERIODIC_SWEEP_SECONDS)
        try:
            await _run_backlog_sweep("Periodic sweep")
        except Exception:
            print(f"⚠️ Periodic sweep failed:\n{traceback.format_exc()}")

async def _start_with_flood_wait_retry(client, label):
    while True:
        try:
            await client.start()
            print(f"✅ {label} started successfully.")
            return
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 60))
            print(
                f"⏳ {label}: FloodWait — sleeping {wait_s + 10}s before retrying login "
                f"(container staying alive, NOT crashing/restarting)..."
            )
            await asyncio.sleep(wait_s + 10)

async def send_last_message_startup_test():
    print("🧪 STARTUP TEST: Fetching the most recent source post to confirm live routing...")
    target_msg = None
    try:
        async for msg in user_app.get_chat_history(SOURCE_CHANNEL_ID, limit=1):
            target_msg = msg
            break
    except Exception:
        try:
            async for msg in user_app.get_chat_history(SOURCE_GROUP_ID, limit=1):
                if is_target_bot(msg):
                    target_msg = msg
                    break
        except Exception:
            pass

    if target_msg:
        print(f"🎯 Found live test message #{target_msg.id} — sending confirmation post to destination now...")
        try:
            await process_and_send_message(target_msg)
            print("✨ Startup confirmation test completed successfully!")
        except Exception as e:
            print(f"❌ Startup test dispatch failed: {e}")
    else:
        print("⚠️ Startup test: No recent messages found to verify.")

# 7. STARTUP
async def main():
    print(f"🚀 SCRIPT VERSION: {SCRIPT_VERSION}")
    await _start_with_flood_wait_retry(user_app, "Userbot")
    await _start_with_flood_wait_retry(bot_app, "Bot")

    try:
        dialog_count = 0
        async for dialog in user_app.get_dialogs(limit=200):
            dialog_count += 1
        print(f"🔄 Synced {dialog_count} userbot dialogs.")
    except Exception:
        print(f"⚠️ Dialog sync failed:\n{traceback.format_exc()}")

    try:
        info = await bot_api_call("getChat", {"chat_id": int(DESTINATION_CHAT_ID)})
        print(f"✅ Bot API confirms access to destination chat: {info.get('title', info.get('id'))}")
    except Exception as e:
        print(f"⚠️ Bot API getChat check failed (bot may not be a member/admin there): {e}")

    await send_last_message_startup_test()

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

    asyncio.create_task(periodic_safety_sweep_loop())
    print(f"🛡️ Periodic safety-net sweep running every {PERIODIC_SWEEP_SECONDS}s alongside live monitoring.")

    await idle()
    await user_app.stop()
    await bot_app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print(f"💥 Fatal error:\n{traceback.format_exc()}")
