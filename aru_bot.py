#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================
 ARU BOT - Python version (single file) - full port of PHP "FIXED VERSION 30/31"
============================================================
Features (nothing dropped from the PHP version):
 - AI chat (Groq) with per-chat language + per-user memory (Firebase + local backup)
 - Music download (/song) with safe URL validation
 - Group moderation: ban / mute / unmute / pin / unpin, bad words, bad stickers,
   anti-service (promo) shield, bio-link protection, foreign-language filter
 - Welcome system: custom message, rotating videos, DM welcome, default premium card
 - Clone bot system: encrypted tokens, dynamic webhook routing (?clone_id=...)
 - Broadcast (users + groups, reply-friendly, retry queue)
 - Join request handler (auto / manual, admin-channel notifications)
 - Custom replies / triggers (/setreply, /delreply, /replies, /mset)
 - Settings panel per group, language selection
 - VC notifications + optional external recording worker hooks
 - User/group tracking, memberships, events, profile snapshots
 - NEW: /users NAME (fuzzy name search: "frank" also finds "PRANK"),
        /groups, /addedlog (log of groups the bot was added to)

Install:   pip install flask requests cryptography        (optional: waitress)
Run:       python aru_bot.py

Environment variables (set them in the hosting panel):
  BOT_TOKEN            (required)  main bot token
  GROQ_API_KEY         Groq key for AI chat
  GROQ_MODEL           default: openai/gpt-oss-20b
  APP_KEY              long random string (>=32 chars) used to encrypt clone tokens
                       (if empty a random key is generated and stored in bot_data/.app_key)
  WEBHOOK_URL          public HTTPS URL of this app, e.g. https://mybot.example.com/webhook
                       -> webhook mode (needed for clone bots). If empty -> long polling.
  PORT                 web port (default 8080)
  FIREBASE_URL         default: https://toxice-aaruchatbot-default-rtdb.firebaseio.com
  FIREBASE_AUTH        optional database secret / token
  ADMIN_CHANNEL_ID     default -1004474853689
  ADMIN_IDS            comma separated, default 7708956278,8618892732
  DEFAULT_WELCOME_VIDEO, STICKER_RESPONSE_FILE_ID, MUSIC_API
  RECORDING_HOOK_URL, RECORDING_RESULT_SECRET, VC_EVENT_SECRET
  CLONE_ADMIN_ONLY=1   only global admins may create clones (default: everyone)
  CLONE_LIMIT          max clones per normal user (default 5)
  WORKERS              thread pool size (default 32)
  DISABLE_WEB=1        polling mode without web server
  BOT_DATA_DIR         folder for local backup data (default ./bot_data)
============================================================
"""

import os
import sys
import re
import json
import time
import html
import hmac
import hashlib
import base64
import random
import threading
import logging
import atexit
import ipaddress
import socket
import tempfile
import queue
import traceback
import unicodedata
import secrets
import difflib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin, quote

import requests
from requests.adapters import HTTPAdapter

try:
    from flask import Flask, request, jsonify
except ImportError:  # pragma: no cover
    print("Flask missing. Run: pip install flask requests cryptography")
    raise

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sympadding

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("aru_bot")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ============================================================
# CONFIGURATION
# ============================================================


def env(name, default=""):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip()


BOT_TOKEN = env("BOT_TOKEN")
GROQ_API_KEY = env("GROQ_API_KEY")
if GROQ_API_KEY.upper() == "PASTE_GROQ_API_KEY_HERE":
    GROQ_API_KEY = ""
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MUSIC_API = env("MUSIC_API", "https://music-api-wheat-mu.vercel.app/search")
FIREBASE_URL = env("FIREBASE_URL", "https://toxice-aaruchatbot-default-rtdb.firebaseio.com").rstrip("/")
FIREBASE_AUTH = env("FIREBASE_AUTH")
if FIREBASE_URL and not re.match(r"^https?://", FIREBASE_URL):
    log.error("FIREBASE_URL is invalid - Firebase disabled, local backup only.")
    FIREBASE_URL = ""

try:
    ADMIN_CHANNEL_ID = int(env("ADMIN_CHANNEL_ID", "-1004474853689"))
except ValueError:
    ADMIN_CHANNEL_ID = 0

ADMIN_IDS = []
for _x in env("ADMIN_IDS", "7708956278,8618892732").split(","):
    _x = _x.strip()
    if _x.lstrip("-").isdigit():
        ADMIN_IDS.append(int(_x))

RECORDING_HOOK_URL = env("RECORDING_HOOK_URL")
RECORDING_RESULT_SECRET = env("RECORDING_RESULT_SECRET")
VC_EVENT_SECRET = env("VC_EVENT_SECRET")
WEBHOOK_URL = env("WEBHOOK_URL").rstrip("/")
PORT = int(env("PORT", "8080") or 8080)
CLONE_ADMIN_ONLY = env("CLONE_ADMIN_ONLY", "0") in ("1", "true", "yes")
CLONE_LIMIT = int(env("CLONE_LIMIT", "5") or 5)
WORKERS = int(env("WORKERS", "32") or 32)
DISABLE_WEB = env("DISABLE_WEB", "0") in ("1", "true", "yes")

DATA_DIR = env("BOT_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data"))
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _t = os.path.join(DATA_DIR, ".w")
    with open(_t, "w") as _f:
        _f.write("1")
    os.remove(_t)
except Exception:
    DATA_DIR = os.path.join(tempfile.gettempdir(), "telegram_bot_data")
    os.makedirs(DATA_DIR, exist_ok=True)

APP_KEY = env("APP_KEY")
if not APP_KEY:
    _kf = os.path.join(DATA_DIR, ".app_key")
    try:
        if os.path.isfile(_kf):
            APP_KEY = open(_kf, "r").read().strip()
        if not APP_KEY:
            APP_KEY = secrets.token_hex(32)
            with open(_kf, "w") as _f:
                _f.write(APP_KEY)
            log.warning("APP_KEY not set: generated one in %s. Set APP_KEY env on hosts with temporary disk!", _kf)
    except Exception:
        APP_KEY = hashlib.sha256((os.path.abspath(__file__) + "|local-clone-key").encode()).hexdigest()

CLONE_WEBHOOK_SECRET_PREFIX = "tgclone_v1"

# ---------------- bot constants ----------------
BOT_NAME = "۝🎀ꔷᎪ𝐍ᎶᎬᏞ❤"
OWNER_NAME = "۝🎀ꔷᎪ𝐍ᎶᎬᏞ❤ 💓"
OWNER_USERNAME = "@OFFLINE_HACKER_FRANK"
OWNER_LINK = "https://t.me/OFFLINE_HACKER_FRANK"
SUPPORT_LINK = "https://t.me/HACKER_FRANK"
DEFAULT_WELCOME_VIDEO = env("DEFAULT_WELCOME_VIDEO", "https://yourimageshare.com/ib/AuODJWI2We.mp4")
WELCOME_DM_ON = True
FOREIGN_LANGUAGE_FILTER_ON = True
FOREIGN_LANGUAGE_DELETE_AFTER = 180
JOINREQ_DEFAULT_AUTO = False
JOIN_DM_ON = True  # DM only after successful approval
AUTO_PROFILE_SNAPSHOT = True
DB_SCHEMA_VERSION = 7
STICKER_REPLY_ONLY_WHEN_ADDRESSED = True
AI_REACTION_ON = True
AI_REACTION_EMOJI = "❤️"
JOIN_REQUEST_ADMIN_NOTIFY_ON = True
JOIN_REQUEST_PENDING_DM_ON = False
MAX_AUDIO_BYTES = 48 * 1024 * 1024
BIO_CACHE_TTL = 300
ADMIN_CACHE_TTL = 30
SPECIAL_SENDER_IDS = {1087, 777000, 136817688}  # anonymous admin bot, channel forwards, channel bot

STICKER_RESPONSES = [s for s in [env("STICKER_RESPONSE_FILE_ID",
                                     "CAACAgUAAxkBAAEXVGirQtJy2VHA4E6mHdESDZEr9F7LUMWACjRoAajn_qFnf0P-jOwrDCz0E")] if s]

EMOJI_RESPONSES_LOVE = ["🥰😍😘💖💕💞💓💗💘💝🫶", "💋💌🫀❤️‍🔥👩‍❤️‍👨💏💍💐🌹"]
EMOJI_RESPONSES_FUN = ["🙈🙉🙊😜🤪😛😝😜😝😜", "🤡👻🤖👾👽🎃😺😸😹😻"]
EMOJI_RESPONSES_PARTY = ["🔥💥🎉🎊🥳🍾🥂🍻🍹🍸💃🕺", "✨🌟💫⚡️⭐🌠🎇🎆🎈🎆"]
EMOJI_RESPONSES_SAD = ["🤗💖💪✨ Arey ro mat! Sab theek ho jayega! 🚀🥰", "😘🫂 Chal haso ab! Main hu na tumhare sath! 🌸💯"]
EMOJI_RESPONSES_COOL = ["😎🕶️🔥💯⚡️🚀👑🏆💪💪💪", "🎯🔮💎🧿🔱🧬⚔️🛡️🔥🚀"]
EMOJI_RESPONSES_GENERAL = ["🥰😘🙈😜💖🌸✨🔥👀🫡💔🥺🥳🚀💯🎉", "✨🌟💫🔥💯😍🥰😘🙏🙌👏👍"]

# ============================================================
# SMALL HELPERS
# ============================================================


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_ts():
    return int(time.time())


def h(value):
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]*>", "", s or ""))


def user_mention(user_id, name):
    safe = h(name if name else "Friend")
    return '<a href="tg://user?id=%d">%s</a>' % (user_id, safe) if user_id > 0 else safe


def firebase_key(value):
    return "k_" + base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def firebase_key_decode(key):
    raw = key[2:] if key.startswith("k_") else key
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def custom_reply_path(chat_id, trigger):
    return "/custom_replies/%d/%s" % (chat_id, firebase_key(trigger.strip().lower()))


def split_cmd(full_text):
    parts = full_text.split(None, 1)
    raw = parts[0] if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    return raw, args


def chunk_text(text, limit=4000):
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                out.append(cur)
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        out.append(cur)
    return out


# ============================================================
# LOCAL STORAGE (in-memory + background flush, atomic writes)
# ============================================================


class LocalDB:
    def __init__(self, directory):
        self.dir = directory
        self.lock = threading.RLock()
        self.data = {}
        self.dirty = set()
        threading.Thread(target=self._flusher, daemon=True).start()
        atexit.register(self.flush)

    def _file(self, name):
        return os.path.join(self.dir, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json")

    def _load(self, name):
        if name in self.data:
            return
        val = {}
        f = self._file(name)
        try:
            if os.path.isfile(f):
                with open(f, "r", encoding="utf-8") as fh:
                    raw = fh.read()
                if raw.strip():
                    val = json.loads(raw)
        except Exception as e:
            log.warning("Local read failed for %s: %s", name, e)
        if not isinstance(val, dict):
            val = {}
        self.data[name] = val

    def read(self, name):
        with self.lock:
            self._load(name)
            return dict(self.data[name])

    def get(self, name, key, default=None):
        with self.lock:
            self._load(name)
            return self.data[name].get(key, default)

    def set(self, name, key, value):
        with self.lock:
            self._load(name)
            self.data[name][key] = value
            self.dirty.add(name)

    def patch(self, name, key, patch):
        with self.lock:
            self._load(name)
            old = self.data[name].get(key)
            if not isinstance(old, dict):
                old = {}
            merged = dict(old)
            merged.update(patch)
            self.data[name][key] = merged
            self.dirty.add(name)

    def delete(self, name, key):
        with self.lock:
            self._load(name)
            if key in self.data[name]:
                del self.data[name][key]
                self.dirty.add(name)

    def clear(self, name):
        with self.lock:
            self.data[name] = {}
            self.dirty.add(name)

    def replace(self, name, value):
        with self.lock:
            self.data[name] = value
            self.dirty.add(name)

    def flush(self):
        with self.lock:
            names = list(self.dirty)
            self.dirty.clear()
            snapshots = {n: json.dumps(self.data.get(n, {}), ensure_ascii=False) for n in names}
        for n, js in snapshots.items():
            f = self._file(n)
            tmp = f + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(js)
                os.replace(tmp, f)
            except Exception as e:
                log.error("Local write failed for %s: %s", n, e)
                with self.lock:
                    self.dirty.add(n)

    def _flusher(self):
        while True:
            time.sleep(3)
            try:
                self.flush()
            except Exception:
                pass


LOCAL = LocalDB(DATA_DIR)

# ============================================================
# FIREBASE REALTIME DB (REST) - async writes, sync reads
# ============================================================


class Firebase:
    def __init__(self, base):
        self.base = base
        self.s = requests.Session()
        ad = HTTPAdapter(pool_connections=10, pool_maxsize=40)
        self.s.mount("https://", ad)
        self.s.mount("http://", ad)
        self.q = queue.Queue(maxsize=20000)
        if base:
            for _ in range(3):
                threading.Thread(target=self._worker, daemon=True).start()

    def _url(self, path):
        p = path.strip("/")
        return "%s/%s.json" % (self.base, p) if p else "%s/.json" % self.base

    def req(self, path, data=None, method="GET"):
        """returns (ok, data)"""
        if not self.base:
            return False, None
        method = method.upper()
        kw = {"timeout": (5, 15)}
        if FIREBASE_AUTH:
            kw["params"] = {"auth": FIREBASE_AUTH}
        if data is not None and method in ("POST", "PUT", "PATCH"):
            kw["data"] = json.dumps(data, ensure_ascii=False).encode("utf-8")
            kw["headers"] = {"Content-Type": "application/json"}
        try:
            r = self.s.request(method, self._url(path), **kw)
        except requests.RequestException as e:
            log.warning("Firebase network error: %s", e)
            return False, None
        if not 200 <= r.status_code < 300:
            log.warning("Firebase HTTP %s on %s %s: %s", r.status_code, method, path, r.text[:200])
            return False, None
        if r.text in ("", "null"):
            return True, None
        try:
            return True, r.json()
        except ValueError:
            return False, None

    def _worker(self):
        while True:
            path, data, method = self.q.get()
            for attempt in range(3):
                ok, _ = self.req(path, data, method)
                if ok:
                    break
                time.sleep(1 + attempt)

    def write(self, path, data, method="PUT"):
        """async (fire & forget)"""
        if not self.base:
            return
        try:
            self.q.put_nowait((path, data, method))
        except queue.Full:
            log.warning("Firebase queue full, dropping write %s", path)

    def write_sync(self, path, data, method="PUT"):
        ok, _ = self.req(path, data, method)
        return ok

    def read_value(self, path):
        ok, d = self.req(path)
        return d if ok else None

    def read_dict(self, path):
        ok, d = self.req(path)
        if isinstance(d, dict):
            return d
        if isinstance(d, list):
            return {str(i): v for i, v in enumerate(d) if v is not None}
        return {}


FB = Firebase(FIREBASE_URL)

# ============================================================
# HTTP SESSION / TELEGRAM API
# ============================================================

SESSION = requests.Session()
_ad = HTTPAdapter(pool_connections=20, pool_maxsize=100)
SESSION.mount("https://", _ad)
SESSION.mount("http://", _ad)

_tl = threading.local()


class Ctx:
    """Per-update context (main bot or a clone bot)."""

    def __init__(self, token, clone_id="", clone_record=None):
        self.token = token
        self.clone_id = clone_id
        self.clone_record = clone_record or {}
        info = get_bot_info(token)
        self.bot_id = info["id"]
        self.bot_username = info["username"]
        admins = list(ADMIN_IDS)
        oid = self.clone_record.get("owner_id")
        if oid is not None and str(oid).lstrip("-").isdigit():
            admins.append(int(oid))
        self.admin_ids = set(admins)


def C():
    return _tl.ctx


def set_ctx(ctx):
    _tl.ctx = ctx


def tg(method, payload=None, token=None, files=None, timeout=20):
    token = token or C().token
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    try:
        if files:
            data = {}
            for k, v in (payload or {}).items():
                data[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            r = SESSION.post(url, data=data, files=files, timeout=(8, 60))
        else:
            r = SESSION.post(url, json=payload or {}, timeout=(5, timeout))
    except requests.RequestException as e:
        log.warning("Telegram network error (%s): %s", method, type(e).__name__)
        return {"ok": False, "error_code": 0, "description": type(e).__name__ + ": " + str(e)[:150]}
    try:
        j = r.json()
    except ValueError:
        return {"ok": False, "error_code": r.status_code, "description": "Invalid Telegram API response"}
    if not isinstance(j, dict):
        return {"ok": False, "error_code": r.status_code, "description": "Invalid Telegram API response"}
    if not j.get("ok"):
        j.setdefault("error_code", r.status_code)
        desc = str(j.get("description", ""))
        if "message is not modified" not in desc:
            log.info("Telegram %s failed: %s %s", method, j.get("error_code"), desc[:160])
    return j


_bot_info_cache = {}


def get_bot_info(token):
    c = _bot_info_cache.get(token)
    if c and c["id"] and time.time() - c["at"] < 86400:
        return c
    r = tg("getMe", {}, token=token)
    info = {"id": 0, "username": "", "at": time.time()}
    if r.get("ok") and isinstance(r.get("result"), dict):
        info["id"] = int(r["result"].get("id", 0))
        info["username"] = r["result"].get("username", "") or ""
        _bot_info_cache[token] = info
    else:
        log.error("getMe failed: %s", r.get("description"))
    return info


def _is_parse_error(res):
    return "can't parse entities" in str(res.get("description", "")).lower() or "can't find end" in str(res.get("description", "")).lower()


def send_msg(chat_id, text, keyboard=None, reply_to=None, token=None):
    chunks = chunk_text(text, 4000)
    res = {"ok": False, "error_code": 0, "description": "empty"}
    for i, ch in enumerate(chunks):
        data = {"chat_id": chat_id, "text": ch, "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard is not None and i == len(chunks) - 1:
            data["reply_markup"] = keyboard
        if reply_to is not None and i == 0:
            data["reply_to_message_id"] = reply_to
            data["allow_sending_without_reply"] = True
        res = tg("sendMessage", data, token=token)
        if not res.get("ok") and _is_parse_error(res):
            data.pop("parse_mode", None)
            data["text"] = strip_tags(ch)
            res = tg("sendMessage", data, token=token)
    return res


def send_msg_reply(chat_id, text, reply_to=None, keyboard=None):
    return send_msg(chat_id, text, keyboard, reply_to)


def send_typing(chat_id):
    return tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def edit_message_text(chat_id, message_id, text, keyboard=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if keyboard is not None:
        data["reply_markup"] = keyboard
    res = tg("editMessageText", data)
    if not res.get("ok") and _is_parse_error(res):
        data.pop("parse_mode", None)
        data["text"] = strip_tags(text)
        res = tg("editMessageText", data)
    return res


def edit_cb_message(cb, text, keyboard=None):
    """Edit the callback's message; if it has no text (video/photo) send a new message instead."""
    m = cb.get("message") or {}
    chat = int((m.get("chat") or {}).get("id", 0))
    mid = int(m.get("message_id", 0))
    if not chat:
        return {"ok": False}
    if "text" in m and mid:
        r = edit_message_text(chat, mid, text, keyboard)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return r
    return send_msg(chat, text, keyboard)


def answer_cb(cb_id, text, show_alert=False):
    return tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:200], "show_alert": show_alert})


def delete_message(chat_id, message_id, token=None):
    return tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, token=token)


def send_sticker(chat_id, sticker, reply_to=None):
    data = {"chat_id": chat_id, "sticker": sticker}
    if reply_to is not None:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    return tg("sendSticker", data)


def set_reaction(chat_id, message_id, emoji="❤️"):
    if not chat_id or not message_id or not emoji:
        return {"ok": False, "error_code": 0, "description": "Invalid reaction target."}
    return tg("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
                                     "reaction": [{"type": "emoji", "emoji": emoji}], "is_big": False})


def restrict_member(chat_id, user_id, until=0):
    d = {"chat_id": chat_id, "user_id": user_id, "permissions": {"can_send_messages": False}}
    if until:
        d["until_date"] = until
    return tg("restrictChatMember", d)


def unrestrict_member(chat_id, user_id):
    return tg("restrictChatMember", {"chat_id": chat_id, "user_id": user_id, "permissions": {
        "can_send_messages": True, "can_send_audios": True, "can_send_documents": True, "can_send_photos": True,
        "can_send_videos": True, "can_send_video_notes": True, "can_send_voice_notes": True, "can_send_polls": True,
        "can_send_other_messages": True, "can_add_web_page_previews": True, "can_invite_users": True}})


def ban_member(chat_id, user_id):
    return tg("banChatMember", {"chat_id": chat_id, "user_id": user_id})


def unban_member(chat_id, user_id):
    return tg("unbanChatMember", {"chat_id": chat_id, "user_id": user_id, "only_if_banned": True})


def approve_join(chat_id, user_id):
    return tg("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})


def decline_join(chat_id, user_id):
    return tg("declineChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})


def send_safe_dm(user_id, text, keyboard=None):
    res = send_msg(user_id, text, keyboard)
    if not res.get("ok"):
        log.info("DM failed for user %s: %s", user_id, res.get("description"))
    return res


def send_msg_safe_broadcast(chat_id, text, keyboard=None, token=None):
    if chat_id == 0:
        return {"ok": False, "error_code": 0, "description": "Destination chat is not configured."}
    attempts = 0
    r = {}
    while attempts < 3:
        r = send_msg(chat_id, text, keyboard, token=token)
        if r.get("ok"):
            return r
        code = int(r.get("error_code", 0) or 0)
        if code == 429:
            time.sleep(min(int((r.get("parameters") or {}).get("retry_after", 3)), 15))
            attempts += 1
            continue
        if code >= 500 or code == 0:
            time.sleep(0.5 * (attempts + 1))
            attempts += 1
            continue
        return r
    return r


def send_admin_notification(text, keyboard=None):
    cx = C()
    if ADMIN_CHANNEL_ID != 0:
        r = send_msg_safe_broadcast(ADMIN_CHANNEL_ID, text, keyboard)
        if not r.get("ok") and cx.clone_id:
            r = send_msg_safe_broadcast(ADMIN_CHANNEL_ID, text, keyboard, token=BOT_TOKEN)
        return r
    fallback = next(iter(sorted(cx.admin_ids)), 0)
    if fallback > 0:
        return send_msg_safe_broadcast(fallback, text, keyboard)
    return {"ok": False, "error_code": 0, "description": "No admin notification destination configured."}


def copy_message(target, from_chat, message_id):
    return tg("copyMessage", {"chat_id": target, "from_chat_id": from_chat, "message_id": message_id})


def is_inactive_chat_error(resp):
    code = int(resp.get("error_code", 0) or 0)
    desc = str(resp.get("description", "")).lower()
    if code == 403:
        return any(p in desc for p in ["bot was blocked by the user", "user is deactivated", "bot was kicked from the group",
                                       "chat write forbidden", "forbidden: bot was kicked", "bot was kicked", "bot is not a member"])
    if code != 400:
        return False
    return any(p in desc for p in ["chat not found", "user is deactivated", "bot was blocked by the user", "user not found",
                                   "kicked from the group", "not a member of the chat"])


def is_bot_addressed(message, bot_username, bot_id):
    text = str(message.get("text") or message.get("caption") or "")
    if bot_username and re.search(r"@" + re.escape(bot_username) + r"\b", text, re.I):
        return True
    rf = (message.get("reply_to_message") or {}).get("from") or {}
    if rf.get("id") and bot_id > 0:
        return int(rf["id"]) == bot_id
    return False


# ============================================================
# URL SAFETY / DOWNLOAD
# ============================================================


def is_safe_remote_url(url):
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme.lower() not in ("https", "http"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return True


def safe_download(url, max_bytes, suffix=".bin", total_timeout=60):
    """Download with SSRF checks on every redirect hop. Returns (path, error)."""
    fd, path = tempfile.mkstemp(prefix="tgdl_", suffix=suffix)
    os.close(fd)
    cur = url
    deadline = time.time() + total_timeout
    try:
        for _ in range(5):
            if not is_safe_remote_url(cur):
                raise ValueError("Unsafe URL rejected")
            r = SESSION.get(cur, stream=True, allow_redirects=False, timeout=(5, 20),
                            headers={"User-Agent": "Mozilla/5.0 TelegramBot/1.0"})
            if r.status_code in (301, 302, 303, 307, 308):
                cur = urljoin(cur, r.headers.get("Location", ""))
                r.close()
                continue
            if not 200 <= r.status_code < 300:
                r.close()
                raise ValueError("HTTP %d" % r.status_code)
            size = 0
            with open(path, "wb") as fh:
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("File too large")
                    if time.time() > deadline:
                        raise ValueError("Download timeout")
                    fh.write(chunk)
            r.close()
            if size == 0:
                raise ValueError("Empty file")
            return path, ""
        raise ValueError("Too many redirects")
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        return None, str(e)


def send_video(chat_id, video, caption="", keyboard=None):
    cap_ok = len(strip_tags(caption)) <= 1000
    data = {"chat_id": chat_id, "video": video, "parse_mode": "HTML"}
    if caption and cap_ok:
        data["caption"] = caption
        if keyboard is not None:
            data["reply_markup"] = keyboard
    elif keyboard is not None and not caption:
        data["reply_markup"] = keyboard
    res = tg("sendVideo", data)
    if not res.get("ok") and re.match(r"^https?://", str(video)):
        path, err = safe_download(video, 48 * 1024 * 1024, ".mp4", 45)
        if path:
            try:
                with open(path, "rb") as fh:
                    up = {"chat_id": chat_id, "parse_mode": "HTML"}
                    if caption and cap_ok:
                        up["caption"] = caption
                        if keyboard is not None:
                            up["reply_markup"] = keyboard
                    res = tg("sendVideo", up, files={"video": ("welcome.mp4", fh, "video/mp4")}, timeout=60)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
    if res.get("ok") and caption and not cap_ok:
        send_msg(chat_id, caption, keyboard)
    return res


# ============================================================
# DATABASE LAYER (Firebase + local)
# ============================================================

_user_sig = {}
_user_sig_lock = threading.Lock()


def get_user_record(user_id):
    rec = LOCAL.get("users", str(user_id))
    if isinstance(rec, dict) and rec:
        return rec
    d = FB.read_value("/users/%d" % user_id)
    return d if isinstance(d, dict) else {}


def save_user(user_id, name, username, extra=None, force=False):
    if user_id <= 0:
        return
    uname = html.unescape(username or "").strip().lstrip("@")
    if uname.upper() == "N/A":
        uname = ""
    payload = {"name": html.unescape(name or "").strip(), "username": uname}
    for k, v in (extra or {}).items():
        if k and re.fullmatch(r"[A-Za-z0-9_-]+", str(k)):
            payload[k] = v
    sig = json.dumps(payload, sort_keys=True, default=str)
    now = time.time()
    with _user_sig_lock:
        prev = _user_sig.get(user_id)
        if not force and prev and prev[0] == sig and now - prev[1] < 60:
            return
        _user_sig[user_id] = (sig, now)
        if len(_user_sig) > 50000:
            _user_sig.clear()
    payload["last_seen"] = now_str()
    payload["last_seen_ts"] = int(now)
    LOCAL.patch("users", str(user_id), payload)
    FB.write("/users/%d" % user_id, payload, "PATCH")


def save_group(chat_id, title, username="", extra=None):
    if chat_id == 0:
        return
    payload = {"title": html.unescape(title or "").strip(),
               "username": html.unescape(username or "").strip().lstrip("@"),
               "last_active": now_str(), "last_active_ts": now_ts()}
    for k, v in (extra or {}).items():
        if k and re.fullmatch(r"[A-Za-z0-9_-]+", str(k)):
            payload[k] = v
    LOCAL.patch("groups", str(chat_id), payload)
    FB.write("/groups/%d" % chat_id, payload, "PATCH")


def remember_event(etype, chat_id=0, user_id=0, data=None):
    key = "%d_%d" % (int(time.time() * 1000), random.randint(1000, 9999))
    payload = {"type": etype, "chat_id": chat_id, "user_id": user_id, "created_at": now_str(), "created_ts": now_ts()}
    for k, v in (data or {}).items():
        if k and re.fullmatch(r"[A-Za-z0-9_-]+", str(k)):
            payload[k] = v
    FB.write("/events/" + key, payload, "PUT")


def save_user_profile_snapshot(user_id):
    if user_id <= 0:
        return {}
    out = {}
    chat = tg("getChat", {"chat_id": user_id})
    if chat.get("ok") and isinstance(chat.get("result"), dict):
        r = chat["result"]
        if "bio" in r:
            out["bio"] = str(r["bio"])
        if "first_name" in r:
            out["name"] = str(r["first_name"])
        if "username" in r:
            out["username"] = str(r["username"])
    photos = tg("getUserProfilePhotos", {"user_id": user_id, "limit": 1})
    try:
        sizes = photos["result"]["photos"][0]
        best = sizes[-1]
        if best.get("file_id"):
            out["dp_file_id"] = str(best["file_id"])
            out["dp_file_unique_id"] = str(best.get("file_unique_id", ""))
    except Exception:
        pass
    if out:
        LOCAL.patch("users", str(user_id), out)
        FB.write("/users/%d" % user_id, out, "PATCH")
    return out


def save_group_snapshot(chat_id):
    if chat_id == 0:
        return {}
    chat = tg("getChat", {"chat_id": chat_id})
    if not chat.get("ok") or not isinstance(chat.get("result"), dict):
        return {}
    r = chat["result"]
    payload = {"title": str(r.get("title", "")), "username": str(r.get("username", "")), "type": str(r.get("type", "")),
               "last_active": now_str(), "last_active_ts": now_ts()}
    ph = r.get("photo") or {}
    if ph.get("big_file_id"):
        payload["dp_file_id"] = str(ph["big_file_id"])
        payload["dp_file_unique_id"] = str(ph.get("big_file_unique_id", ""))
    LOCAL.patch("groups", str(chat_id), payload)
    FB.write("/groups/%d" % chat_id, payload, "PATCH")
    return payload


def remember_user_profile(user_id, name, username, chat_id=0):
    if user_id <= 0:
        return
    existing = get_user_record(user_id)
    is_new = not existing
    save_user(user_id, name, username, {"profile_initialized": existing.get("profile_initialized") or now_str(),
                                         "last_chat_id": chat_id, "db_schema": DB_SCHEMA_VERSION}, force=True)
    if is_new:
        remember_event("new_user", chat_id, user_id, {"name": name, "username": (username or "").lstrip("@")})
    if AUTO_PROFILE_SNAPSHOT and (is_new or not existing.get("dp_file_id") or not existing.get("bio")):
        ctx = C()
        EXEC.submit(run_in_ctx, ctx, save_user_profile_snapshot, user_id)


_group_seen = {}


def remember_group_profile(chat_id, title, username=""):
    if chat_id == 0:
        return
    if time.time() - _group_seen.get(chat_id, 0) < 120:
        return
    _group_seen[chat_id] = time.time()
    existing = LOCAL.get("groups", str(chat_id))
    if not existing:
        d = FB.read_value("/groups/%d" % chat_id)
        existing = d if isinstance(d, dict) else {}
    save_group(chat_id, title, username, {"db_schema": DB_SCHEMA_VERSION})
    if not existing:
        remember_event("new_group", chat_id, 0, {"title": title, "username": (username or "").lstrip("@")})
        ctx = C()
        EXEC.submit(run_in_ctx, ctx, save_group_snapshot, chat_id)


def track_membership(chat_id, user_id, status, meta=None):
    if not chat_id or not user_id:
        return
    meta = meta or {}
    now = now_str()
    payload = {"status": status, "updated_at": now, "updated_ts": now_ts()}
    for k in ("joined_at", "left_at", "invite_link", "via_join_request", "actor_id", "updated_by"):
        if k in meta and meta[k] is not None:
            payload[k] = meta[k]
    LOCAL.patch("memberships", "%d_%d" % (chat_id, user_id), dict(payload, chat_id=chat_id, user_id=user_id))
    FB.write("/memberships/%d/%d" % (chat_id, user_id), payload, "PATCH")
    if status in ("member", "administrator", "creator"):
        md = {"chat_id": chat_id, "user_id": user_id, "status": status, "joined_at": meta.get("joined_at") or now,
              "title": meta.get("title") or "", "username": meta.get("username") or "", "updated_at": now}
    elif status in ("left", "kicked"):
        md = {"chat_id": chat_id, "user_id": user_id, "status": status, "left_at": meta.get("left_at") or now, "updated_at": now}
    else:
        return
    LOCAL.patch("user_groups", "%d_%d" % (user_id, chat_id), md)
    FB.write("/user_groups/%d/%d" % (user_id, chat_id), md, "PATCH")


def merge_records(fb_data, local_data):
    """Local copy is fresher (Firebase writes are async), Firebase fills the gaps."""
    out = dict(fb_data)
    for k, v in local_data.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            m = dict(out[k])
            m.update(v)
            out[k] = m
        else:
            out[k] = v
    return out


def get_user_groups(user_id):
    r = FB.read_dict("/user_groups/%d" % user_id)
    local = {}
    for key, row in LOCAL.read("user_groups").items():
        if not isinstance(row, dict):
            continue
        if "user_id" in row and int(row["user_id"]) != user_id:
            continue
        if "user_id" not in row and not str(key).startswith("%d_" % user_id):
            continue
        gid = str(row.get("chat_id", ""))
        if gid:
            local[gid] = row
    return merge_records(r, local)


def get_all_users():
    return merge_records(FB.read_dict("/users"), LOCAL.read("users"))


def get_all_groups():
    return merge_records(FB.read_dict("/groups"), LOCAL.read("groups"))


def delete_user_from_db(user_id):
    LOCAL.delete("users", str(user_id))
    FB.write("/users/%s" % user_id, None, "DELETE")


def delete_group_from_db(chat_id):
    LOCAL.delete("groups", str(chat_id))
    FB.write("/groups/%s" % chat_id, None, "DELETE")


def reset_firebase_db():
    for c in ("users", "groups", "broadcast_queue", "settings", "user_groups", "memberships", "clone_index", "clones",
              "memory", "custom_replies", "group_add_log", "scheduled_deletions"):
        LOCAL.clear(c)
    SETTINGS_CACHE.clear()
    for p in ("/users", "/groups", "/clones", "/clone_index", "/settings", "/memory", "/memory_chats", "/custom_replies",
              "/user_groups", "/memberships", "/events", "/group_add_log"):
        FB.write_sync(p, None, "DELETE")


# ---------------- group settings ----------------
SETTINGS_CACHE = {}  # chat_id -> (ts, dict)
SETTINGS_TTL = 30


def get_group_settings(chat_id):
    if chat_id == 0:
        return {}
    c = SETTINGS_CACHE.get(chat_id)
    if c and time.time() - c[0] < SETTINGS_TTL:
        return c[1]
    fb = FB.read_dict("/settings/%d" % chat_id)
    local = LOCAL.get("settings", str(chat_id)) or {}
    merged = dict(fb)
    if isinstance(local, dict):
        merged.update(local)
    SETTINGS_CACHE[chat_id] = (time.time(), merged)
    return merged


def set_group_setting(chat_id, key, value):
    if chat_id == 0 or not key:
        return False
    LOCAL.patch("settings", str(chat_id), {key: value})
    FB.write("/settings/%d/%s" % (chat_id, key), value, "PUT")
    s = dict(get_group_settings(chat_id))
    s[key] = value
    SETTINGS_CACHE[chat_id] = (time.time(), s)
    return True


def flag(settings, key, default=True):
    return bool(settings[key]) if key in settings else default


# ---------------- custom replies ----------------
_cr_cache = {}  # chat_id -> (ts, {trigger: reply})


def load_custom_replies(chat_id):
    c = _cr_cache.get(chat_id)
    if c and time.time() - c[0] < 30:
        return c[1]
    out = {}
    for key, val in FB.read_dict("/custom_replies/%d" % chat_id).items():
        trig = firebase_key_decode(key)
        if isinstance(val, dict):
            trig = val.get("trigger", trig)
            val = val.get("reply", "")
        if trig and isinstance(val, str) and val:
            out[trig.lower()] = val
    for trig, val in (LOCAL.get("custom_replies", str(chat_id)) or {}).items():
        if isinstance(val, str) and val:
            out[trig.lower()] = val
    _cr_cache[chat_id] = (time.time(), out)
    return out


def set_custom_reply(chat_id, trigger, reply):
    trigger = trigger.strip().lower()
    LOCAL.patch("custom_replies", str(chat_id), {trigger: reply})
    FB.write(custom_reply_path(chat_id, trigger), {"trigger": trigger, "reply": reply}, "PUT")
    _cr_cache.pop(chat_id, None)


def del_custom_reply(chat_id, trigger):
    trigger = trigger.strip().lower()
    rec = dict(LOCAL.get("custom_replies", str(chat_id)) or {})
    rec.pop(trigger, None)
    LOCAL.set("custom_replies", str(chat_id), rec)
    FB.write(custom_reply_path(chat_id, trigger), None, "DELETE")
    _cr_cache.pop(chat_id, None)


def notify_once(key, ttl=86400):
    safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", key)
    with LOCAL.lock:
        data = LOCAL.data.get("notify_once")
        if data is None:
            LOCAL._load("notify_once")
            data = LOCAL.data["notify_once"]
        now = now_ts()
        for k in [k for k, ts in data.items() if not isinstance(ts, (int, float)) or now - int(ts) > ttl]:
            del data[k]
        if safe in data:
            return False
        data[safe] = now
        LOCAL.dirty.add("notify_once")
        return True


# ============================================================
# ADMIN / PERMISSION CHECKS (cached)
# ============================================================
_admin_cache = {}
_bio_cache = {}


def is_actual_group_admin(chat_id, user_id):
    if chat_id == 0 or user_id <= 0:
        return False
    if user_id in SPECIAL_SENDER_IDS:
        return True
    key = (C().token[:12], chat_id, user_id)
    c = _admin_cache.get(key)
    if c and time.time() - c[1] < ADMIN_CACHE_TTL:
        return c[0]
    data = tg("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    status = (data.get("result") or {}).get("status", "")
    admin = status in ("creator", "administrator")
    _admin_cache[key] = (admin, time.time())
    if len(_admin_cache) > 20000:
        _admin_cache.clear()
    return admin


def is_chat_admin(chat_id, user_id):
    if chat_id == 0 or user_id <= 0:
        return False
    if user_id in C().admin_ids:
        return True
    if chat_id > 0:  # private chat: the user is the "admin" of their own chat
        return chat_id == user_id
    return is_actual_group_admin(chat_id, user_id)


def check_user_bio_for_links(user_id):
    if user_id <= 0:
        return False
    c = _bio_cache.get(user_id)
    if c and time.time() - c[1] < BIO_CACHE_TTL:
        return c[0]
    data = tg("getChat", {"chat_id": user_id})
    has_link = False
    if data.get("ok") and "bio" in (data.get("result") or {}):
        bio = str(data["result"]["bio"])
        has_link = re.search(r"(https?://|t\.me/|telegram\.me/|www\.|[a-zA-Z0-9-]+\.(com|org|net|in|co|me|io|xyz))(?:\b|$)", bio, re.I) is not None
    _bio_cache[user_id] = (has_link, time.time())
    if len(_bio_cache) > 50000:
        _bio_cache.clear()
    return has_link


# ============================================================
# AI / MEMORY
# ============================================================


def _mem_key(user_id, chat_id):
    return "%d_%d" % (chat_id, user_id) if chat_id != 0 else str(user_id)


def _mem_path(user_id, chat_id):
    return "/memory_chats/%d/%d" % (chat_id, user_id) if chat_id != 0 else "/memory/%d" % user_id


def _memory_rows(user_id, chat_id):
    rows = LOCAL.get("memory", _mem_key(user_id, chat_id))
    if isinstance(rows, dict) and rows:
        return rows
    return FB.read_dict(_mem_path(user_id, chat_id))


def load_memory(user_id, chat_id=0):
    if user_id <= 0:
        return []
    data = _memory_rows(user_id, chat_id)
    if not data:
        return []
    entries = [data[k] for k in sorted(data.keys())][-12:]
    messages = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("user"):
            messages.append({"role": "user", "content": str(e["user"])})
        if e.get("bot"):
            messages.append({"role": "assistant", "content": str(e["bot"])})
    return messages[-24:]


def save_memory(user_id, user_msg, bot_reply, chat_id=0):
    if user_id <= 0 or not user_msg.strip() or not bot_reply.strip():
        return
    ts_key = "%d_%d" % (int(time.time() * 1000), random.randint(1000, 9999))
    row = {"user": user_msg, "bot": bot_reply, "timestamp": now_str(), "timestamp_ts": now_ts()}
    lk = _mem_key(user_id, chat_id)
    with LOCAL.lock:
        rows = dict(LOCAL.get("memory", lk) or {})
        rows[ts_key] = row
        old_keys = []
        if len(rows) > 30:
            keys = sorted(rows.keys())
            old_keys = keys[:-30]
            for k in old_keys:
                del rows[k]
        LOCAL.set("memory", lk, rows)
    base = _mem_path(user_id, chat_id)
    FB.write("%s/%s" % (base, ts_key), row, "PUT")
    for k in old_keys:
        FB.write("%s/%s" % (base, k), None, "DELETE")


def clear_memory(user_id, chat_id=0):
    LOCAL.delete("memory", _mem_key(user_id, chat_id))
    FB.write(_mem_path(user_id, chat_id), None, "DELETE")


LANGUAGE_INSTRUCTIONS = {
    "hinglish": "Reply in casual Hinglish (Hindi + English mix).",
    "bhojpuri": "Reply primarily in natural Bhojpuri, using simple Hindi/English when useful.",
    "english": "Reply in clear, friendly English.",
    "bengali": "Reply primarily in natural Bengali.",
    "urdu": "Reply primarily in natural Urdu.",
    "chinese": "Reply primarily in Simplified Chinese.",
}


def query_ai(prompt, user_id=0, chat_id=0):
    bot_name = "Aru"
    language = "hinglish"
    if chat_id != 0:
        language = str(get_group_settings(chat_id).get("language", "hinglish")).lower()
    lang_instr = LANGUAGE_INSTRUCTIONS.get(language, LANGUAGE_INSTRUCTIONS["hinglish"])
    system_prompt = ("You are %s, a cute, friendly girl-style Telegram chatbot with a warm playful personality. %s "
                     "Reply naturally like a cheerful chat friend, but never pretend to be a real human. Keep group replies "
                     "concise, useful and context-aware. Do not repeat the same answer unnecessarily. Do not claim actions you "
                     "did not perform. Never reveal you are an AI unless directly asked. Avoid sexual content, abusive "
                     "behavior, manipulation, or unsafe instructions.") % (bot_name, lang_instr)
    messages = [{"role": "system", "content": system_prompt}] + load_memory(user_id, chat_id) + [{"role": "user", "content": prompt}]

    if not GROQ_API_KEY:
        return "AI chat abhi configure nahi hai. Hosting me GROQ_API_KEY set karein; bot ka baaki chat system active hai. 💓"

    payload = {"model": GROQ_MODEL, "messages": messages, "max_completion_tokens": 1024, "temperature": 0.7}
    if "gpt-oss" in GROQ_MODEL:
        payload["reasoning_effort"] = "low"  # otherwise reasoning eats the token budget -> empty replies

    raw_resp = None
    for attempt in range(2):
        try:
            raw_resp = SESSION.post(GROQ_URL, json=payload, timeout=(5, 40),
                                    headers={"Authorization": "Bearer " + GROQ_API_KEY})
        except requests.RequestException as e:
            log.warning("Groq network error: %s", e)
            raw_resp = None
        if raw_resp is not None and (raw_resp.status_code == 429 or raw_resp.status_code >= 500):
            time.sleep(1.5)
            continue
        break
    if raw_resp is None or not 200 <= raw_resp.status_code < 300:
        log.warning("Groq API error: %s", (raw_resp.status_code, raw_resp.text[:300]) if raw_resp is not None else "no response")
        return "Sorry, abhi AI service unavailable hai. Thodi der baad try karein! 🙏"
    try:
        content = raw_resp.json()["choices"][0]["message"].get("content") or ""
    except Exception:
        content = ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    if not content:
        log.warning("Groq empty content: %s", raw_resp.text[:300])
        return "Kuch error ho gaya! Dobara try karein please 🙏"
    save_memory(user_id, prompt, content, chat_id)
    return content


def check_greeting(text):
    t = text.strip().lower()
    if len(t.split()) > 4:
        return None
    for g in ["hi", "hello", "hey", "namaste", "hii", "hy", "helo", "namaskar"]:
        if re.match(r"^" + re.escape(g) + r"(?:\s|[!?,.]|$)", t):
            return "Hii! Main " + BOT_NAME + " hoon! Kya help kar sakti hoon? 😊🚀"
    return None


def check_local(text):
    t = text.strip().lower()
    if len(t) > 60:
        return None
    if "kaise ho" in t or "kaisi ho" in t:
        return "Main bilkul theek hoon! Aap bataiye, kaise hain? 😊💖"
    if "thank" in t or "shukriya" in t:
        return "You're welcome! Kuch aur help chahiye to bataiyega 🌸✨"
    if re.search(r"\bbye\b", t) or "alvida" in t:
        return "Bye! Take care! Phir milenge 👋💕"
    return None


# ============================================================
# MENUS / TEXT BUILDERS
# ============================================================


def admin_keyboard():
    return {"keyboard": [[{"text": "➕ Add To Your Group 💓"}, {"text": "❓ Help 💓"}],
                         [{"text": "👑 Owner 💓"}, {"text": "📊 Database Check 💓"}]],
            "resize_keyboard": True, "one_time_keyboard": False}


def bot_add_to_group_link():
    u = C().bot_username
    if not u:
        return "https://t.me/"
    return "https://t.me/%s?startgroup=true&admin=post_messages+edit_messages+delete_messages+invite_users+pin_messages+restrict_members" % quote(u)


def language_menu_content(chat_id):
    txt = "🌐🎉🚀 <b>SELECT CHAT LANGUAGE / भाषा चुनें</b> ✨🔥\n\nPlease select the preferred language for bot conversation in this chat:"
    c = str(chat_id)
    kb = {"inline_keyboard": [
        [{"text": "🇮🇳 Hindi / Hinglish ✨", "callback_data": "set_lang:hinglish:" + c},
         {"text": "🌾 Bhojpuri 🌸", "callback_data": "set_lang:bhojpuri:" + c}],
        [{"text": "🇬🇧 English 🚀", "callback_data": "set_lang:english:" + c},
         {"text": "🇧🇩 Bengali 💫", "callback_data": "set_lang:bengali:" + c}],
        [{"text": "🇵🇰 Urdu 🌙", "callback_data": "set_lang:urdu:" + c},
         {"text": "🇨🇳 Chinese 🐉", "callback_data": "set_lang:chinese:" + c}]]}
    return txt, kb


def send_language_selection_menu(chat_id):
    txt, kb = language_menu_content(chat_id)
    send_msg(chat_id, txt, kb)


def settings_menu_content(target_chat):
    s = get_group_settings(target_chat)
    jr = "🟢 ON (Auto) 🎉" if flag(s, "joinreq", JOINREQ_DEFAULT_AUTO) else "🔴 OFF (Manual)"
    bl = "🟢 ON ✨" if flag(s, "biolink") else "🔴 OFF"
    st = "🟢 ON 🔥" if flag(s, "badsticker") else "🔴 OFF"
    asv = "🟢 ON 🚀" if flag(s, "antiservice") else "🔴 OFF"
    txt = ("⚙️🔥 <b>Group Settings Panel</b> 🚀✨\n\nJoin Requests OFF = bot auto-approve nahi karega; Admin ko Accept/Reject buttons "
           "milenge. Telegram ki actual join-request setting Bot API se globally switch nahi hoti.")
    t = str(target_chat)
    kb = {"inline_keyboard": [
        [{"text": "Join Requests: " + jr, "callback_data": "toggle_joinreq:" + t}],
        [{"text": "Bio Link Protection: " + bl, "callback_data": "toggle_biolink:" + t}],
        [{"text": "Bad Sticker Filter: " + st, "callback_data": "toggle_sticker:" + t}],
        [{"text": "Anti-Service Shield: " + asv, "callback_data": "toggle_antiservice:" + t}],
        [{"text": "🌐 Language Selection", "callback_data": "lang_menu:" + t}],
        [{"text": "🔙 Help Menu", "callback_data": "show_help_menu"}]]}
    return txt, kb


def send_settings_menu(chat_id):
    txt, kb = settings_menu_content(chat_id)
    send_msg(chat_id, txt, kb)


def help_menu_content():
    txt = ("┏━━━━━━━━━━━━━━━━━━━━━━┓\n┃ 🤖 <b>ARU BOT — COMPLETE HELP</b> ┃\n┗━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
           "💬 <b>CHAT / AI</b>\n"
           "• <code>/start</code> — Start bot\n"
           "• <code>/help</code> — Full help menu\n"
           "• Group AI: bot ko <b>mention</b> ya reply karo\n"
           "• Private chat: normal messages par AI reply\n"
           "• <code>/language</code> — Chat language chuno\n\n"
           "👤 <b>USER MEMORY / INFO</b>\n"
           "• <code>/memory</code> — Apni saved chat memory\n"
           "• <code>/clearmemory</code> — Apni memory delete karo\n"
           "• <code>/id</code> — User/chat ID\n"
           "• <code>/userinfo USER_ID</code> — User profile + saved DP + groups (Admin)\n"
           "• <code>/usergroups USER_ID</code> — User ke tracked groups (Admin)\n"
           "• <code>/users NAME</code> — Name/username search, fuzzy (Admin)\n\n"
           "👥 <b>GROUP</b>\n"
           "• <code>/groupinfo</code> — Group info + link + DP\n"
           "• <code>/groups [NAME]</code> — Saved groups list/search (Admin)\n"
           "• <code>/addedlog</code> — Jin groups me bot add hua uska log (Admin)\n"
           "• <code>/settings</code> — Group settings\n"
           "• <code>/joinreq on|off</code> — Auto/manual request (Admin/Owner only)\n"
           "• <code>/biolink on|off</code> — Bio-link protection\n"
           "• <code>/antiservice on|off</code> — Promo shield\n"
           "• <code>/stickerfilter on|off</code> — Sticker filter\n"
           "• <code>/setwarn TEXT</code> — Custom warning\n"
           "• <code>/setwelcome TEXT</code> — Custom welcome message\n"
           "• <code>/mute [10m|2h|1d]</code> / <code>/unmute</code> / <code>/ban</code> / <code>/unban</code> — (reply karke)\n"
           "• <code>/pin</code> / <code>/unpin</code> — Pin control\n"
           "• <code>/vc [note]</code> — Voice chat alert\n"
           "• <code>/setreply trigger | reply</code> — Custom trigger\n"
           "• <code>/mset t1 = r1 | t2 = r2</code> — Multiple triggers\n"
           "• <code>/delreply trigger</code> — Delete trigger\n"
           "• <code>/replies</code> — List triggers\n\n"
           "🎙️ <b>VOICE / RECORDING</b>\n"
           "• <code>/record</code> or <code>/recording</code> — Start worker\n"
           "• <code>/stoprecord</code> — Stop worker\n"
           "• VC join/leave notifications worker callback se\n\n"
           "🤖 <b>CLONE</b>\n"
           "• <code>/makeclone TOKEN</code> — Create clone\n"
           "• <code>/myclones</code> — Your clones\n"
           "• <code>/deleteclone BOT_ID</code> — Delete clone\n\n"
           "🎵 <b>MUSIC / WELCOME</b>\n"
           "• <code>/song NAME</code> — Search/send song\n"
           "• <code>/addwelcomevideo URL</code> — Add welcome video\n"
           "• <code>/resetwelcomevideos</code> — Reset custom welcome videos\n\n"
           "👑 <b>OWNER / ADMIN</b>\n"
           "• <code>/broadcast MSG</code> — All saved users\n"
           "• <code>/gbroadcast MSG</code> — All saved groups\n"
           "• <code>/broadcast_retry</code> — Retry saved failed deliveries\n"
           "• <code>/db</code> / <code>/dbcheck</code> — Saved user/group count\n"
           "• <code>/user USER_ID MSG</code> — Direct user message\n"
           "• <code>/syncclones</code> — Sab clone webhooks dobara set karo\n"
           "• <code>/resetdb confirm</code> — Full database reset\n\n"
           "🧠 Firebase automatically remembers users, groups, memberships, events, memory and profile snapshots.\n\n"
           "⚠️ <b>Recording note:</b> actual VC audio ke liye external MTProto/VC worker required hai; Bot API alone audio stream record nahi karta.")
    kb = {"inline_keyboard": [
        [{"text": "➕ Add Me To Group 💓", "url": bot_add_to_group_link()}],
        [{"text": "👨‍💻 Developer 💓", "url": OWNER_LINK}, {"text": "📢 Support 💓", "url": SUPPORT_LINK}],
        [{"text": "🤖 Clone Guide 💓", "callback_data": "clone_help_info"}],
        [{"text": "📊 Database Check 💓", "callback_data": "db_check"}],
        [{"text": "🎬 Welcome Video 💓", "callback_data": "welcome_video_info"}]]}
    return txt, kb


def send_full_help_menu(chat_id):
    txt, kb = help_menu_content()
    send_msg(chat_id, txt, kb)


def get_owner_text():
    return ("👑🔥🚀 <b>ROYAL OWNER DETAILS</b> 💯✨🎉\n\n👨‍💻 <b>Developer:</b> <a href=\"%s\"><b>%s</b></a> 💓\n"
            "⚡ <b>Status:</b> System Developer &amp; Founder! 👑🔥🚀💯✨" % (OWNER_LINK, OWNER_NAME))


def premium_welcome_text(user_id, first_name_raw, username_raw, chat_id=0, group_title="", inviter_id=0, inviter_name_raw=""):
    name = h(first_name_raw or "Friend")
    username = "@" + username_raw.lstrip("@") if username_raw and username_raw.upper() != "N/A" else "N/A"
    mention = '<a href="tg://user?id=%d">%s</a>' % (user_id, name) if user_id > 0 else name
    group = h(group_title or "Private Chat")
    inviter = '<a href="tg://user?id=%d">%s</a>' % (inviter_id, h(inviter_name_raw or "Member")) if inviter_id > 0 else ""
    inviter_line = "\n👥 <b>Invited by:</b> %s\n" % inviter if (inviter_id > 0 and inviter_id != user_id and group_title) else ""
    return ("╔══════════════════════╗\n"
            "     ✨ <b>WELCOME TO OUR WORLD</b> ✨\n"
            "╚══════════════════════╝\n"
            "🌷 <b>GROUP NAME</b>\n"
            "Hey %s! 👋\n"
            "Welcome to 🌷 <b>%s</b>\n"
            "Humein bahut khushi hai ki aap hamari family ka hissa bane! 🤝%s\n"
            "╔══════════════════════╗\n"
            "       📜 <b>GROUP RULES</b> 📜\n"
            "╚══════════════════════╝\n"
            "✅ Be Respectful to everyone.\n"
            "✅ Stay Active &amp; Keep the chat alive.\n"
            "❌ No 18+ content or Abusive language.\n"
            "❌ No Spamming or Promotion (Links).\n"
            "❌ Don't PM members without permission.\n\n"
            "╔════════════════════╗\n"
            "       💡 <b>QUICK INFO</b> 💡\n"
            "╚════════════════════╝\n"
            "╭───────────────⟢\n"
            "│ ➻ <b>FIRST NAME</b> : %s\n"
            "│ ➻ <b>USERNAME</b> : %s\n"
            "│ ➻ <b>ID</b> = <code>%d</code>\n"
            "╰───────────────⟢\n\n"
            "Hum umeed karte hain ki aap yahan naye dost banayenge aur enjoy karenge! ✨\n\n"
            "Enjoy your stay! ❤️🎉") % (mention, group, inviter_line, mention, h(username), user_id)


def welcome_keyboard(user_id=0):
    profile = "tg://user?id=%d" % user_id if user_id > 0 else OWNER_LINK
    return {"inline_keyboard": [
        [{"text": "👤 Profile ✨", "url": profile}, {"text": "🤖 Anti-Service 🛡️", "callback_data": "welcome_antiservice"}],
        [{"text": "📜 All Commands 💓", "callback_data": "show_help_menu"}, {"text": "⚙️ Settings", "callback_data": "welcome_settings"}],
        [{"text": "👨‍💻 Developer 💓", "url": OWNER_LINK}, {"text": "📢 Support 💓", "url": SUPPORT_LINK}],
        [{"text": "🤖 Make Your Clone 💓", "callback_data": "clone_help_info"}],
        [{"text": "➕ Add Bot To Group 🚀", "url": bot_add_to_group_link()}]]}


# ---------------- language moderation ----------------


def normalize_language_text(text):
    return re.sub(r"https?://\S+|www\.\S+|t\.me/\S+", " ", text, flags=re.I).strip()


def is_hindi_english_only(text):
    text = normalize_language_text(text)
    if not text:
        return True
    for ch in text:
        if ch.isalpha():
            o = ord(ch)
            if not (("A" <= ch <= "Z") or ("a" <= ch <= "z") or 0x0900 <= o <= 0x097F):
                return False
    return True


def queue_deletion(chat_id, message_id, delay=FOREIGN_LANGUAGE_DELETE_AFTER):
    if not chat_id or not message_id:
        return
    key = "%d:%d" % (chat_id, message_id)
    LOCAL.set("scheduled_deletions", key, {"chat_id": chat_id, "message_id": message_id,
                                           "delete_at": now_ts() + delay, "clone_id": C().clone_id})
    q = LOCAL.read("scheduled_deletions")
    if len(q) > 5000:
        for k in sorted(q, key=lambda x: int(q[x].get("delete_at", 0)))[:500]:
            LOCAL.delete("scheduled_deletions", k)


def process_scheduled_deletions():
    q = LOCAL.read("scheduled_deletions")
    if not q:
        return
    now = now_ts()
    for key, job in q.items():
        if not isinstance(job, dict) or int(job.get("delete_at", 0)) > now:
            continue
        LOCAL.delete("scheduled_deletions", key)
        token = BOT_TOKEN
        cid = str(job.get("clone_id", "") or "")
        if cid:
            rec = get_clone_record(cid)
            tk = decrypt_clone_token(str(rec.get("token_enc", ""))) if rec else None
            if not tk:
                continue
            token = tk
        delete_message(int(job.get("chat_id", 0)), int(job.get("message_id", 0)), token=token)


def deletion_loop():
    while True:
        time.sleep(10)
        try:
            process_scheduled_deletions()
        except Exception:
            log.exception("scheduled deletions failed")


# ---------------- stickers ----------------


def sticker_is_bad(sticker):
    set_name = str(sticker.get("set_name") or "").lower()
    emoji = str(sticker.get("emoji") or "")
    if emoji in ["🔞", "🍆", "🍑", "🍌", "💦", "💋", "🖕"]:
        return True
    return any(w in set_name for w in ["adult", "sex", "nsfw", "porn", "nude", "18plus", "18+", "hot", "gali", "fuck", "xxx"])


def sticker_reply_if_addressed(chat_id, message_id, msg):
    cx = C()
    if not STICKER_REPLY_ONLY_WHEN_ADDRESSED:
        return False
    rf = (msg.get("reply_to_message") or {}).get("from") or {}
    if cx.bot_id <= 0 or int(rf.get("id", 0)) != cx.bot_id:
        return False
    fid = str((msg.get("sticker") or {}).get("file_id", ""))
    if fid:
        r = send_sticker(chat_id, fid, message_id)
        if r.get("ok"):
            return True
    if STICKER_RESPONSES:
        r = send_sticker(chat_id, random.choice(STICKER_RESPONSES), message_id)
        if r.get("ok"):
            return True
    set_reaction(chat_id, message_id, "😂")
    return False


# ============================================================
# MUSIC
# ============================================================


def parse_duration(d):
    if isinstance(d, (int, float)):
        return int(d)
    secs = 0
    parts = str(d or "").split(":")[::-1]
    mult = [1, 60, 3600]
    for i, p in enumerate(parts[:3]):
        try:
            secs += int(float(p)) * mult[i]
        except ValueError:
            pass
    return secs


def search_and_send_song(chat_id, song_name, message_id, first_name_raw, user_id):
    try:
        r = SESSION.get(MUSIC_API, params={"song": song_name}, timeout=12)
        data = r.json()
    except Exception as e:
        send_msg_reply(chat_id, "❌ Network Error: " + h(str(e)[:150]), message_id)
        return
    if not isinstance(data, dict) or not data.get("success") or not data.get("results"):
        send_msg_reply(chat_id, "❌ <b>%s</b> nahi mila 😅\nDusra naam try karo! ✨" % h(song_name), message_id)
        return
    song = data["results"][0]
    title = str(song.get("title") or "Unknown Song")
    artist = song.get("artists") or "Unknown Artist"
    if isinstance(artist, list):
        artist = ", ".join(str(a) for a in artist)
    artist = str(artist)
    duration = song.get("duration") or "0:00"
    dl_url = str(song.get("download_url") or "")
    if not dl_url or not is_safe_remote_url(dl_url):
        log.warning("Rejected unsafe music download URL.")
        send_msg_reply(chat_id, "❌ <b>%s</b> ka safe download link available nahi hai! 🚫" % h(song_name), message_id)
        return
    mention = user_mention(user_id, first_name_raw)
    dur_txt = duration if isinstance(duration, str) else "%d:%02d" % divmod(int(duration), 60)
    caption = ("lo %s 🎵🎧 <b>%s</b> sun lo 😊💜✨🎉\n\n🎤 <b>Artist:</b> %s\n⏱ <b>Duration:</b> %s 🚀"
               % (mention, h(title), h(artist), h(dur_txt)))
    send_audio_with_caption(chat_id, dl_url, title, artist, parse_duration(duration), caption)


def send_audio_with_caption(chat_id, audio_url, title, artist, duration=0, caption=""):
    path, err = safe_download(audio_url, MAX_AUDIO_BYTES, ".mp3", 60)
    if not path:
        send_msg(chat_id, "❌ Song download nahi ho paya 😅 %s 🚫" % (h(err) if err else "Dobara try karo!"))
        return
    try:
        safe_name = re.sub(r"[^\w .()-]+", "", title, flags=re.U).strip() or "song"
        with open(path, "rb") as fh:
            res = tg("sendAudio", {"chat_id": chat_id, "title": title[:64], "performer": artist[:64], "duration": duration,
                                   "caption": caption, "parse_mode": "HTML"},
                     files={"audio": (safe_name + ".mp3", fh, "audio/mpeg")}, timeout=90)
        if not res.get("ok"):
            log.warning("sendAudio failed: %s", res.get("description"))
            send_msg(chat_id, "❌ Telegram par audio send nahi ho paya. File/size ya Telegram limit check karein. 🚫")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================
# CLONE TOKEN ENCRYPTION / ROUTING
# ============================================================


def clone_crypto_key():
    return hashlib.sha256(APP_KEY.encode()).digest()


def encrypt_clone_token(token):
    iv = os.urandom(12)
    ct_tag = AESGCM(clone_crypto_key()).encrypt(iv, token.encode(), None)  # ciphertext || tag(16)
    cipher, tag = ct_tag[:-16], ct_tag[-16:]
    return "v2:" + base64.b64encode(iv + tag + cipher).decode()  # same layout as the PHP version


def decrypt_clone_token(encoded):
    try:
        if encoded.startswith("v2:"):
            raw = base64.b64decode(encoded[3:], validate=True)
            if len(raw) <= 28:
                return None
            iv, tag, cipher = raw[:12], raw[12:28], raw[28:]
            plain = AESGCM(clone_crypto_key()).decrypt(iv, cipher + tag, None)
            return plain.decode() or None
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) <= 16:
            return None
        iv, cipher = raw[:16], raw[16:]
        dec = Cipher(algorithms.AES(clone_crypto_key()), modes.CBC(iv)).decryptor()
        padded = dec.update(cipher) + dec.finalize()
        un = sympadding.PKCS7(128).unpadder()
        return (un.update(padded) + un.finalize()).decode() or None
    except Exception:
        return None


def clone_webhook_secret(bot_id):
    return CLONE_WEBHOOK_SECRET_PREFIX + "_" + hmac.new(APP_KEY.encode(), ("webhook:%s" % bot_id).encode(), hashlib.sha256).hexdigest()[:48]


MAIN_WEBHOOK_SECRET = "tgmain_v1_" + hmac.new(APP_KEY.encode(), b"webhook:main", hashlib.sha256).hexdigest()[:48]


def get_clone_record(clone_id):
    rec = LOCAL.get("clone_index", str(clone_id))
    if isinstance(rec, dict) and rec.get("token_enc"):
        return rec
    d = FB.read_value("/clone_index/" + str(clone_id))
    return d if isinstance(d, dict) else {}


def get_user_clones(user_id):
    c = FB.read_dict("/clones/%d" % user_id)
    local = LOCAL.get("clones", str(user_id)) or {}
    return merge_records(c, local if isinstance(local, dict) else {})


# ============================================================
# RECORDING HOOK
# ============================================================


def call_recording_hook(action, chat_id, chat_title, requested_by, bot_username):
    if not RECORDING_HOOK_URL or not re.match(r"^https?://", RECORDING_HOOK_URL):
        return {"ok": False, "configured": False, "description": "RECORDING_HOOK_URL is not configured."}
    safe_title = re.sub(r"[^\w._ -]+", "", chat_title, flags=re.U).strip() or "Telegram_Group"
    payload = {"action": action, "chat_id": chat_id, "chat_title": chat_title,
               "safe_filename": "VC Recording - %s - %s.ogg" % (safe_title, time.strftime("%Y-%m-%d %H-%M-%S")),
               "requested_by": requested_by, "bot_username": bot_username, "timestamp": now_ts()}
    try:
        r = SESSION.post(RECORDING_HOOK_URL, json=payload, timeout=(5, 15))
        if not 200 <= r.status_code < 300:
            raise RuntimeError("HTTP %d" % r.status_code)
        try:
            resp = r.json()
        except ValueError:
            resp = r.text
        return {"ok": True, "configured": True, "response": resp}
    except Exception as e:
        log.warning("Recording hook %s failed: %s", action, e)
        return {"ok": False, "configured": True, "description": "Recording worker did not accept the request."}


# ============================================================
# BROADCAST
# ============================================================
_broadcast_lock = threading.Lock()


def save_broadcast_failure(btype, target, source_chat, source_mid, text):
    key = "%s:%d:%d:%d" % (btype, target, source_chat, source_mid or 0)
    old = LOCAL.get("broadcast_queue", key) or {}
    LOCAL.set("broadcast_queue", key, {"type": btype, "target_chat_id": target, "source_chat_id": source_chat,
                                       "source_message_id": source_mid, "text": text, "created_at": now_str(),
                                       "attempts": int(old.get("attempts", 0))})
    q = LOCAL.read("broadcast_queue")
    if len(q) > 5000:
        for k in list(q.keys())[:len(q) - 5000]:
            LOCAL.delete("broadcast_queue", k)


def clear_broadcast_failure(btype, target, source_chat, source_mid):
    LOCAL.delete("broadcast_queue", "%s:%d:%d:%d" % (btype, target, source_chat, source_mid or 0))


def broadcast_to_chat(target, source_chat, source_mid, text=""):
    if source_mid is not None and source_mid > 0:
        r = {}
        for _ in range(3):
            r = copy_message(target, source_chat, source_mid)
            if r.get("ok"):
                return r
            if int(r.get("error_code", 0) or 0) == 429:
                time.sleep(max(1, min(int((r.get("parameters") or {}).get("retry_after", 3)), 15)))
                continue
            return r
        return r
    return send_msg_safe_broadcast(target, text)


def retry_broadcast_queue(admin_chat):
    queue_ = LOCAL.read("broadcast_queue")
    if not queue_:
        send_msg(admin_chat, "✅ Broadcast retry queue empty hai. 💓")
        return
    ok = fail = 0
    for key, job in queue_.items():
        if not isinstance(job, dict):
            continue
        btype = str(job.get("type", "user"))
        target = int(job.get("target_chat_id", 0))
        src = int(job.get("source_chat_id", admin_chat))
        mid = job.get("source_message_id")
        mid = int(mid) if mid else None
        r = broadcast_to_chat(target, src, mid, str(job.get("text", "")))
        if r.get("ok"):
            LOCAL.delete("broadcast_queue", key)
            ok += 1
        else:
            fail += 1
            job["attempts"] = int(job.get("attempts", 0)) + 1
            LOCAL.set("broadcast_queue", key, job)
            if is_inactive_chat_error(r):
                LOCAL.delete("broadcast_queue", key)
                (delete_user_from_db if btype == "user" else delete_group_from_db)(str(target))
        time.sleep(0.03)
    send_msg(admin_chat, "🔁 <b>Broadcast Retry Complete</b> 💓\n\n✅ Delivered: <b>%d</b>\n❌ Still pending: <b>%d</b>" % (ok, fail))


def run_broadcast(chat_id, kind, text, source_reply_id):
    """kind = 'user' or 'group'. Runs in its own thread."""
    label = "Official Broadcast" if kind == "user" else "Official Announcement"
    targets = get_all_users() if kind == "user" else get_all_groups()
    total = len(targets)
    word = "Users" if kind == "user" else "Groups"
    try:
        if total == 0:
            send_msg(chat_id, "❌ Database me koi %s nahi mila. 🚫" % ("user" if kind == "user" else "group"))
            return
        init = send_msg(chat_id, "🚀🔥 <b>%s Broadcast Started...</b> 🎯✨\n\n🎯 <b>Target %s:</b> %d" % (word[:-1], word, total))
        status_mid = (init.get("result") or {}).get("message_id")
        count = failed = i = 0
        body = "📢🔥 <b>%s from %s:</b>\n\n%s" % (label, h(OWNER_NAME), h(text))
        step = 50 if kind == "user" else 25
        for tid in list(targets.keys()):
            tid = str(tid)
            if not re.fullmatch(r"-?\d+", tid):
                continue
            if kind == "user" and int(tid) <= 0:
                continue
            i += 1
            r = broadcast_to_chat(int(tid), chat_id, source_reply_id, body)
            if r.get("ok"):
                count += 1
                clear_broadcast_failure(kind, int(tid), chat_id, source_reply_id)
            else:
                failed += 1
                if is_inactive_chat_error(r):
                    (delete_user_from_db if kind == "user" else delete_group_from_db)(tid)
                    clear_broadcast_failure(kind, int(tid), chat_id, source_reply_id)
                else:
                    save_broadcast_failure(kind, int(tid), chat_id, source_reply_id, body)
            time.sleep(0.04)
            if i % step == 0 and status_mid:
                edit_message_text(chat_id, int(status_mid), "🚀🔥 <b>%s Broadcast Running...</b> ⚡✨\n\n📊 Processed: <b>%d / %d</b>\n✅ Delivered: %d | ❌ Failed: %d"
                                  % (word[:-1], i, total, count, failed))
        final = ("✅🎉 <b>%s Broadcast Completed!</b> 🚀💯\n\n👥 <b>Total Targeted:</b> %d\n🎉 <b>Delivered:</b> %d\n⚠️ <b>Failed:</b> %d"
                 % (word[:-1], total, count, failed))
        if status_mid:
            edit_message_text(chat_id, int(status_mid), final)
        else:
            send_msg(chat_id, final)
    finally:
        _broadcast_lock.release()


def start_broadcast(chat_id, kind, text, source_reply_id):
    if not _broadcast_lock.acquire(blocking=False):
        send_msg(chat_id, "⏳ Ek broadcast already chal raha hai. Complete hone do. ✨")
        return
    ctx = C()
    threading.Thread(target=run_in_ctx, args=(ctx, run_broadcast, chat_id, kind, text, source_reply_id), daemon=True).start()


# ============================================================
# USER / GROUP SEARCH HELPERS (new)
# ============================================================


def fuzzy_score(query, *fields):
    q = query.lower().strip().lstrip("@")
    if not q:
        return 1.0
    best = 0.0
    for f in fields:
        f = str(f or "").lower()
        if not f:
            continue
        if q == f:
            return 3.0
        if q in f:
            best = max(best, 2.0 + len(q) / max(len(f), 1))
            continue
        for tok in [f] + re.split(r"[\s._\-|/]+", f):
            if tok and len(tok) >= 2:
                best = max(best, difflib.SequenceMatcher(None, q, tok).ratio())
    return best if best >= 0.6 else 0.0


def cmd_users_search(chat_id, query):
    users = get_all_users()
    rows = []
    for uid, u in users.items():
        if not re.fullmatch(r"-?\d+", str(uid)) or not isinstance(u, dict):
            continue
        sc = 3.0 if query.strip() == str(uid) else fuzzy_score(query, u.get("name"), u.get("username"))
        if sc > 0:
            rows.append((sc, int(u.get("last_seen_ts", 0) or 0), str(uid), u))
    rows.sort(key=lambda r: (-r[0], -r[1]) if query.strip() else (0, -r[1]))
    if not rows:
        send_msg(chat_id, "❌ Koi user nahi mila: <code>%s</code>" % h(query))
        return
    lines = ["👤🔎 <b>USER SEARCH</b> — %d match%s%s\n" % (len(rows), "es" if len(rows) != 1 else "", (" for <code>%s</code>" % h(query)) if query else "")]
    for sc, ts, uid, u in rows[:40]:
        un = str(u.get("username") or "")
        lines.append('• <a href="tg://user?id=%s">%s</a> — %s — <code>%s</code>' % (uid, h(u.get("name") or "Unknown"), ("@" + h(un)) if un else "N/A", uid))
    if len(rows) > 40:
        lines.append("\n… aur %d users. Query specific karo." % (len(rows) - 40))
    send_msg(chat_id, "\n".join(lines))


def cmd_groups_list(chat_id, query):
    groups = get_all_groups()
    rows = []
    for gid, g in groups.items():
        if not re.fullmatch(r"-?\d+", str(gid)) or not isinstance(g, dict):
            continue
        sc = fuzzy_score(query, g.get("title"), g.get("username")) if query else 1.0
        if sc > 0:
            rows.append((sc, int(g.get("last_active_ts", 0) or 0), str(gid), g))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    if not rows:
        send_msg(chat_id, "❌ Koi group nahi mila.")
        return
    lines = ["👥📋 <b>GROUP LIST</b> — %d total\n" % len(rows)]
    for sc, ts, gid, g in rows[:60]:
        un = str(g.get("username") or "")
        link = (' — <a href="https://t.me/%s">@%s</a>' % (h(un), h(un))) if un else ""
        lines.append("• %s%s — <code>%s</code>" % (h(g.get("title") or "Unknown"), link, gid))
    if len(rows) > 60:
        lines.append("\n… aur %d groups. Name se search karo: /groups NAME" % (len(rows) - 60))
    send_msg(chat_id, "\n".join(lines))


def log_group_added(chat, actor, new_status):
    cid = int(chat.get("id", 0))
    key = "%d_%d" % (int(time.time() * 1000), abs(cid))
    rec = {"chat_id": cid, "title": str(chat.get("title", "")), "username": str(chat.get("username", "")),
           "type": str(chat.get("type", "")), "added_by_id": int(actor.get("id", 0) or 0),
           "added_by_name": str(actor.get("first_name", "")), "added_by_username": str(actor.get("username", "")),
           "status": new_status, "added_at": now_str(), "added_ts": now_ts()}
    LOCAL.set("group_add_log", key, rec)
    FB.write("/group_add_log/" + key, rec, "PUT")
    return rec


def cmd_added_log(chat_id):
    rows = sorted(LOCAL.read("group_add_log").values(), key=lambda r: int(r.get("added_ts", 0)), reverse=True)
    if not rows:
        fb = FB.read_dict("/group_add_log")
        rows = sorted([v for v in fb.values() if isinstance(v, dict)], key=lambda r: int(r.get("added_ts", 0)), reverse=True)
    if not rows:
        send_msg(chat_id, "📭 Abhi tak koi group add log nahi hai.")
        return
    lines = ["📜 <b>BOT ADDED TO GROUPS — LAST %d</b>\n" % min(len(rows), 20)]
    for r in rows[:20]:
        un = str(r.get("username") or "")
        lines.append("• <b>%s</b>%s\n  🆔 <code>%s</code> | 👤 %s (<code>%s</code>)\n  🕒 %s" % (
            h(r.get("title") or "Unknown"), (" (@%s)" % h(un)) if un else "", r.get("chat_id"),
            h(r.get("added_by_name") or "?"), r.get("added_by_id"), h(r.get("added_at"))))
    send_msg(chat_id, "\n".join(lines))


# ============================================================
# UPDATE DEDUPLICATION
# ============================================================
_seen_updates = {}
_seen_lock = threading.Lock()
_seen_msgs = OrderedDict()


def is_duplicate_update(bot_key, update_id):
    with _seen_lock:
        od = _seen_updates.setdefault(bot_key, OrderedDict())
        if update_id in od:
            return True
        od[update_id] = 1
        while len(od) > 2000:
            od.popitem(last=False)
        return False


def is_duplicate_message(bot_id, chat_id, message_id):
    if not chat_id or not message_id:
        return False
    key = (bot_id, chat_id, message_id)
    with _seen_lock:
        if key in _seen_msgs:
            return True
        _seen_msgs[key] = 1
        while len(_seen_msgs) > 5000:
            _seen_msgs.popitem(last=False)
        return False


# ============================================================
# HANDLERS
# ============================================================
EXEC = ThreadPoolExecutor(max_workers=WORKERS)


def run_in_ctx(ctx, fn, *args):
    set_ctx(ctx)
    try:
        return fn(*args)
    except Exception:
        log.error("Handler error:\n%s", traceback.format_exc())


def process_update(update):
    if "chat_join_request" in update:
        return handle_join_request(update["chat_join_request"])
    if "callback_query" in update:
        return handle_callback(update["callback_query"])
    if "chat_member" in update:
        return handle_chat_member(update["chat_member"])
    if "my_chat_member" in update:
        return handle_my_chat_member(update["my_chat_member"])
    if "message" in update:
        return handle_message(update["message"])


# ---------------- join requests ----------------


def handle_join_request(req):
    cx = C()
    chat = req.get("chat") or {}
    frm = req.get("from") or {}
    req_chat = int(chat.get("id", 0))
    title_raw = str(chat.get("title") or "Group")
    title = h(title_raw)
    req_username = str(chat.get("username") or "")
    req_user = int(frm.get("id", 0))
    user_chat_id = int(req.get("user_chat_id") or req_user)
    fname_raw = str(frm.get("first_name") or "Friend")
    fname = h(fname_raw)
    uname = "@" + frm["username"] if frm.get("username") else "N/A"

    if req_chat:
        save_group(req_chat, title_raw, req_username)
    if not (req_chat and req_user):
        return
    remember_user_profile(req_user, fname_raw, str(frm.get("username") or ""), req_chat)
    track_membership(req_chat, req_user, "pending", {"via_join_request": True, "actor_id": req_user})
    remember_event("join_request", req_chat, req_user, {"title": title_raw, "username": uname})
    settings = get_group_settings(req_chat)
    auto = flag(settings, "joinreq", JOINREQ_DEFAULT_AUTO)
    start_url = ("https://t.me/%s?start=welcomed" % cx.bot_username) if cx.bot_username else ""
    dm_kb = {"inline_keyboard": [[{"text": "🚀 Open / Start Bot Chat ✨", "url": start_url}]]} if start_url else None

    if auto:
        res = approve_join(req_chat, req_user)
        if res.get("ok"):
            track_membership(req_chat, req_user, "member", {"joined_at": now_str(), "via_join_request": True, "actor_id": req_user,
                                                           "title": title_raw, "username": req_username})
            remember_event("join_request_auto_approved", req_chat, req_user, {"title": title_raw})
            if notify_once("join_auto_approved:%d:%d" % (req_chat, req_user), 21600):
                send_admin_notification("📥✅ <b>Join Request Auto-Approved</b>\n\n👤 <b>User:</b> <code>%d</code>\n💬 <b>Group:</b> <b>%s</b>" % (req_user, title))
            pm = ("┌───────────────────────┐\n│ 🎉🥳🚀🔥💯 <b>JOIN REQUEST APPROVED!</b> │\n└───────────────────────┘\n\n"
                  "Hii <b>%s</b>! Aapki join request <b>%s</b> ke liye accept ho gayi hai! Welcome! 💜✨🥰🔥🥳\n\n"
                  "📋 <b>Details:</b>\n👤 <b>Name:</b> %s\n🆔 <b>User ID:</b> <code>%d</code>\n🔗 <b>Username:</b> %s"
                  % (fname, title, fname, req_user, h(uname)))
            if JOIN_DM_ON:
                send_safe_dm(user_chat_id, pm, dm_kb)
        else:
            log.info("auto approve failed: %s", res.get("description"))
    else:
        mention = '<a href="tg://user?id=%d">%s</a>' % (req_user, fname)
        admin_text = ("📥🔥 <b>New Join Request Received!</b> 👀✨\n\n👤 <b>User:</b> %s\n🆔 <b>ID:</b> <code>%d</code>\n"
                      "🔗 <b>Username:</b> %s\n💬 <b>Group:</b> <b>%s</b>\n\nAdmins please select action: 👇✨" % (mention, req_user, h(uname), title))
        kb = {"inline_keyboard": [
            [{"text": "✅ Accept Request", "callback_data": "req_app:%d:%d" % (req_chat, req_user)},
             {"text": "❌ Reject Request", "callback_data": "req_dec:%d:%d" % (req_chat, req_user)}],
            [{"text": "⚙️ Manual Mode: ON", "callback_data": "show_joinreq:%d" % req_chat}]]}
        if notify_once("join_request:%d:%d" % (req_chat, req_user), 21600):
            sent = {"ok": False}
            if JOIN_REQUEST_ADMIN_NOTIFY_ON and ADMIN_CHANNEL_ID != 0:
                sent = send_admin_notification(admin_text, kb)  # buttons included: global admins / group admins can act
            if not sent.get("ok"):
                send_msg(req_chat, admin_text, kb)
        if JOIN_REQUEST_PENDING_DM_ON and user_chat_id > 0:
            send_safe_dm(user_chat_id, "📥 <b>Join Request Received</b>\n\nHii <b>%s</b>! Aapki <b>%s</b> ki join request receive ho gayi hai. 💜✨" % (fname, title), dm_kb)


# ---------------- callbacks ----------------


def handle_callback(cb):
    cx = C()
    cb_id = cb.get("id", "")
    data = str(cb.get("data") or "")
    cb_user = int((cb.get("from") or {}).get("id", 0))
    m = cb.get("message") or {}
    cb_chat = int((m.get("chat") or {}).get("id", 0))
    cb_chat_type = (m.get("chat") or {}).get("type", "")

    if data.startswith("lang_menu:"):
        target = int(data.split(":", 1)[1] or 0)
        if target and is_chat_admin(target, cb_user):
            txt, kb = language_menu_content(target)
            edit_cb_message(cb, txt, kb)
            answer_cb(cb_id, "🌐 Language menu")
        else:
            answer_cb(cb_id, "❌ Only Admins can set group language! 🚫", True)
        return

    if data.startswith("set_lang:"):
        parts = data.split(":", 2)
        lang = parts[1] if len(parts) > 1 else "hinglish"
        target = int(parts[2]) if len(parts) > 2 and re.fullmatch(r"-?\d+", parts[2]) else cb_chat
        if lang not in LANGUAGE_INSTRUCTIONS:
            lang = "hinglish"
        if is_chat_admin(target, cb_user):
            set_group_setting(target, "language", lang)
            answer_cb(cb_id, "✅ Language set to: %s 🌐✨" % lang.upper())
            edit_cb_message(cb, "✅🎉 <b>Chat language set to: %s</b> 🚀🔥" % lang.upper())
        else:
            answer_cb(cb_id, "❌ Only Admins can set group language! 🚫", True)
        return

    if data.startswith("req_app:") or data.startswith("req_dec:"):
        approve = data.startswith("req_app:")
        parts = data.split(":", 2)
        target_chat = int(parts[1]) if len(parts) > 1 and re.fullmatch(r"-?\d+", parts[1]) else 0
        target_user = int(parts[2]) if len(parts) > 2 and re.fullmatch(r"-?\d+", parts[2]) else 0
        if not (target_chat and target_user and (cb_user in cx.admin_ids or is_actual_group_admin(target_chat, cb_user))):
            answer_cb(cb_id, "❌ Sirf Group Admins ise %s kar sakte hain! 🚫" % ("approve" if approve else "decline"), True)
            return
        if approve:
            res = approve_join(target_chat, target_user)
            if res.get("ok"):
                track_membership(target_chat, target_user, "member", {"joined_at": now_str(), "via_join_request": True, "actor_id": cb_user})
                answer_cb(cb_id, "✅ Request Approved! 🎉🔥")
                edit_cb_message(cb, "✅🎉 <b>Join request approved by Admin.</b> 🚀🔥💯", {"inline_keyboard": [[{"text": "✅ Approved", "callback_data": "noop"}]]})
                info = tg("getChat", {"chat_id": target_chat})
                title_raw = str((info.get("result") or {}).get("title") or "group")
                rec = get_user_record(target_user)
                start_url = "https://t.me/%s?start=welcomed" % cx.bot_username
                kb = {"inline_keyboard": [[{"text": "🚀 Open Bot / Start", "url": start_url}]]}
                remember_event("join_request_approved", target_chat, target_user, {"approved_by": cb_user})
                if JOIN_DM_ON:
                    send_safe_dm(target_user, premium_welcome_text(target_user, str(rec.get("name") or "Friend"),
                                                                    str(rec.get("username") or ""), target_chat, title_raw), kb)
            else:
                answer_cb(cb_id, "❌ Request expire ho chuki hai ya approve nahi ho saki! ⚠️", True)
        else:
            res = decline_join(target_chat, target_user)
            if res.get("ok"):
                track_membership(target_chat, target_user, "declined", {"updated_by": cb_user})
                remember_event("join_request_declined", target_chat, target_user, {"declined_by": cb_user})
                answer_cb(cb_id, "❌ Request Declined! 🚫")
                edit_cb_message(cb, "❌🚫 <b>Join request declined by Admin.</b>", {"inline_keyboard": [[{"text": "❌ Rejected", "callback_data": "noop"}]]})
            else:
                answer_cb(cb_id, "❌ Request expire ho chuki hai! ⚠️", True)
        return

    if data == "noop":
        answer_cb(cb_id, "This request is already processed.")
        return

    if data.startswith("show_joinreq:"):
        target = int(data.split(":", 1)[1] or 0)
        if target and is_chat_admin(target, cb_user):
            manual = not flag(get_group_settings(target), "joinreq", JOINREQ_DEFAULT_AUTO)
            answer_cb(cb_id, "Manual Accept/Reject mode is active." if manual else "Auto approval mode is active.")
        else:
            answer_cb(cb_id, "❌ Admin rights required.", True)
        return

    if data == "welcome_antiservice":
        if cb_chat < 0 and cb_chat_type in ("group", "supergroup"):
            if not is_chat_admin(cb_chat, cb_user):
                answer_cb(cb_id, "❌ Sirf group admin/owner use kar sakta hai.", True)
                return
            new = not flag(get_group_settings(cb_chat), "antiservice")
            set_group_setting(cb_chat, "antiservice", new)
            answer_cb(cb_id, "🛡️ Anti-Service ON kar diya." if new else "⚠️ Anti-Service OFF kar diya.")
            return
        answer_cb(cb_id, "🛡️ Anti-Service protection group setting hai. Group me use karein.", True)
        return

    if data == "welcome_settings":
        if cb_chat < 0 and cb_chat_type in ("group", "supergroup"):
            if not is_chat_admin(cb_chat, cb_user):
                answer_cb(cb_id, "❌ Admin rights required.", True)
                return
            txt, kb = settings_menu_content(cb_chat)
            edit_cb_message(cb, txt, kb)
            answer_cb(cb_id, "⚙️ Settings opened.")
            return
        answer_cb(cb_id, "⚙️ Settings sirf group admins ke liye hain.", True)
        return

    if data == "show_help_menu":
        txt, kb = help_menu_content()
        edit_cb_message(cb, txt, kb)
        answer_cb(cb_id, "Menu Updated! ✨🚀")
        return

    if data == "owner_info":
        edit_cb_message(cb, get_owner_text(), {"inline_keyboard": [[{"text": "🔙 Back To Menu ✨", "callback_data": "show_help_menu"}]]})
        answer_cb(cb_id, "Owner Info! 👑🔥")
        return

    if data == "db_check":
        if cb_user not in cx.admin_ids:
            answer_cb(cb_id, "❌ Sirf bot admins ke liye. 🚫", True)
            return
        users, groups = get_all_users(), get_all_groups()
        edit_cb_message(cb, "📊 <b>DATABASE CHECK</b> 💓\n\n👤 Users: <b>%d</b>\n👥 Groups: <b>%d</b>\n📡 Firebase + Local Backup active.\n🤖 AI Model: <code>%s</code>\n🚀 Broadcast union database se targets leta hai."
                        % (len(users), len(groups), h(GROQ_MODEL)),
                        {"inline_keyboard": [[{"text": "🔙 Back To Help 💓", "callback_data": "show_help_menu"}]]})
        answer_cb(cb_id, "Database checked! 💓")
        return

    if data == "welcome_video_info":
        edit_cb_message(cb, "🎬 <b>WELCOME VIDEO</b> 💓\n\nDefault video active hai. Group admin <code>/addwelcomevideo URL</code> se custom video add kar sakta hai.\n\nCurrent default: <code>%s</code>" % h(DEFAULT_WELCOME_VIDEO),
                        {"inline_keyboard": [[{"text": "🔙 Back To Help 💓", "callback_data": "show_help_menu"}]]})
        answer_cb(cb_id, "Welcome video settings! 🎬")
        return

    if data == "clone_help_info":
        txt = ("🤖✨ <b>CLONE BOT SYSTEM GUIDE</b> 🚀\n\nApna khud ka clone bot banane ke liye private chat me ye command bhejo:\n"
               "<code>/makeclone &lt;YOUR_BOT_TOKEN&gt;</code>\n\nApne clones dekhne ke liye: <code>/myclones</code>\n"
               "Delete karne ke liye: <code>/deleteclone &lt;BOT_ID&gt;</code> ✨💯")
        edit_cb_message(cb, txt, {"inline_keyboard": [[{"text": "🔙 Back To Menu ✨", "callback_data": "show_help_menu"}]]})
        answer_cb(cb_id, "Clone Info! 🤖🚀")
        return

    if data.startswith("toggle_"):
        key_map = {"toggle_joinreq": "joinreq", "toggle_biolink": "biolink", "toggle_sticker": "badsticker", "toggle_antiservice": "antiservice"}
        tkey, _, rest = data.partition(":")
        target = int(rest) if re.fullmatch(r"-?\d+", rest or "") else 0
        if tkey in key_map and target and is_chat_admin(target, cb_user):
            key = key_map[tkey]
            cur = flag(get_group_settings(target), key, JOINREQ_DEFAULT_AUTO if key == "joinreq" else True)
            new = not cur
            set_group_setting(target, key, new)
            labels = {"joinreq": "ON (Auto-Approve) 🟢🎉" if new else "OFF (Manual) 🔴",
                      "biolink": "ON 🟢" if new else "OFF 🔴",
                      "badsticker": "ON (Bad Stickers Deleted) 🟢" if new else "OFF 🔴",
                      "antiservice": "ON (Auto-Deleted) 🟢" if new else "OFF 🔴"}
            answer_cb(cb_id, "%s set to: %s" % (tkey, labels[key]))
            txt, kb = settings_menu_content(target)
            edit_cb_message(cb, txt, kb)
        else:
            answer_cb(cb_id, "❌ Admin rights required! 🚫", True)
        return


# ---------------- chat_member / my_chat_member ----------------


def handle_chat_member(cm):
    cx = C()
    c = cm.get("chat") or {}
    u = (cm.get("new_chat_member") or {}).get("user") or {}
    cid = int(c.get("id", 0))
    uid = int(u.get("id", 0))
    old = str((cm.get("old_chat_member") or {}).get("status", "left"))
    new = str((cm.get("new_chat_member") or {}).get("status", "left"))
    joined = old in ("left", "kicked") and new in ("member", "administrator", "creator")
    left = old in ("member", "administrator", "creator") and new in ("left", "kicked")
    if not (cid and uid and c.get("type") in ("group", "supergroup")):
        return
    title = str(c.get("title") or "Group")
    uname = str(c.get("username") or "")
    remember_group_profile(cid, title, uname)
    if not u.get("is_bot"):
        remember_user_profile(uid, str(u.get("first_name") or "Friend"), str(u.get("username") or ""), cid)
    track_membership(cid, uid, new, {"joined_at": now_str() if joined else None, "left_at": now_str() if left else None,
                                     "invite_link": (cm.get("invite_link") or {}).get("invite_link", ""),
                                     "via_join_request": bool(cm.get("via_join_request", False)),
                                     "actor_id": int((cm.get("from") or {}).get("id", 0)), "title": title, "username": uname})
    if joined:
        remember_event("member_join", cid, uid, {"title": title})
        # Premium welcome is sent by the new_chat_members service-message handler (no second welcome here).
    elif left and uid != cx.bot_id and not u.get("is_bot"):
        remember_event("member_leave", cid, uid, {"title": title})
        send_msg(cid, "🚪 <b>Member Left</b>\n\n👤 %s\n🆔 <code>%d</code>" % (user_mention(uid, str(u.get("first_name") or "Member")), uid))


def handle_my_chat_member(m):
    c = m.get("chat") or {}
    cid = int(c.get("id", 0))
    ctype = str(c.get("type", ""))
    old = str((m.get("old_chat_member") or {}).get("status", ""))
    new = str((m.get("new_chat_member") or {}).get("status", ""))
    actor = m.get("from") or {}
    if ctype in ("group", "supergroup") and cid:
        title_raw = str(c.get("title") or "Group")
        title = h(title_raw)
        if new in ("kicked", "left"):
            send_admin_notification("🚪 <b>Bot Removed From Group</b>\n\n💬 <b>%s</b>\n🆔 <code>%d</code>\n👤 By: %s" % (title, cid, user_mention(int(actor.get("id", 0)), str(actor.get("first_name", "")))))
            delete_group_from_db(str(cid))
            return
        if old in ("left", "kicked", "") and new in ("member", "administrator", "restricted"):
            save_group(cid, title_raw, str(c.get("username") or ""), {"db_schema": DB_SCHEMA_VERSION})
            rec = log_group_added(c, actor, new)
            cnt = tg("getChatMemberCount", {"chat_id": cid})
            count_txt = str(cnt.get("result", "N/A")) if cnt.get("ok") else "N/A"
            un = str(c.get("username") or "")
            send_admin_notification("➕🎉 <b>Bot Added To New Group</b>\n\n💬 <b>Group:</b> %s\n🆔 <b>ID:</b> <code>%d</code>\n🔗 <b>Username:</b> %s\n👥 <b>Members:</b> %s\n👤 <b>Added by:</b> %s (<code>%s</code>)\n🛡 <b>Status:</b> %s"
                                    % (title, cid, ("@" + h(un)) if un else "N/A", count_txt, user_mention(int(actor.get("id", 0)), str(actor.get("first_name", "User"))), rec["added_by_id"], new))
            ctx = C()
            EXEC.submit(run_in_ctx, ctx, save_group_snapshot, cid)
            return
        if new == "administrator" and old != "administrator":
            send_admin_notification("🛡️ <b>Bot Admin Added</b>\n\n💬 <b>%s</b>\n🤖 Bot ko admin rights mil gaye hain. ✅" % title)
        elif old == "administrator" and new != "administrator":
            send_admin_notification("⚠️ <b>Bot Admin Status Changed</b>\n\n💬 <b>%s</b>\n🤖 Bot ab administrator nahi hai. ❌" % title)
    elif ctype == "private" and cid > 0:
        if new == "kicked":
            LOCAL.patch("users", str(cid), {"blocked_bot": True})
            FB.write("/users/%d" % cid, {"blocked_bot": True}, "PATCH")
        elif new == "member" and old == "kicked":
            LOCAL.patch("users", str(cid), {"blocked_bot": False})
            FB.write("/users/%d" % cid, {"blocked_bot": False}, "PATCH")


# ---------------- message ----------------

BAD_WORDS_SHORT = ["bc", "mc", "b.c", "m.c"]
BAD_WORDS_LONG = ["chutiya", "chutia", "bhenchod", "behenchod", "madarchod", "bsdk", "bhosdike", "gaand", "gand", "lund", "loda",
                  "harami", "kamina", "kamine", "kutte"]
SERVICE_KEYWORDS = [
    "paid service", "cheap service", "sell service", "buy service", "dm for service", "paid promo", "promotion available", "paid work",
    "dm for work", "paid bot", "sell bot", "buy bot", "follower sell", "subscriber sell", "cheap followers", "cheap views", "smm panel",
    "smm service", "crypto signal", "invest project", "earn money", "daily income", "hack tool", "cracked script", "dm me for setup",
    "service available", "service chahiye", "service milega", "paid promo available", "promo available", "paid promotion",
    "followers available", "views available", "members available", "subscriber available", "paise kamaye", "online earning",
    "earning proof", "investment", "profit guaranteed", "deal available", "inbox me", "dm me", "contact me", "setup service",
    "bot service", "account service"]
_BAD_SHORT_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in BAD_WORDS_SHORT) + r")(?![a-z0-9])", re.I)
_BAD_LONG_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(w) for w in BAD_WORDS_LONG) + r")(?:s|a|e|i|o|pa|pan|pana)?(?![a-z])", re.I)


def contains_bad_word(text):
    low = text.lower()
    if _BAD_SHORT_RE.search(low):
        return True
    if _BAD_LONG_RE.search(low):
        return True
    squashed = re.sub(r"(?<=\w)[\s._\-*]+(?=\w)", "", low)  # b.h.o.s.d.i.k.e style obfuscation
    return squashed != low and _BAD_LONG_RE.search(squashed) is not None


def is_single_emoji(t):
    t2 = t.replace("\ufe0f", "").replace("\u200d", "")
    t2 = "".join(ch for ch in t2 if not 0x1F3FB <= ord(ch) <= 0x1F3FF)
    if not t2 or len(t2) > 3:
        return False
    first = t2[0]
    return unicodedata.category(first) in ("So", "Sk") or ord(first) >= 0x1F000 or first in "❤✨⚡⭐"


def norm_emoji(t):
    return t.replace("\ufe0f", "")


def parse_duration_arg(s):
    mm = re.fullmatch(r"(\d+)\s*([mhdw])", (s or "").strip().lower())
    if not mm:
        return 0
    n = int(mm.group(1))
    return now_ts() + n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[mm.group(2)]


def handle_message(msg):
    cx = C()
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id", 0))
    chat_type = str(chat.get("type", ""))
    chat_title = str(chat.get("title") or "")
    chat_username = str(chat.get("username") or "")
    text = str(msg.get("text") or "").strip()
    caption = str(msg.get("caption") or "").strip()
    message_id = int(msg.get("message_id", 0))
    frm = msg.get("from") or {}
    user_id = int(frm.get("id", 0))
    first_raw = str(frm.get("first_name") or "Friend")
    first = h(first_raw)
    username = "@" + frm["username"] if frm.get("username") else "N/A"
    is_group = chat_type in ("group", "supergroup")
    is_global_admin = user_id in cx.admin_ids
    sender_chat = msg.get("sender_chat") or {}
    is_anon = bool(sender_chat) and int(sender_chat.get("id", 0)) == chat_id  # anonymous admin posting as the group
    if is_anon:
        user_id = 1087

    # ---- voice chat service events ----
    if is_group:
        title_h = h(chat_title or "Group")
        if "video_chat_started" in msg:
            send_msg(chat_id, "🎙️ <b>Voice Chat Started!</b>\n\n📢 <b>%s</b> ka voice chat start ho gaya hai. 🔥🎧" % title_h)
            return
        if "video_chat_ended" in msg:
            dur = int((msg["video_chat_ended"] or {}).get("duration", 0))
            dtxt = "⏱ <b>Duration:</b> <code>%s</code>\n" % time.strftime("%H:%M:%S", time.gmtime(dur)) if dur > 0 else ""
            send_msg(chat_id, "🛑 <b>Voice Chat Ended!</b>\n\n%s📢 <b>%s</b> ka voice chat khatam ho gaya. 👋🎧" % (dtxt, title_h))
            return
        if "video_chat_participants_invited" in msg:
            names = [h(u.get("first_name", "User")) for u in (msg["video_chat_participants_invited"].get("users") or [])]
            send_msg(chat_id, "🎙️ <b>VC Invite!</b>\n\n👥 Invited: %s\n🎧 Voice Chat join kar sakte hain." % (", ".join(names) if names else "members"))
            return

    if message_id > 0 and is_duplicate_message(cx.bot_id, chat_id, message_id):
        return

    # ---- register user ----
    if user_id > 0 and not frm.get("is_bot") and user_id not in SPECIAL_SENDER_IDS:
        existing = get_user_record(user_id)
        old_name = str(existing.get("name") or "")
        old_un = str(existing.get("username") or "").lstrip("@")
        new_un = username.lstrip("@")
        new_un_clean = "" if new_un.upper() == "N/A" else new_un
        if existing and ((old_name and old_name != first_raw) or (old_un and old_un != new_un_clean)) and is_group:
            if notify_once("userchange:%d:%s:%s" % (user_id, first_raw, new_un_clean), 3600):
                send_admin_notification(
                    "┌───────────────────────┐\n│ ⚠️👀🔥 <b>USER DETAILS CHANGED</b> │\n└───────────────────────┘\n\n"
                    "👤 <b>User:</b> <a href=\"tg://user?id=%d\">%s</a>\n🆔 <b>User ID:</b> <code>%d</code>\n\n"
                    "🔴 <b>Old Name:</b> %s\n🟢 <b>New Name:</b> %s\n🔴 <b>Old Username:</b> %s\n🟢 <b>New Username:</b> %s"
                    % (user_id, first, user_id, h(old_name), first, h(old_un or "N/A"), h(new_un_clean or "N/A")))
        save_user(user_id, first_raw, username, {"last_chat_id": chat_id, "db_schema": DB_SCHEMA_VERSION})

    if is_group:
        remember_group_profile(chat_id, chat_title, chat_username)

    def admin_here():
        return is_anon or is_chat_admin(chat_id, user_id)

    # ---- foreign language filter ----
    if (FOREIGN_LANGUAGE_FILTER_ON and is_group and text and not text.startswith("/") and not is_anon
            and user_id not in SPECIAL_SENDER_IDS and not is_hindi_english_only(text) and not admin_here()):
        queue_deletion(chat_id, message_id)
        r = send_msg(chat_id, "⚠️🌐 <b>Hindi/English ke alawa language allowed nahi hai.</b> Ye message 3 minutes ke andar delete ho jayega. 💓")
        if r.get("ok"):
            queue_deletion(chat_id, int(r["result"]["message_id"]), 60)
        return

    # ---- new members welcome ----
    if "new_chat_members" in msg:
        handle_new_members(msg, chat_id, chat_title, chat_username)
        return

    # ---- stickers ----
    if "sticker" in msg:
        sticker = msg["sticker"]
        if is_group:
            if flag(get_group_settings(chat_id), "badsticker") and not admin_here() and sticker_is_bad(sticker):
                delete_message(chat_id, message_id)
                send_msg(chat_id, "⚠️🚫 %s <b>Bad sticker allowed nahi hai!</b> ❌" % user_mention(user_id, first_raw))
                return
        if sticker_reply_if_addressed(chat_id, message_id, msg) and AI_REACTION_ON:
            set_reaction(chat_id, message_id, "😂")
        return

    # ---- emoji auto reply ----
    if chat_type == "private" and text and not text.startswith("/") and is_single_emoji(text):
        t = norm_emoji(text)
        pool = EMOJI_RESPONSES_GENERAL
        if t in [norm_emoji(x) for x in ["❤️", "💖", "💕", "💋", "🥰", "😍", "💞"]]:
            pool = EMOJI_RESPONSES_LOVE
        elif t in ["😂", "🤣", "😜", "🙈", "🤪"]:
            pool = EMOJI_RESPONSES_FUN
        elif t in ["🔥", "🎉", "🥳", "🚀", "✨"]:
            pool = EMOJI_RESPONSES_PARTY
        elif t in ["😭", "😢", "💔", "🥺"]:
            pool = EMOJI_RESPONSES_SAD
        elif t in ["😎", "💯", "💪", "⚡"]:
            pool = EMOJI_RESPONSES_COOL
        send_msg_reply(chat_id, random.choice(pool), message_id)
        return

    if not text and not caption:
        return

    full_text = text if text else caption
    raw_command, cmd_args = split_cmd(full_text)
    cmd_key = ""
    if raw_command.startswith("/"):
        base, _, suffix = raw_command.partition("@")
        if suffix and cx.bot_username and suffix.lower() != cx.bot_username.lower():
            return  # command meant for another bot
        cmd_key = base.lower()
    addressed = is_group and is_bot_addressed(msg, cx.bot_username, cx.bot_id)

    # ---- owner detection ----
    if re.search(r"\b(owner|creator|malik|boss|admin kaun hai)\b", full_text, re.I) and len(full_text.split()) <= 6 and (chat_type == "private" or addressed) and not cmd_key:
        send_msg_reply(chat_id, get_owner_text(), message_id)
        return

    # ---- anti-service ----
    if is_group and full_text and flag(get_group_settings(chat_id), "antiservice"):
        norm = re.sub(r"[\s\u200B_\-]+", " ", full_text).lower()
        promo = any(k in norm for k in SERVICE_KEYWORDS)
        if not promo and (re.search(r"(https?://|t\.me/|telegram\.me/)", full_text, re.I) or re.search(r"@[a-zA-Z0-9_]{5,}", full_text)):
            if re.search(r"\b(service|paid|buy|sell|deal|inbox|dm|cheap|crypto)\b", full_text, re.I):
                promo = True
        if promo and not admin_here():
            delete_message(chat_id, message_id)
            send_msg(chat_id, "🚫❌🔥 %s <b>Group me Service Promotions, Ads ya Spam links bhejna strictly allowed nahi hai! Message auto-delete kar diya gaya hai.</b> 🛡️✨" % user_mention(user_id, first_raw))
            return

    # ---- bad words ----
    if is_group and full_text and contains_bad_word(full_text) and not admin_here():
        delete_message(chat_id, message_id)
        send_msg(chat_id, "⚠️ %s <b>A gaya tu apni aukat par, teri yahi aukat thi 🥺😂🤣😯</b>" % user_mention(user_id, first_raw))
        return

    # ---- bio link protection ----
    if is_group and full_text and user_id > 0 and not is_anon and user_id not in SPECIAL_SENDER_IDS:
        settings = get_group_settings(chat_id)
        if flag(settings, "biolink") and not admin_here() and check_user_bio_for_links(user_id):
            delete_message(chat_id, message_id)
            if notify_once("biowarn:%d:%d" % (chat_id, user_id), 60):
                warn = settings.get("warn_text") or "⚠️🛡️ {user}, <b>aapke Bio me link paaya gaya hai!</b>\n\nGroup me message karne ke liye link hata lein. ❌"
                send_msg(chat_id, str(warn).replace("{user}", user_mention(user_id, first_raw)))
            return

    # ---- custom replies (group: only when addressed; private: always) ----
    if full_text and not cmd_key and (addressed or chat_type == "private"):
        reply_text = full_text
        if cx.bot_username:
            reply_text = re.sub(r"@" + re.escape(cx.bot_username) + r"\b", "", reply_text, flags=re.I)
        low = reply_text.strip().lower()
        replies = load_custom_replies(chat_id)
        found = replies.get(low)
        if not found:
            for trig, val in replies.items():
                if low.startswith(trig):
                    found = val
                    break
        if found:
            send_msg_reply(chat_id, h(found), message_id)
            return

    # ---- group custom reply commands ----
    if is_group and cmd_key in ("/setreply", "/delreply", "/replies"):
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admin ye command use kar sakte hain.", message_id)
            return
        handle_reply_commands(chat_id, cmd_key, cmd_args, message_id)
        return

    if is_group:
        if handle_group_commands(msg, chat_id, chat_title, user_id, first_raw, first, message_id, cmd_key, cmd_args, full_text, admin_here):
            return

    # ---- private: /start, /ping, /help, /id, /language ----
    if chat_type == "private":
        if handle_private_basic(msg, chat_id, user_id, first_raw, first, username, message_id, cmd_key, cmd_args, is_global_admin):
            return

    # ---- music ----
    if cmd_key == "/song":
        if not cmd_args:
            send_msg_reply(chat_id, "🎵 Song ka naam likho!\nJaise: <code>/song Tum Hi Ho</code> ✨", message_id)
            return
        send_typing(chat_id)
        send_msg_reply(chat_id, "🎵🎧 %s, <b>%s</b> dhundh rahi hoon... ⏳✨🚀" % (user_mention(user_id, first_raw), h(cmd_args)), message_id)
        search_and_send_song(chat_id, cmd_args, message_id, first_raw, user_id)
        return

    # ---- clone commands (private) ----
    if chat_type == "private":
        if handle_clone_commands(chat_id, user_id, cmd_key, cmd_args, full_text, message_id, is_global_admin):
            return
        if is_global_admin and handle_admin_commands(msg, chat_id, user_id, cmd_key, cmd_args, full_text, message_id):
            return
        if handle_private_ai(msg, chat_id, user_id, first_raw, username, cmd_key, full_text, message_id, is_global_admin):
            return
        return

    # ---- group AI trigger ----
    if is_group and addressed:
        send_typing(chat_id)
        if AI_REACTION_ON:
            set_reaction(chat_id, message_id, AI_REACTION_EMOJI)
        clean = full_text
        if cx.bot_username:
            clean = re.sub(r"@" + re.escape(cx.bot_username) + r"\b", "", clean, flags=re.I)
        clean = clean.strip()
        mention = user_mention(user_id, first_raw)
        if not clean:
            send_msg_reply(chat_id, mention + " 💕 Haan bolo, main sun rahi hoon 😊✨", message_id)
            return
        g = check_greeting(clean)
        if g:
            send_msg_reply(chat_id, mention + " " + g, message_id)
            save_memory(user_id, clean, g, chat_id)
            return
        lc = check_local(clean)
        if lc:
            send_msg_reply(chat_id, mention + " " + lc, message_id)
            save_memory(user_id, clean, lc, chat_id)
            return
        ai = query_ai(clean, user_id, chat_id)
        send_msg_reply(chat_id, mention + " 💕 " + h(ai.strip()), message_id)


def handle_new_members(msg, chat_id, chat_title, chat_username):
    cx = C()
    inviter_id = int((msg.get("from") or {}).get("id", 0))
    inviter_name = str((msg.get("from") or {}).get("first_name") or "Member")
    settings = get_group_settings(chat_id)
    custom = str(settings.get("welcome_msg") or "").strip()
    videos = settings.get("welcome_videos") or []
    if isinstance(videos, dict):
        videos = list(videos.values())
    if not isinstance(videos, list):
        videos = []
    if not videos and DEFAULT_WELCOME_VIDEO:
        videos = [DEFAULT_WELCOME_VIDEO]
    videos = [v for v in videos if isinstance(v, str) and v.strip()]
    for member in msg.get("new_chat_members") or []:
        m_id = int(member.get("id", 0))
        if m_id == cx.bot_id:
            send_language_selection_menu(chat_id)
            continue
        if member.get("is_bot"):
            continue
        m_name = str(member.get("first_name") or "Friend")
        m_un_raw = str(member.get("username") or "")
        m_un = "@" + m_un_raw if m_un_raw else "N/A"
        if m_id > 0:
            save_user(m_id, m_name, m_un, {"last_chat_id": chat_id, "db_schema": DB_SCHEMA_VERSION})
            track_membership(chat_id, m_id, "member", {"joined_at": now_str(), "actor_id": inviter_id, "title": chat_title, "username": chat_username})
            remember_event("group_join", chat_id, m_id, {"inviter_id": inviter_id, "inviter_name": inviter_name})
        if custom:
            mp = {"MENTION": user_mention(m_id, m_name), "USERNAME": h(m_un), "ID": str(m_id), "FIRST_NAME": h(m_name), "GROUP": h(chat_title),
                  "INVITER": user_mention(inviter_id, inviter_name), "INVITER_ID": str(inviter_id),
                  "user": user_mention(m_id, m_name), "group": h(chat_title), "inviter": user_mention(inviter_id, inviter_name), "inviter_id": str(inviter_id)}
            welcome = re.sub(r"\{(" + "|".join(mp.keys()) + r")\}", lambda mm: mp[mm.group(1)], custom)
        else:
            welcome = premium_welcome_text(m_id, m_name, m_un_raw, chat_id, chat_title, inviter_id, inviter_name)
        kb = welcome_keyboard(m_id)
        if videos:
            res = send_video(chat_id, random.choice(videos), welcome, kb)
            if not res.get("ok"):
                send_msg(chat_id, welcome, kb)
        else:
            send_msg(chat_id, welcome, kb)
        if WELCOME_DM_ON and m_id > 0:
            dm = send_safe_dm(m_id, premium_welcome_text(m_id, m_name, m_un_raw, 0, "Private Chat"), welcome_keyboard(m_id))
            if dm.get("ok"):
                remember_event("welcome_dm", chat_id, m_id)


def handle_reply_commands(chat_id, cmd_key, cmd_args, message_id):
    if cmd_key == "/setreply":
        parts = re.split(r"\s*\|\s*", cmd_args, maxsplit=1)
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            send_msg_reply(chat_id, "❌ Use: <code>/setreply trigger | reply</code>", message_id)
            return
        set_custom_reply(chat_id, parts[0], parts[1].strip())
        send_msg_reply(chat_id, "✅ Custom reply saved. Ab bot ko us trigger par reply karega.", message_id)
        return
    if cmd_key == "/delreply":
        if not cmd_args.strip():
            send_msg_reply(chat_id, "❌ Use: <code>/delreply trigger</code>", message_id)
            return
        del_custom_reply(chat_id, cmd_args)
        send_msg_reply(chat_id, "🗑️ Custom reply removed.", message_id)
        return
    r = load_custom_replies(chat_id)
    if not r:
        send_msg_reply(chat_id, "No custom replies saved.", message_id)
        return
    out = "🤖 <b>CUSTOM REPLIES</b>\n\n" + "".join("• <code>%s</code> → %s\n" % (h(k), h(v)) for k, v in r.items())
    send_msg_reply(chat_id, out, message_id)


TOGGLE_CMDS = {
    "/biolink": ("biolink", "🛡️🟢 <b>Bio-Link Protection ON!</b> ✨", "⚠️🔴 <b>Bio-Link Protection OFF!</b>",
                 "❓ Sahi tarika:\n<code>/biolink on</code> - Protection active\n<code>/biolink off</code> - Protection disable ✨"),
    "/antiservice": ("antiservice", "🛡️🔥 <b>Anti-Service Promotion Shield ON!</b> 🚀", "⚠️🔴 <b>Anti-Service Promotion Shield OFF!</b>",
                     "❓ Sahi tarika:\n<code>/antiservice on</code> - Service Auto-Delete ON\n<code>/antiservice off</code> - Shield OFF ✨"),
    "/stickerfilter": ("badsticker", "🛡🟢 <b>Bad Sticker Filter ON!</b> ✨", "⚠️🔴 <b>Bad Sticker Filter OFF!</b>",
                       "❓ Sahi tarika:\n<code>/stickerfilter on</code> - ON\n<code>/stickerfilter off</code> - OFF ✨"),
}


def handle_group_commands(msg, chat_id, chat_title, user_id, first_raw, first, message_id, cmd_key, cmd_args, full_text, admin_here):
    """Returns True if the message was handled."""
    cx = C()
    reply = msg.get("reply_to_message") or {}
    target_id = int((reply.get("from") or {}).get("id", 0))
    if not cmd_key:
        return False

    if cmd_key == "/start":
        send_msg_reply(chat_id, "Hii <a href=\"tg://user?id=%d\">%s</a>! Main <b>%s</b> active hoon group me! ✨🎉🥳\n\nCommands janne ke liye mujhe Private DM me <code>/start</code> bhejo ya <code>/help</code> likho! 💜🚀🔥" % (user_id, first, h(BOT_NAME)), message_id)
        return True

    if cmd_key == "/ping":
        t0 = time.time()
        pm = send_msg_reply(chat_id, "🏓✨ <i>Pinging...</i> 🚀", message_id)
        latency = round((time.time() - t0) * 1000)
        p_text = "🏓🔥 <b>PONG!</b> 🚀✨\n\n⚡ <b>Latency:</b> <code>%d ms</code>\n🤖 <b>Status:</b> Super Fast &amp; Ready! ✨💯🎉" % latency
        if pm.get("ok"):
            edit_message_text(chat_id, int(pm["result"]["message_id"]), p_text)
        else:
            send_msg_reply(chat_id, p_text, message_id)
        return True

    if cmd_key in ("/language", "/lang"):
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi language change kar sakte hain! 🚫", message_id)
        else:
            send_language_selection_menu(chat_id)
        return True

    if cmd_key in ("/record", "/recording", "/stoprecord"):
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins VC recording control kar sakte hain. 🚫", message_id)
            return True
        action = "stop" if cmd_key == "/stoprecord" else "start"
        hook = call_recording_hook(action, chat_id, chat_title, user_id, cx.bot_username)
        if not hook["configured"]:
            hint = "VC Recording - %s - %s.ogg" % (re.sub(r"[^\w._ -]+", "", chat_title, flags=re.U).strip() or "Telegram_Group", time.strftime("%Y-%m-%d %H-%M-%S"))
            if action == "start":
                t = ("🎙️ <b>Recording shortcut ready</b>\n\nActual VC audio recording ke liye MTProto recording worker connect karna zaroori hai. Bot API khud group-call audio stream record nahi karta.\n\n"
                     "📁 Suggested filename: <code>%s</code>\n\n⚙️ Worker connect karne ke baad <code>/record</code> start aur <code>/stoprecord</code> stop karega." % h(hint))
            else:
                t = ("🛑 <b>Recording stop shortcut ready</b>\n\nAbhi recording worker configured nahi hai, isliye koi fake recording/file nahi banayi gayi.\n\n"
                     "⚙️ <code>RECORDING_HOOK_URL</code> configure karne ke baad stop command worker ko signal bhejegi.")
            send_msg_reply(chat_id, t, message_id)
        elif hook["ok"]:
            if action == "start":
                set_group_setting(chat_id, "recording_active", True)
                send_msg_reply(chat_id, "🔴🎙️ <b>VC Recording Start Signal Sent!</b>\n\nRecording worker ab Voice Chat record karega. Stop ke liye <code>/stoprecord</code> use karein. 🚀", message_id)
            else:
                set_group_setting(chat_id, "recording_active", False)
                send_msg_reply(chat_id, "🛑🎙️ <b>VC Recording Stop Signal Sent!</b>\n\nWorker recording ko process karke configured destination par bhej sakta hai. 📁✨", message_id)
        else:
            send_msg_reply(chat_id, "❌ Recording worker response nahi de raha. Server/hook configuration check karein. 🚫", message_id)
        return True

    if cmd_key == "/vc":
        mention = user_mention(user_id, first_raw)
        note = "\n💬✨ <b>Note:</b> " + h(cmd_args) if cmd_args else ""
        info = tg("getChat", {"chat_id": chat_id})
        link = ""
        if info.get("ok") and (info.get("result") or {}).get("username"):
            link = "https://t.me/" + info["result"]["username"]
        else:
            inv = tg("exportChatInviteLink", {"chat_id": chat_id})
            if inv.get("ok") and inv.get("result"):
                link = str(inv["result"])
        if not link:
            send_msg_reply(chat_id, "❌ Group ka invite link nahi mil paya. Bot ko admin rights dekar dobara <code>/vc</code> try karein.\n\nℹ️ Telegram Bot API direct Voice Chat join-link generate nahi karta. 🚫", message_id)
            return True
        box = ("┌──────────────────────────────┐\n│ 🎙🎧🔥 <b>VOICE CHAT ALERT!</b> 🎙🎉 │\n└──────────────────────────────┘\n\n"
               "📢 <b>ATTENTION EVERYONE!</b> ✨🚀\n\nHey Friends! %s is calling everyone to join Voice Chat! 🎧✨🥳%s\n\n"
               "🚀🔥 <b>OPEN GROUP &amp; JOIN THE VOICE CHAT!</b> 🎉💯\n👇 <i>Button se group open karein; Voice Chat Telegram ke andar join karein.</i> ✨" % (mention, note))
        send_msg(chat_id, box, {"inline_keyboard": [[{"text": "🎙 Open Group 🚀✨", "url": link}]]})
        return True

    if cmd_key in ("/mute", "/unmute", "/ban", "/unban"):
        if not admin_here():
            return True
        if target_id <= 0:
            send_msg_reply(chat_id, "❌ Kisi user ke message par reply karke <code>%s</code> likhein! ✨" % cmd_key, message_id)
            return True
        if cmd_key == "/mute":
            r = restrict_member(chat_id, target_id, parse_duration_arg(cmd_args))
            ok_t, fail_t = "🔇🤫 <b>User muted successfully!</b> 🚫", "❌ Mute failed: "
        elif cmd_key == "/unmute":
            r = unrestrict_member(chat_id, target_id)
            ok_t, fail_t = "🔊🎉 <b>User unmuted successfully!</b> ✨", "❌ Unmute failed: "
        elif cmd_key == "/ban":
            r = ban_member(chat_id, target_id)
            ok_t, fail_t = "🚫🔥 <b>User banned successfully!</b> ❌", "❌ Ban failed: "
        else:
            r = unban_member(chat_id, target_id)
            ok_t, fail_t = "✅ <b>User unbanned.</b>", "❌ Unban failed: "
        send_msg_reply(chat_id, ok_t if r.get("ok") else fail_t + h(str(r.get("description") or "Telegram API error")), message_id)
        return True

    if cmd_key == "/addwelcomevideo":
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi Welcome Video add kar sakte hain! 🚫", message_id)
            return True
        file_id = None
        if (reply.get("video") or {}).get("file_id"):
            file_id = reply["video"]["file_id"]
        elif re.match(r"^https?://\S+$", cmd_args):
            file_id = cmd_args
        if file_id:
            videos = get_group_settings(chat_id).get("welcome_videos") or []
            if isinstance(videos, dict):
                videos = list(videos.values())
            if not isinstance(videos, list):
                videos = []
            if file_id not in videos:
                videos.append(file_id)
            set_group_setting(chat_id, "welcome_videos", videos)
            send_msg_reply(chat_id, "✅🎉 <b>Naya Welcome Video Add Ho Gaya!</b> Total Videos: <b>%d</b> 🚀✨" % len(videos), message_id)
        else:
            send_msg_reply(chat_id, "❌ Kisi video ka reply karke <code>/addwelcomevideo</code> likhein ya Direct URL pass karein!\nExample: <code>/addwelcomevideo https://catbox.moe/demo.mp4</code> ✨", message_id)
        return True

    if cmd_key == "/resetwelcomevideos":
        if admin_here():
            set_group_setting(chat_id, "welcome_videos", [DEFAULT_WELCOME_VIDEO] if DEFAULT_WELCOME_VIDEO else [])
            send_msg_reply(chat_id, "✅🔄 <b>Sabhi custom welcome videos clear kar diye gaye hain! (Default restored)</b> 🎉", message_id)
        return True

    if cmd_key == "/setwelcome":
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi Welcome message set kar sakte hain! 🚫", message_id)
            return True
        if not cmd_args:
            send_msg_reply(chat_id, "❌ Text add karein!\nExample: <code>/setwelcome Welcome {user} to {group}! 🌸✨</code>", message_id)
            return True
        set_group_setting(chat_id, "welcome_msg", cmd_args)
        send_msg_reply(chat_id, "✅🎉 <b>Custom Welcome Message Update Ho Gaya Hai!</b> ✨🚀", message_id)
        return True

    if cmd_key in ("/mset", "/set"):
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi trigger set kar sakte hain! 🚫", message_id)
            return True
        count = 0
        for pair in cmd_args.split("|"):
            if "=" in pair:
                trig, ans = pair.split("=", 1)
                if trig.strip() and ans.strip():
                    set_custom_reply(chat_id, trig, ans.strip())
                    count += 1
        if count:
            send_msg_reply(chat_id, "✅🎉 Successfully set <b>%d</b> custom replies! 🚀✨" % count, message_id)
        else:
            send_msg_reply(chat_id, "❓ Sahi tarika:\n<code>/mset trigger1 = reply1 | trigger2 = reply2</code> ✨", message_id)
        return True

    if cmd_key == "/id":
        t = "📍✨ <b>CHAT INFO</b> 🚀\n\n💬 <b>Group ID:</b> <code>%d</code>\n👤 <b>Your ID:</b> <code>%d</code>\n" % (chat_id, user_id)
        if target_id:
            t += "📌 <b>Replied User ID:</b> <code>%d</code> (%s)\n" % (target_id, h((reply.get("from") or {}).get("first_name", "User")))
        send_msg_reply(chat_id, t, message_id)
        return True

    if cmd_key == "/pin":
        if admin_here():
            if reply.get("message_id"):
                r = tg("pinChatMessage", {"chat_id": chat_id, "message_id": int(reply["message_id"])})
                send_msg_reply(chat_id, "📌🔥 <b>Message pinned!</b> 🎉" if r.get("ok") else "❌ Pin failed: " + h(str(r.get("description") or "Telegram API error")), message_id)
            else:
                send_msg_reply(chat_id, "❌ Message ka reply karke <code>/pin</code> likho! ✨", message_id)
        return True

    if cmd_key == "/unpin":
        if admin_here():
            args = {"chat_id": chat_id}
            if reply.get("message_id"):
                args["message_id"] = int(reply["message_id"])
            r = tg("unpinChatMessage", args)
            send_msg_reply(chat_id, "📌🔄 <b>Message unpinned!</b> ✨" if r.get("ok") else "❌ Unpin failed: " + h(str(r.get("description") or "Telegram API error")), message_id)
        return True

    if cmd_key == "/joinreq":
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi ye command use kar sakte hain! 🚫", message_id)
            return True
        arg = cmd_args.lower().strip()
        if arg == "on":
            set_group_setting(chat_id, "joinreq", True)
            send_msg_reply(chat_id, "✅🎉 <b>Auto Join Request Approval ON kar diya gaya hai!</b> 🚀", message_id)
        elif arg == "off":
            set_group_setting(chat_id, "joinreq", False)
            send_msg_reply(chat_id, "⚙️📋 <b>Manual Join-Request Mode ON.</b> Har pending request par sirf us group ke actual Admin/Owner Accept/Reject kar sakte hain. Telegram ki actual invite-link join-request setting yahan se globally off nahi hoti. ✨", message_id)
        else:
            send_msg_reply(chat_id, "❓ Sahi tarika:\n<code>/joinreq on</code> - Auto approve requests\n<code>/joinreq off</code> - Manual Accept/Reject mode ✨\n\n⚠️ Ye command Telegram ki invite-link join-request setting ko globally switch nahi karta; bot ka auto-approval mode control karta hai.", message_id)
        return True

    if cmd_key in TOGGLE_CMDS:
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi ye command use kar sakte hain! 🚫", message_id)
            return True
        key, on_t, off_t, usage = TOGGLE_CMDS[cmd_key]
        arg = cmd_args.lower().strip()
        if arg == "on":
            set_group_setting(chat_id, key, True)
            send_msg_reply(chat_id, on_t, message_id)
        elif arg == "off":
            set_group_setting(chat_id, key, False)
            send_msg_reply(chat_id, off_t, message_id)
        else:
            send_msg_reply(chat_id, usage, message_id)
        return True

    if cmd_key == "/setwarn":
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi ye command use kar sakte hain! 🚫", message_id)
            return True
        if not cmd_args:
            send_msg_reply(chat_id, "❌ Text add karein!\nExample: <code>/setwarn Hey {user}, please bio se link hatao!</code> ✨", message_id)
            return True
        set_group_setting(chat_id, "warn_text", cmd_args)
        send_msg_reply(chat_id, "✅🎉 <b>Custom Warning Message update ho gaya hai!</b> 🚀", message_id)
        return True

    if cmd_key == "/settings":
        if not admin_here():
            send_msg_reply(chat_id, "❌ Sirf Group Admins hi settings open kar sakte hain! 🚫", message_id)
        else:
            send_settings_menu(chat_id)
        return True

    if cmd_key == "/help":
        send_full_help_menu(chat_id)
        return True

    if cmd_key in ("/groupinfo", "/group"):
        if admin_here():
            send_group_info(chat_id, chat_id)
        return True

    if cmd_key == "/memory":
        send_memory(chat_id, user_id)
        return True

    if cmd_key == "/clearmemory":
        clear_memory(user_id, chat_id)
        send_msg_reply(chat_id, "🧹 Is chat ki aapki memory delete kar di gayi hai.", message_id)
        return True

    if cmd_key == "/song":
        return False
    return False


def send_memory(chat_id, user_id):
    mem = load_memory(user_id, chat_id)
    if not mem:
        send_msg(chat_id, "🧠 Aapki saved memory abhi empty hai.")
        return
    out = "🧠 <b>YOUR SAVED MEMORY</b>\n\n"
    for m in mem[-10:]:
        out += ("👤 " if m["role"] == "user" else "🤖 ") + h(m["content"][:300]) + "\n"
    send_msg(chat_id, out)


def send_group_info(reply_chat, target):
    g = dict(get_group_record(target))
    g.update(save_group_snapshot(target))
    link = ("https://t.me/" + str(g["username"]).lstrip("@")) if g.get("username") else ""
    if not link:
        inv = tg("exportChatInviteLink", {"chat_id": target})
        if inv.get("ok") and inv.get("result"):
            link = str(inv["result"])
    members = FB.read_dict("/memberships/%d" % target)
    local_m = [k for k in LOCAL.read("memberships") if str(k).startswith("%d_" % target)]
    mc = max(len(members), len(local_m))
    un = str(g.get("username") or "").lstrip("@")
    txt = ("👥🔥 <b>GROUP INFORMATION</b>\n\n🆔 ID: <code>%d</code>\n🏷 Name: <b>%s</b>\n🔗 Username: %s\n👤 Tracked Members: <b>%d</b>\n🖼 DP: %s\n"
           % (target, h(g.get("title") or "Unknown"), ('<a href="%s">@%s</a>' % (link, h(un))) if (link and un) else "N/A", mc, "Saved" if g.get("dp_file_id") else "Not available"))
    if link:
        txt += "\n🌐 Link: " + h(link)
    send_msg(reply_chat, txt)
    if g.get("dp_file_id"):
        tg("sendPhoto", {"chat_id": reply_chat, "photo": g["dp_file_id"], "caption": "🖼 Group DP"})


def get_group_record(gid):
    rec = LOCAL.get("groups", str(gid))
    if isinstance(rec, dict) and rec:
        return rec
    d = FB.read_value("/groups/%d" % gid)
    return d if isinstance(d, dict) else {}


def handle_private_basic(msg, chat_id, user_id, first_raw, first, username, message_id, cmd_key, cmd_args, is_global_admin):
    cx = C()
    if cmd_key == "/start":
        existing = get_user_record(user_id)
        is_new = not existing
        save_user(user_id, first_raw, username, force=True)
        if is_new and not is_global_admin:
            send_admin_notification("👤🔥 <b>New User Started The Bot!</b> 🎉✨\n\n• <b>Name:</b> %s\n• <b>Username:</b> %s\n• <b>User ID:</b> <code>%d</code>" % (first, h(username), user_id))
            remember_event("new_user", chat_id, user_id, {"name": first_raw})
        send_typing(chat_id)
        welcome = premium_welcome_text(user_id, first_raw, username.lstrip("@") if username != "N/A" else "", 0, "Private Chat")
        kb = welcome_keyboard(user_id)
        res = send_video(chat_id, DEFAULT_WELCOME_VIDEO, welcome, kb) if DEFAULT_WELCOME_VIDEO else send_msg(chat_id, welcome, kb)
        if not res.get("ok") and DEFAULT_WELCOME_VIDEO:
            send_msg(chat_id, welcome, kb)
        send_msg(chat_id, "👇 Neeche ke buttons se menu use karein ✨", admin_keyboard())
        return True

    if cmd_key == "/ping":
        t0 = time.time()
        pm = send_msg_reply(chat_id, "🏓✨ <i>Pinging...</i> 🚀", message_id)
        latency = round((time.time() - t0) * 1000)
        p_text = "🏓🔥 <b>PONG!</b> 🚀✨\n\n⚡ <b>Latency:</b> <code>%d ms</code>\n🤖 <b>Status:</b> Super Fast &amp; Ready! ✨💯🎉" % latency
        if pm.get("ok"):
            edit_message_text(chat_id, int(pm["result"]["message_id"]), p_text)
        else:
            send_msg_reply(chat_id, p_text, message_id)
        return True

    if cmd_key == "/help" or msg.get("text") == "❓ Help 💓":
        send_full_help_menu(chat_id)
        return True
    if cmd_key == "/id":
        send_msg_reply(chat_id, "📍✨ <b>CHAT INFO</b> 🚀\n\n👤 <b>Your ID:</b> <code>%d</code>\n💬 <b>Chat ID:</b> <code>%d</code>" % (user_id, chat_id), message_id)
        return True
    if cmd_key in ("/language", "/lang"):
        send_language_selection_menu(chat_id)
        return True
    if cmd_key == "/memory":
        send_memory(chat_id, user_id)
        return True
    if cmd_key == "/clearmemory":
        clear_memory(user_id, chat_id)
        send_msg_reply(chat_id, "🧹 Aapki saved memory delete kar di gayi hai.", message_id)
        return True
    if msg.get("text") == "👑 Owner 💓":
        send_msg(chat_id, get_owner_text(), {"inline_keyboard": [[{"text": "💓 " + OWNER_USERNAME, "url": OWNER_LINK}, {"text": "📢 Support 💓", "url": SUPPORT_LINK}]]})
        return True
    if msg.get("text") == "📊 Database Check 💓":
        if not is_global_admin:
            send_msg_reply(chat_id, "❌ Ye option sirf bot admins ke liye hai. 🚫", message_id)
        else:
            send_msg(chat_id, "📊 <b>DATABASE CHECK</b> 💓\n\n👤 Users: <b>%d</b>\n👥 Groups: <b>%d</b>\n🔥 Storage: Firebase + Local Backup\n📡 Broadcast: Union of both databases" % (len(get_all_users()), len(get_all_groups())))
        return True
    if msg.get("text") == "➕ Add To Your Group 💓":
        if not cx.bot_username:
            send_msg(chat_id, "❌ Bot username load nahi hua. Telegram API/getMe check karein. 🚫")
        else:
            send_msg(chat_id, "➕ Is link se group me add karo:\n%s ✨🚀" % h(bot_add_to_group_link()), admin_keyboard())
        return True
    return False


# ---------------- clone commands ----------------


def handle_clone_commands(chat_id, user_id, cmd_key, cmd_args, full_text, message_id, is_global_admin):
    cx = C()
    if cmd_key == "/clone" and cmd_args:
        cmd_key = "/makeclone"
    if cmd_key == "/makeclone":
        if CLONE_ADMIN_ONLY and not is_global_admin:
            send_msg_reply(chat_id, "❌ Clone banana abhi sirf bot admins ke liye enabled hai. 🚫", message_id)
            return True
        token = cmd_args.strip()
        if len(APP_KEY) < 32:
            send_msg_reply(chat_id, "❌ Clone system disabled: APP_KEY me kam se kam 32 random characters set karein. 🔐", message_id)
            return True
        if not token or not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{20,}", token):
            send_msg_reply(chat_id, "⚠️ <b>Usage:</b> <code>/makeclone &lt;BOT_TOKEN&gt;</code>\nExample: <code>/makeclone 123456789:ABCdefGhIJKlmNoPQ...</code> ✨", message_id)
            return True
        delete_message(chat_id, message_id)  # remove the raw token from the chat
        if not is_global_admin and len(get_user_clones(user_id)) >= CLONE_LIMIT:
            send_msg(chat_id, "❌ Clone limit (%d) reach ho gayi hai. Pehle koi clone delete karein. 🚫" % CLONE_LIMIT)
            return True
        send_typing(chat_id)
        chk = tg("getMe", {}, token=token)
        if not chk.get("ok"):
            send_msg(chat_id, "❌ <b>Invalid Token!</b> Please check your bot token and try again. 🚫")
            return True
        c_id = int(chk["result"]["id"])
        c_user = str(chk["result"].get("username", ""))
        c_name = str(chk["result"].get("first_name", ""))
        if c_id == cx.bot_id or c_id == get_bot_info(BOT_TOKEN)["id"]:
            send_msg(chat_id, "❌ Main bot ka token clone nahi kiya ja sakta. 🚫")
            return True
        existing = get_clone_record(str(c_id))
        if existing and int(existing.get("owner_id", 0)) not in (user_id,) and not is_global_admin:
            send_msg(chat_id, "❌ Ye bot already kisi aur ke clone ke roop me registered hai. 🚫")
            return True
        base = WEBHOOK_URL or LAST_BASE_URL.get("url", "")
        if not base:
            send_msg(chat_id, "❌ <b>WEBHOOK_URL configure nahi hai.</b> Exact public HTTPS URL hosting environment me set karein. 🚫")
            return True
        if not base.lower().startswith("https://"):
            send_msg(chat_id, "❌ Clone webhook ke liye HTTPS URL zaroori hai. WEBHOOK_URL set karein. 🔐")
            return True
        base = base.split("?")[0]
        hook_url = "%s?clone_id=%d" % (base, c_id)
        sw = tg("setWebhook", {"url": hook_url, "secret_token": clone_webhook_secret(c_id), "drop_pending_updates": False,
                               "allowed_updates": ["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member"]}, token=token)
        if not sw.get("ok"):
            send_msg(chat_id, "❌ Failed to link Webhook for Clone Bot: %s 🚫" % h(str(sw.get("description", ""))[:150]))
            return True
        try:
            token_enc = encrypt_clone_token(token)
        except Exception as e:
            log.error("Clone token encryption failed: %s", e)
            send_msg(chat_id, "❌ Clone secure storage setup failed. APP_KEY check karein. 🚫")
            return True
        created = now_str()
        owner_rec = {"bot_id": c_id, "bot_username": c_user, "bot_name": c_name, "created_at": created, "schema_version": DB_SCHEMA_VERSION}
        index_rec = {"owner_id": user_id, "bot_id": c_id, "bot_username": c_user, "token_enc": token_enc, "created_at": created, "schema_version": DB_SCHEMA_VERSION}
        LOCAL.patch("clones", str(user_id), {str(c_id): owner_rec})
        LOCAL.set("clone_index", str(c_id), index_rec)
        LOCAL.flush()
        if FB.base:
            ok1 = FB.write_sync("/clones/%d/%d" % (user_id, c_id), owner_rec)
            ok2 = FB.write_sync("/clone_index/%d" % c_id, index_rec)
            if not (ok1 and ok2):
                log.warning("Firebase clone save failed for %s; local backup retained.", c_id)
        send_msg(chat_id, "🎉🥳🚀🔥 <b>CLONE CREATED SUCCESSFULLY!</b> 💯✨\n\n🤖 <b>Bot Name:</b> %s\n🔗 <b>Bot Username:</b> @%s\n🆔 <b>Bot ID:</b> <code>%d</code>\n⚡ <b>Status:</b> Active &amp; Webhook Synced! ✨🔥\n\n🔒 Aapka token encrypted store hua hai aur chat se message delete kar diya gaya." % (h(c_name), h(c_user), c_id))
        send_admin_notification("🤖 <b>New Clone Created</b>\n\n👤 Owner ID: <code>%d</code>\n🤖 Bot: <b>%s</b>\n🔗 Username: @%s\n🆔 Bot ID: <code>%d</code>\n🔐 Token: securely encrypted in database; raw token is not forwarded." % (user_id, h(c_name), h(c_user), c_id))
        return True

    if cmd_key == "/myclones":
        clones = get_user_clones(user_id)
        clones = {k: v for k, v in clones.items() if isinstance(v, dict)}
        if not clones:
            send_msg_reply(chat_id, "❌ Aapke paas koi active clone bot nahi hai. 😅\nCreate karne ke liye: <code>/makeclone &lt;TOKEN&gt;</code> 🚀", message_id)
            return True
        out = "📊🔥 <b>YOUR ACTIVE CLONE BOTS:</b> 🚀✨\n──────────────────\n"
        for cid, cd in clones.items():
            out += "🤖 <b>%s</b> (@%s)\n🆔 <code>%s</code>\n──────────────────\n" % (h(cd.get("bot_name", "Bot")), h(cd.get("bot_username", "N/A")), h(cid))
        out += "❌ Delete karne ke liye: <code>/deleteclone &lt;BOT_ID&gt;</code> 🗑️"
        send_msg_reply(chat_id, out, message_id)
        return True

    if cmd_key == "/deleteclone":
        parts = cmd_args.split()
        target = parts[0] if parts else ""
        force = len(parts) > 1 and parts[1].lower() == "force"
        if not target:
            send_msg_reply(chat_id, "⚠️ <b>Usage:</b> <code>/deleteclone &lt;BOT_ID&gt;</code>", message_id)
            return True
        mine = get_user_clones(user_id)
        if target not in mine and not (is_global_admin and get_clone_record(target)):
            send_msg_reply(chat_id, "❌ Bot ID <code>%s</code> nahi mila ya ye aapka clone nahi hai. 🚫" % h(target), message_id)
            return True
        rec = get_clone_record(target)
        token = decrypt_clone_token(str(rec.get("token_enc", ""))) if rec else None
        ok = False
        if token:
            ok = bool(tg("deleteWebhook", {}, token=token).get("ok"))
        if not ok and not force:
            send_msg_reply(chat_id, "❌ Clone webhook remove nahi hua. Database record delete nahi kiya gaya, taaki clone recover kiya ja sake. ⚠️\n(Agar token invalid/lost hai to <code>/deleteclone %s force</code>)" % h(target), message_id)
            return True
        owner = int(rec.get("owner_id", user_id)) if rec else user_id
        LOCAL.patch("clones", str(owner), {})
        cl = dict(LOCAL.get("clones", str(owner)) or {})
        cl.pop(target, None)
        LOCAL.set("clones", str(owner), cl)
        LOCAL.delete("clone_index", target)
        FB.write("/clones/%d/%s" % (owner, target), None, "DELETE")
        FB.write("/clone_index/" + target, None, "DELETE")
        send_msg_reply(chat_id, "🗑✅ <b>Clone bot successfully removed!</b> 🎉", message_id)
        return True

    if cmd_key == "/syncclones" and is_global_admin:
        base = WEBHOOK_URL or LAST_BASE_URL.get("url", "")
        if not base.lower().startswith("https://"):
            send_msg_reply(chat_id, "❌ WEBHOOK_URL (HTTPS) set karein.", message_id)
            return True
        idx = merge_records(FB.read_dict("/clone_index"), LOCAL.read("clone_index"))
        ok = bad = 0
        for cid, rec in idx.items():
            tk = decrypt_clone_token(str(rec.get("token_enc", ""))) if isinstance(rec, dict) else None
            if not tk:
                bad += 1
                continue
            r = tg("setWebhook", {"url": "%s?clone_id=%s" % (base.split("?")[0], cid), "secret_token": clone_webhook_secret(int(cid)),
                                  "allowed_updates": ["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member"]}, token=tk)
            ok += 1 if r.get("ok") else 0
            bad += 0 if r.get("ok") else 1
        send_msg_reply(chat_id, "🔁 <b>Clone sync done</b>\n✅ OK: <b>%d</b>\n❌ Failed (token/APP_KEY issue): <b>%d</b>" % (ok, bad), message_id)
        return True
    return False


# ---------------- admin commands (private) ----------------


def handle_admin_commands(msg, chat_id, user_id, cmd_key, cmd_args, full_text, message_id):
    reply = msg.get("reply_to_message") or {}

    if cmd_key == "/resetdb":
        if cmd_args.strip().lower() != "confirm":
            send_msg(chat_id, "⚠️ Ye destructive command hai. Pura DB delete karne ke liye <code>/resetdb confirm</code> bhejo.")
            return True
        send_typing(chat_id)
        reset_firebase_db()
        send_msg_reply(chat_id, "🔥💥🚀 <b>Firebase Realtime Database Has Been Completely Reset!</b> All users, groups, clone data, and memory cleared. 💯✨", message_id)
        return True

    if cmd_key == "/admin":
        panel = ("⚙️👑🔥 <b>VIP ADMIN CONTROL PANEL</b> 🚀💯✨\n──────────────────\n"
                 "📢 <b>User Broadcast:</b> <code>/broadcast &lt;msg&gt;</code> (Supports Reply)\n"
                 "📢 <b>Group Broadcast:</b> <code>/gbroadcast &lt;msg&gt;</code> (Supports Reply)\n"
                 "👤 <b>Direct DM User:</b> <code>/user &lt;id&gt; &lt;msg&gt;</code>\n"
                 "🔎 <b>User Search:</b> <code>/users &lt;name&gt;</code>\n"
                 "👥 <b>Groups:</b> <code>/groups [name]</code> | <code>/addedlog</code>\n"
                 "🔁 <b>Retry Failed:</b> <code>/broadcast_retry</code>\n"
                 "🔥 <b>Reset Firebase DB:</b> <code>/resetdb confirm</code>\n──────────────────")
        send_msg_reply(chat_id, panel, message_id, admin_keyboard())
        return True

    if cmd_key == "/userinfo" or cmd_key == "/whois":
        target = int(cmd_args) if re.fullmatch(r"-?\d+", cmd_args.strip()) else 0
        if not target and (reply.get("from") or {}).get("id"):
            target = int(reply["from"]["id"])
        if not target:
            send_msg(chat_id, "❌ Use: <code>/userinfo USER_ID</code> ya user ke message par reply karke <code>/userinfo</code>")
            return True
        u = dict(get_user_record(target))
        u.update(save_user_profile_snapshot(target))
        groups = get_user_groups(target)
        lines = ["• %s <code>%s</code>" % (h(gd.get("title") or gid), h(gid)) for gid, gd in groups.items() if isinstance(gd, dict)]
        dp = "\n🖼 DP: <b>Saved</b>" if u.get("dp_file_id") else "\n🖼 DP: <b>Not available</b>"
        un = str(u.get("username") or "N/A").lstrip("@")
        txt = ("👤🔥 <b>USER INFORMATION</b>\n\n🆔 ID: <code>%d</code>\n👤 Name: <b>%s</b>\n🔗 Username: @%s%s\n🕒 Last Seen: <code>%s</code>\n\n👥 <b>Known Groups (%d):</b>\n%s"
               % (target, h(u.get("name") or "Unknown"), h(un), dp, h(u.get("last_seen") or "N/A"), len(groups), "\n".join(lines[:30]) if lines else "• No tracked groups"))
        send_msg(chat_id, txt)
        if u.get("dp_file_id"):
            tg("sendPhoto", {"chat_id": chat_id, "photo": u["dp_file_id"], "caption": "🖼 Saved profile DP"})
        return True

    if cmd_key == "/users":
        cmd_users_search(chat_id, cmd_args)
        return True
    if cmd_key == "/groups":
        cmd_groups_list(chat_id, cmd_args)
        return True
    if cmd_key == "/addedlog":
        cmd_added_log(chat_id)
        return True

    if cmd_key in ("/db", "/dbcheck", "/database"):
        send_msg(chat_id, "🗄️ <b>FIREBASE DATA</b>\n\n👤 Saved users: %d\n👥 Saved groups: %d\n\nUse <code>/userinfo USER_ID</code> or <code>/groupinfo GROUP_ID</code> for details." % (len(get_all_users()), len(get_all_groups())))
        return True

    if cmd_key in ("/groupinfo", "/group"):
        target = int(cmd_args) if re.fullmatch(r"-?\d+", cmd_args.strip()) else chat_id
        send_group_info(chat_id, target)
        return True

    if cmd_key == "/usergroups":
        target = int(cmd_args) if re.fullmatch(r"-?\d+", cmd_args.strip()) else int((reply.get("from") or {}).get("id", 0))
        if not target:
            send_msg(chat_id, "❌ Use: <code>/usergroups USER_ID</code>")
            return True
        out = "👥 <b>GROUPS FOR USER %d</b>\n\n" % target
        for gid, gd in get_user_groups(target).items():
            if isinstance(gd, dict):
                out += "• %s — <code>%s</code> — %s\n" % (h(gd.get("title") or gid), h(gid), h(gd.get("status") or "unknown"))
        send_msg(chat_id, out)
        return True

    if cmd_key in ("/setreply", "/delreply", "/replies"):
        handle_reply_commands(chat_id, cmd_key, cmd_args, message_id)
        return True

    if cmd_key == "/broadcast_retry":
        retry_broadcast_queue(chat_id)
        return True

    if cmd_key in ("/broadcast", "/gbroadcast"):
        kind = "user" if cmd_key == "/broadcast" else "group"
        src = int(reply["message_id"]) if reply.get("message_id") else None
        if not cmd_args and src is None:
            send_msg(chat_id, "❌ Message likho ya kisi message par reply karke <code>%s</code> bhejo! ✨" % cmd_key)
            return True
        if kind == "group":
            first_tok, _, rest = cmd_args.partition(" ")
            if re.fullmatch(r"-\d+", first_tok):
                gid = int(first_tok)
                if not rest.strip() and src is None:
                    send_msg(chat_id, "❌ Message bhi likho ✨")
                    return True
                r = broadcast_to_chat(gid, chat_id, src, "📢🔥 <b>Official Announcement from %s:</b>\n\n%s" % (h(OWNER_NAME), h(rest.strip())))
                if r.get("ok"):
                    send_msg(chat_id, "✅ Group %d ko message bhej diya gaya hai. 🎉" % gid)
                elif is_inactive_chat_error(r):
                    delete_group_from_db(str(gid))
                    send_msg(chat_id, "❌ Group %d inactive hai, isliye DB se remove kar diya. 🚫" % gid)
                else:
                    send_msg(chat_id, "❌ Group %d me message deliver nahi hua; database record safe rakha gaya hai. ⚠️" % gid)
                return True
        start_broadcast(chat_id, kind, cmd_args, src)
        return True

    if cmd_key == "/user":
        parts = full_text.split(None, 2)
        if len(parts) < 3 or not re.fullmatch(r"-?\d+", parts[1]):
            send_msg(chat_id, "❌ Sahi use:\n/user USER_ID message ✨")
            return True
        r = send_msg(int(parts[1]), "💬🔥 <b>Direct Message from %s:</b>\n\n%s" % (h(OWNER_NAME), h(parts[2])))
        send_msg(chat_id, "✅ User %s ko message gaya. 🎉" % h(parts[1]) if r.get("ok") else "❌ Message deliver nahi ho paya. 🚫")
        return True
    return False


def handle_private_ai(msg, chat_id, user_id, first_raw, username, cmd_key, full_text, message_id, is_global_admin):
    save_user(user_id, first_raw, username, {"last_chat_id": chat_id, "db_schema": DB_SCHEMA_VERSION})
    if cmd_key:
        send_msg_reply(chat_id, "❓ Ye command mujhe samajh nahi aaya. Full list ke liye <code>/help</code> bhejo. ✨", message_id)
        return True
    kb = admin_keyboard()
    g = check_greeting(full_text)
    if g:
        send_typing(chat_id)
        send_msg_reply(chat_id, g, message_id, kb)
        save_memory(user_id, full_text, g, chat_id)
        return True
    lc = check_local(full_text)
    if lc:
        send_typing(chat_id)
        send_msg_reply(chat_id, lc, message_id, kb)
        save_memory(user_id, full_text, lc, chat_id)
        return True
    send_typing(chat_id)
    ai = query_ai(full_text, user_id, chat_id)
    send_msg_reply(chat_id, h(ai), message_id, kb)
    return True


# ============================================================
# EXTERNAL RECORDING / VC CALLBACKS
# ============================================================


def handle_external_callback(is_vc, secret, body):
    expected = VC_EVENT_SECRET if is_vc else RECORDING_RESULT_SECRET
    if not expected or not hmac.compare_digest(expected, secret):
        return 403
    if not isinstance(body, dict):
        body = {}
    target = int(body.get("chat_id", 0) or 0)
    if not target:
        return 200
    set_ctx(MAIN_CTX())
    if is_vc:
        event = str(body.get("event", "")).lower()
        uid = int(body.get("user_id", 0) or 0)
        name = h(body.get("name", "Member"))
        if event in ("join", "joined", "start"):
            send_msg(target, "🎙️ <b>VC Join</b>\n\n👤 %s <code>%d</code> voice chat me aa gaya. 🎧🔥" % (name, uid))
        elif event in ("leave", "left", "stop"):
            send_msg(target, "🚪🎙️ <b>VC Leave</b>\n\n👤 %s <code>%d</code> voice chat se nikal gaya." % (name, uid))
    else:
        file_id = str(body.get("file_id", ""))
        file_url = str(body.get("file_url", ""))
        file_name = h(body.get("file_name", "VC Recording.ogg"))
        cap = "🎙️ <b>VC Recording Ready</b>\n\n📁 <code>%s</code>\n✨ Recording complete." % file_name
        if file_id or file_url:
            tg("sendAudio", {"chat_id": target, "audio": file_id or file_url, "caption": cap, "parse_mode": "HTML"})
        else:
            send_msg(target, cap + "\n\n⚠️ Worker ne file_id/file_url nahi diya.")
        set_group_setting(target, "recording_active", False)
    return 200


# ============================================================
# WEB SERVER (webhook) + POLLING
# ============================================================
app = Flask(__name__)
LAST_BASE_URL = {}
_main_ctx = None
_main_ctx_lock = threading.Lock()
MAIN_WEBHOOK_ACTIVE = {"on": False}


def MAIN_CTX():
    global _main_ctx
    with _main_ctx_lock:
        if _main_ctx is None or not _main_ctx.bot_id:
            _main_ctx = Ctx(BOT_TOKEN)
        return _main_ctx


@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "ARU BOT is running ✅", 200
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    LAST_BASE_URL["url"] = "%s://%s%s" % (proto, host, request.path.rstrip("/"))
    raw = request.get_data(cache=False) or b""

    # external callbacks (recording worker / VC events)
    if "recording_callback" in request.args or "vc_callback" in request.args:
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else dict(request.form)
        except Exception:
            body = dict(request.form)
        code = handle_external_callback("vc_callback" in request.args, request.args.get("secret", "").strip(), body)
        return jsonify({"ok": code == 200}), code

    clone_id = re.sub(r"[^0-9]", "", request.args.get("clone_id", ""))
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if clone_id:
        if not provided or not hmac.compare_digest(clone_webhook_secret(int(clone_id)), provided):
            return "", 403
        rec = get_clone_record(clone_id)
        token = decrypt_clone_token(str(rec.get("token_enc", ""))) if rec else None
        if not token:
            return "", 404
        ctx = Ctx(token, clone_id, rec)
    else:
        if MAIN_WEBHOOK_ACTIVE["on"] and (not provided or not hmac.compare_digest(MAIN_WEBHOOK_SECRET, provided)):
            return "", 403
        ctx = MAIN_CTX()

    try:
        update = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"ok": True})
    if not isinstance(update, dict):
        return jsonify({"ok": True})
    submit_update(ctx, update)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})


def submit_update(ctx, update):
    uid = update.get("update_id")
    if uid is not None and is_duplicate_update(ctx.token[:16], int(uid)):
        return
    EXEC.submit(run_in_ctx, ctx, process_update, update)


ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member"]


def poll_loop():
    ctx = MAIN_CTX()
    offset = int(LOCAL.get("meta", "poll_offset", 0) or 0)
    log.info("Long polling started for @%s", ctx.bot_username)
    while True:
        try:
            r = SESSION.post("https://api.telegram.org/bot%s/getUpdates" % BOT_TOKEN,
                             json={"offset": offset, "timeout": 50, "allowed_updates": ALLOWED_UPDATES}, timeout=(5, 65))
            j = r.json()
            if not j.get("ok"):
                log.warning("getUpdates failed: %s", j.get("description"))
                time.sleep(3)
                continue
            for upd in j.get("result", []):
                offset = int(upd["update_id"]) + 1
                submit_update(ctx, upd)
            if j.get("result"):
                LOCAL.set("meta", "poll_offset", offset)
        except requests.RequestException:
            time.sleep(3)
        except Exception:
            log.error("poll error:\n%s", traceback.format_exc())
            time.sleep(3)


def setup_main_bot():
    ctx = MAIN_CTX()
    if not ctx.bot_id:
        log.error("Cannot reach Telegram with BOT_TOKEN (getMe failed). Check the token / network.")
    set_ctx(ctx)
    if WEBHOOK_URL:
        r = tg("setWebhook", {"url": WEBHOOK_URL, "secret_token": MAIN_WEBHOOK_SECRET, "allowed_updates": ALLOWED_UPDATES,
                              "drop_pending_updates": False}, token=BOT_TOKEN)
        MAIN_WEBHOOK_ACTIVE["on"] = bool(r.get("ok"))
        LAST_BASE_URL["url"] = WEBHOOK_URL.split("?")[0]
        log.info("Webhook set: %s -> %s", WEBHOOK_URL, r.get("ok") or r.get("description"))
        return False
    tg("deleteWebhook", {"drop_pending_updates": False}, token=BOT_TOKEN)
    return True


def main():
    if not BOT_TOKEN or BOT_TOKEN in ("PASTE_YOUR_BOT_TOKEN_HERE", "PASTE_BOT_TOKEN_HERE"):
        print("FATAL: BOT_TOKEN missing. Set BOT_TOKEN in your hosting environment.")
        sys.exit(1)
    polling = setup_main_bot()
    threading.Thread(target=deletion_loop, daemon=True).start()
    if polling:
        if DISABLE_WEB:
            poll_loop()
            return
        threading.Thread(target=poll_loop, daemon=True).start()
    log.info("Web server on port %d (webhook mode: %s)", PORT, not polling)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=max(8, WORKERS))
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
