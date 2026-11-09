import asyncio, base64, hashlib, json, os, re, secrets, time, uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

RT = "https://web.kick.com/api/v1/realtime"
ID = "https://id.kick.com"
API = "https://api.kick.com/public/v1"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
HERE = Path(__file__).parent
STATIC = HERE / "static"
BADGES_DIR = HERE / "badges"
BADGES_DIR.mkdir(exist_ok=True)
BADGES_SRC = "https://cpwemotes.co.uk/kick/kickBadges/"
BADGE_NAMES = ["broadcaster", "moderator", "vip", "og", "founder", "verified",
               "staff", "subGifter", "sidekick", "subscriber"]

HISTORY_MAX = 300          # mensajes que se recuerdan por canal
HUB_IDLE_SECONDS = 600     # un canal sin nadie conectado se cierra pasado este tiempo
MAX_CHANNELS = 15          # canales por usuario
SESSION_DAYS = 90
COOKIE = "kc_sid"

KEEP = ("ChatMessageEvent", "MessageDeletedEvent", "UserBannedEvent", "UserUnbannedEvent",
        "PinnedMessageCreatedEvent", "PinnedMessageDeletedEvent")
SEEN = set()


def load_env():
    try:
        for line in (HERE / ".env").read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_env()
PORT = int(os.getenv("PORT") or 8765)
HOST = os.getenv("HOST") or "127.0.0.1"
REDIRECT = os.getenv("KICK_REDIRECT_URI") or f"http://localhost:{PORT}/callback"


def load(name, default):
    try:
        return json.loads((HERE / name).read_text("utf-8"))
    except Exception:
        return default


def save(name, value, private=False):
    p = HERE / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
    os.replace(tmp, p)


CFG = load("config.json", {})
CID = CFG.get("client_id") or os.getenv("KICK_CLIENT_ID")
SEC = CFG.get("client_secret") or os.getenv("KICK_CLIENT_SECRET")


def norm(n):
    """Kick trata 'a_b' y 'a-b' como el mismo usuario."""
    return str(n or "").strip().lower().replace("_", "-")


# Dueños de la app (usuarios de Kick separados por coma): pueden crear y asignar insignias.
ADMINS = {norm(x) for x in (os.getenv("ADMIN_USERS") or "").split(",") if x.strip()}
CB_DIR = HERE / "cbadges"
CB_DIR.mkdir(exist_ok=True)
CB = load("custom_badges.json", {})
if not isinstance(CB, dict):
    CB = {}
if not isinstance(CB.get("badges"), dict):
    CB["badges"] = {}
if not isinstance(CB.get("users"), dict):
    CB["users"] = {}


def is_admin(sess):
    return bool(sess.get("name")) and norm(sess.get("name")) in ADMINS

sessions = load("sessions.json", {})
if not isinstance(sessions, dict):
    sessions = {}
sessions_dirty = False
history = load("history.json", {})
if not isinstance(history, dict):
    history = {}
history_dirty = False
pending = {}   # state -> (verifier, sid, tiempo)
hubs = {}


def touch():
    global sessions_dirty
    sessions_dirty = True


def save_sessions(force=False):
    global sessions_dirty
    if not (sessions_dirty or force):
        return
    sessions_dirty = False
    try:
        save("sessions.json", sessions, private=True)
    except Exception as e:
        print("no se pudo guardar sesiones:", e)


def purge_sessions():
    limit = time.time() - SESSION_DAYS * 86400
    for sid in [k for k, v in sessions.items() if (v.get("seen") or 0) < limit]:
        sessions.pop(sid, None)
        touch()


# ---------------- sesión por usuario (cookie) ----------------
@web.middleware
async def session_mw(req, handler):
    sid = req.cookies.get(COOKIE, "")
    new = sid not in sessions
    if new:
        sid = secrets.token_urlsafe(32)
        sessions[sid] = {"channels": [], "tokens": {}, "name": None, "seen": time.time()}
        touch()
    sess = sessions[sid]
    if time.time() - (sess.get("seen") or 0) > 3600:
        sess["seen"] = time.time()
        touch()
    req["sid"], req["sess"] = sid, sess

    def put_cookie(resp):
        secure = req.headers.get("X-Forwarded-Proto", req.scheme) == "https"
        resp.set_cookie(COOKIE, sid, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="Lax", secure=secure, path="/")

    try:
        resp = await handler(req)
    except web.HTTPException as ex:
        if new:
            put_cookie(ex)
        raise
    if new and not resp.prepared:
        put_cookie(resp)
    return resp


# ---------------- utilidades de red ----------------
async def ensure_badges(s):
    for n in BADGE_NAMES:
        f = BADGES_DIR / f"{n}.svg"
        if f.exists():
            continue
        try:
            async with s.get(f"{BADGES_SRC}{n}.svg", headers=UA) as r:
                if r.status == 200:
                    f.write_bytes(await r.read())
                    print("badge descargado:", n)
                else:
                    print("badge no disponible:", n, r.status)
        except Exception as e:
            print("badge falló:", n, e)


async def oauth_post(s, data):
    async with s.post(f"{ID}/oauth/token", data=data) as r:
        return await r.json(content_type=None)


def store_tokens(sess, j):
    t = sess.setdefault("tokens", {})
    t.update(access_token=j["access_token"],
             refresh_token=j.get("refresh_token", t.get("refresh_token")),
             expires_at=time.time() + j.get("expires_in", 3600))
    touch()


async def user_token(s, sess):
    t = sess.get("tokens") or {}
    if not t.get("access_token"):
        return None
    if time.time() > t.get("expires_at", 0) - 60:
        try:
            j = await oauth_post(s, {"grant_type": "refresh_token", "client_id": CID,
                                     "client_secret": SEC, "refresh_token": t.get("refresh_token", "")})
        except Exception as e:
            print("refresh falló:", e)
            return None
        if "access_token" not in j:
            sess["tokens"] = {}
            sess["name"] = None
            touch()
            return None
        store_tokens(sess, j)
    return sess["tokens"]["access_token"]


async def jget(s, url):
    try:
        async with s.get(url, headers=UA) as r:
            if r.status == 200:
                return await r.json(content_type=None)
            print("jget", r.status, url)
    except Exception as e:
        print("jget falló:", url, type(e).__name__, e)
    return None


async def v2(s, slug):
    return await jget(s, f"https://kick.com/api/v2/channels/{slug}")


async def official_channel(s, sess, slug):
    tok = await user_token(s, sess)
    if not tok:
        return None
    try:
        async with s.get(f"{API}/channels", params={"slug": slug},
                         headers={"Authorization": f"Bearer {tok}"}) as r:
            if r.status == 200:
                d = (await r.json(content_type=None)).get("data") or []
                return d[0] if d else None
    except Exception as e:
        print("official_channel falló:", e)
    return None


# ---------------- historial + conexión a Kick (una por canal, compartida) ----------------
def save_history(force=False):
    global history_dirty
    if not (history_dirty or force):
        return
    history_dirty = False
    try:
        for room, h in hubs.items():
            history[room] = list(h.buf)
        while len(history) > 200:
            history.pop(next(iter(history)))
        save("history.json", history)
    except Exception as e:
        print("no se pudo guardar el historial:", e)


async def housekeeping():
    n = 0
    while True:
        await asyncio.sleep(30)
        n += 1
        save_history()
        save_sessions()
        now = time.time()
        for room, hub in list(hubs.items()):
            if not hub.clients and now - hub.last_active > HUB_IDLE_SECONDS:
                history[room] = list(hub.buf)
                hub.stop()
                hubs.pop(room, None)
                globals()["history_dirty"] = True
        for k in [k for k, v in pending.items() if now - v[2] > 600]:
            pending.pop(k, None)
        if n % 2880 == 0:  # ~una vez al día
            purge_sessions()


class Hub:
    def __init__(self, s, room, slug=None, channel_id=None):
        self.s = s
        self.room = str(room)
        self.slug = slug
        self.channel_id = channel_id
        self.clients = set()
        self.state = "wait"
        self.last_active = time.time()
        saved = history.get(self.room, [])
        self.buf = deque([e for e in saved if isinstance(e, dict) and "event" in e],
                         maxlen=HISTORY_MAX)
        self.task = asyncio.create_task(self.run())
        self.pre = asyncio.create_task(self.prefill())

    def stop(self):
        self.task.cancel()
        self.pre.cancel()
        for ws in list(self.clients):
            asyncio.create_task(ws.close())

    async def broadcast(self, payload):
        if not self.clients:
            return

        async def one(ws):
            try:
                await ws.send_json(payload)
            except Exception:
                self.clients.discard(ws)

        await asyncio.gather(*(one(ws) for ws in list(self.clients)))

    async def publish(self, event, data):
        global history_dirty
        name = event.replace("App\\Events\\", "")
        if name not in SEEN:
            SEEN.add(name)
            print("evento de Kick:", name)
        ts = datetime.now(timezone.utc).isoformat()
        if name in KEEP or "Prediction" in name:
            self.buf.append({"event": event, "data": data, "ts": ts})
            history_dirty = True
        await self.broadcast({"event": event, "data": data, "ts": ts})

    async def set_state(self, st):
        self.state = st
        await self.broadcast({"event": "relay:status", "data": {"state": st}})

    def merge(self, extra):
        chats, others = {}, []
        for e in self.buf:
            if str(e["event"]).endswith("ChatMessageEvent"):
                chats[str((e.get("data") or {}).get("id"))] = e
            else:
                others.append(e)
        for m in extra:
            chats.setdefault(str(m.get("id")), {"event": "App\\Events\\ChatMessageEvent", "data": m})
        ordered = sorted(chats.values(), key=lambda e: (e.get("data") or {}).get("created_at") or "")
        self.buf = deque(ordered + others, maxlen=HISTORY_MAX)

    async def prefill(self):
        global history_dirty
        try:
            cid = self.channel_id
            if not cid and self.slug:
                info = await v2(self.s, self.slug)
                cid = info.get("id") if info else None
                self.channel_id = cid
            if not cid:
                return
            j = await jget(self.s, f"https://kick.com/api/v2/channels/{cid}/messages")
            msgs = (((j or {}).get("data") or {}).get("messages")) or []
            msgs = [m for m in msgs if isinstance(m, dict) and m.get("id") is not None]
            if not msgs:
                return
            self.merge(msgs)
            history_dirty = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("prefill falló:", e)

    async def pump(self):
        s = self.s
        cid = str(uuid.uuid4())
        body = {"client": {"id": cid, "type": "web"},
                "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]}}
        async with s.post(f"{RT}/connection", json=body, headers=UA) as r:
            conns = (await r.json(content_type=None))["data"]["connections"]
        url = next(c["credentials"]["url"] for c in conns if c["provider"] == "centrifugo")
        async with s.post(f"{RT}/auth/connection", json={"client_id": cid}, headers=UA) as r:
            token = (await r.json(content_type=None))["data"]["token"]
        refresher = None
        async with s.ws_connect(url, heartbeat=None) as up:
            await up.send_str(json.dumps({"connect": {"token": token, "name": "js"}, "id": 1}))
            try:
                async for m in up:
                    if m.type != WSMsgType.TEXT:
                        continue
                    for line in m.data.split("\n"):
                        if not line.strip():
                            continue
                        msg = json.loads(line)
                        if msg == {}:
                            await up.send_str("{}")
                        elif msg.get("id") == 1 and "connect" in msg:
                            refresher = asyncio.create_task(
                                refresh_loop(s, up, cid, msg["connect"].get("ttl", 2700)))
                            await up.send_str(json.dumps(
                                {"subscribe": {"channel": f"chatrooms.{self.room}.v2"}, "id": 2}))
                            if self.channel_id:
                                await up.send_str(json.dumps(
                                    {"subscribe": {"channel": f"channel.{self.channel_id}"}, "id": 3}))
                        elif msg.get("id") == 2 and "subscribe" in msg:
                            await self.set_state("on")
                        elif "push" in msg:
                            pub = msg["push"].get("pub", {}).get("data", {})
                            data = pub.get("data", "{}")
                            if isinstance(data, str):
                                data = json.loads(data)
                            await self.publish(pub.get("event", ""), data)
            finally:
                if refresher:
                    refresher.cancel()

    async def run(self):
        while True:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("conexión con Kick perdida:", e)
            await self.set_state("wait")
            await asyncio.sleep(5)


async def refresh_loop(s, up, cid, ttl):
    n = 10
    while True:
        await asyncio.sleep(max(ttl - 120, 60))
        async with s.post(f"{RT}/auth/connection", json={"client_id": cid}, headers=UA) as r:
            tok = (await r.json(content_type=None))["data"]["token"]
        n += 1
        await up.send_str(json.dumps({"refresh": {"token": tok}, "id": n}))


def get_hub(s, room, slug=None, channel_id=None):
    room = str(room)
    hub = hubs.get(room)
    if hub is None:
        hub = hubs[room] = Hub(s, room, slug, channel_id)
    return hub


def find_channel(sess, slug):
    return next((c for c in sess["channels"] if c["slug"] == slug), None)


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    room = req.query.get("room", "")
    ch = next((c for c in req["sess"]["channels"] if str(c.get("chatroom_id")) == room), None)
    if not room.isdigit() or ch is None:
        await ws.close()
        return ws
    hub = get_hub(req.app["s"], room, ch.get("slug"), ch.get("channel_id"))
    items = list(hub.buf)
    hub.clients.add(ws)
    hub.last_active = time.time()
    try:
        await ws.send_json({"event": "relay:history", "data": {"items": items}})
        await ws.send_json({"event": "relay:status", "data": {"state": hub.state}})
        async for _ in ws:
            pass
    finally:
        hub.clients.discard(ws)
        hub.last_active = time.time()
    return ws


# ---------------- API ----------------
SLUG_RE = re.compile(r"^[a-z0-9_\-]{1,40}$")


async def api_state(req):
    sess = req["sess"]
    return web.json_response({"configured": bool(CID and SEC), "user": sess.get("name"),
                              "admin": is_admin(sess), "channels": sess["channels"]})


async def api_add(req):
    s, sess = req.app["s"], req["sess"]
    slug = req.query.get("slug", "").strip().lower().lstrip("@")
    room = req.query.get("room", "").strip()
    if not SLUG_RE.match(slug):
        return web.json_response({"error": "Nombre de canal inválido"}, status=400)
    if len(sess["channels"]) >= MAX_CHANNELS and not find_channel(sess, slug):
        return web.json_response({"error": f"Máximo {MAX_CHANNELS} canales"}, status=400)
    info = await v2(s, slug)
    ch = {"slug": slug, "chatroom_id": None, "user_id": None}
    if info and (info.get("chatroom") or {}).get("id"):
        ch["chatroom_id"] = info["chatroom"]["id"]
        ch["user_id"] = info.get("user_id")
        ch["channel_id"] = info.get("id")
    elif room.isdigit():
        ch["chatroom_id"] = int(room)
    else:
        return web.json_response({"error": "need_room"}, status=422)
    sess["channels"] = [c for c in sess["channels"] if c["slug"] != slug] + [ch]
    touch()
    return web.json_response(ch)


async def api_remove(req):
    sess = req["sess"]
    slug = req.query.get("slug", "")
    sess["channels"] = [c for c in sess["channels"] if c["slug"] != slug]
    touch()
    return web.json_response({"ok": True})


async def api_live(req):
    s, sess = req.app["s"], req["sess"]
    slug = req.query.get("slug", "")
    if not SLUG_RE.match(slug):
        return web.json_response({"live": None})
    info = await v2(s, slug)
    if info is not None:
        ls = info.get("livestream")
        return web.json_response({"live": ls is not None,
                                  "viewers": (ls or {}).get("viewer_count")})
    ch = await official_channel(s, sess, slug)
    if ch is not None:
        return web.json_response({"live": bool((ch.get("stream") or {}).get("is_live"))})
    return web.json_response({"live": None})


emote_cache = {}
tv_global_cache = [0, {}]


def seventv_map(emotes):
    """Acepta urls con '//', 'https://' o 'http://' y tolera datos faltantes."""
    out = {}
    for e in emotes or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        host = (e.get("data") or {}).get("host") or {}
        url = host.get("url")
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        out[e["name"]] = url.rstrip("/") + "/2x.webp"
    return out


async def seventv_channel(s, user_id):
    for plat in ("KICK", "kick"):
        j = await jget(s, f"https://7tv.io/v3/users/{plat}/{user_id}")
        if isinstance(j, dict):
            return j
    return {}


async def api_emotes(req):
    s, sess = req.app["s"], req["sess"]
    slug = req.query.get("slug", "")
    if not SLUG_RE.match(slug):
        return web.json_response({"sections": [], "seventv": {}, "sub_badges": []})
    hit = emote_cache.get(slug)
    if hit and time.time() - hit[0] < 600:
        return web.json_response(hit[1])
    ch = find_channel(sess, slug) or {}
    info = await v2(s, slug) or {}
    user_id = ch.get("user_id") or info.get("user_id")

    sets = await jget(s, f"https://kick.com/emotes/{slug}")
    if not isinstance(sets, list):
        sets = []
    kick = {"canal": [], "subs": [], "global": [], "emoji": []}
    for st in sets:
        set_name = (st.get("name") or "").lower()
        for e in st.get("emotes", []):
            item = {"n": e["name"], "u": f"https://files.kick.com/emotes/{e['id']}/fullsize",
                    "t": f"[emote:{e['id']}:{e['name']}]", "id": e["id"], "k": True}
            if set_name == "global":
                kick["global"].append(item)
            elif set_name == "emoji":
                kick["emoji"].append(item)
            elif e.get("subscribers_only"):
                kick["subs"].append(item)
            else:
                kick["canal"].append(item)

    if time.time() - tv_global_cache[0] < 3600 and tv_global_cache[1]:
        tv_global = tv_global_cache[1]
    else:
        glob = await jget(s, "https://7tv.io/v3/emote-sets/global") or {}
        tv_global = seventv_map(glob.get("emotes"))
        if tv_global:
            tv_global_cache[0], tv_global_cache[1] = time.time(), tv_global
    tv_chan = {}
    if user_id:
        chan = await seventv_channel(s, user_id)
        tv_chan = seventv_map((chan.get("emote_set") or {}).get("emotes"))
    print(f"emotes {slug}: kick={sum(len(v) for v in kick.values())} 7tv_global={len(tv_global)} 7tv_canal={len(tv_chan)}")

    def tv_items(m):
        return [{"n": n, "u": u, "t": n} for n, u in m.items()]

    sections = [
        {"key": "canal", "title": "Kick · Emotes del canal", "items": kick["canal"]},
        {"key": "subs", "title": "Kick · Emotes de suscriptores", "items": kick["subs"]},
        {"key": "kick_global", "title": "Kick · Globales", "items": kick["global"]},
        {"key": "emoji", "title": "Kick · Emojis", "items": kick["emoji"]},
        {"key": "tv_canal", "title": "7TV · Emotes del canal", "items": tv_items(tv_chan)},
        {"key": "tv_global", "title": "7TV · Globales", "items": tv_items(tv_global)},
    ]
    sub_badges = [{"m": b.get("months", 0), "u": (b.get("badge_image") or {}).get("src")}
                  for b in info.get("subscriber_badges", [])
                  if (b.get("badge_image") or {}).get("src")]
    data = {"sections": sections, "seventv": {**tv_global, **tv_chan}, "sub_badges": sub_badges}
    # solo se guarda en caché si llegó todo; si no, se reintenta en la próxima petición
    if sets and tv_global:
        emote_cache[slug] = (time.time(), data)
    return web.json_response(data)


async def api_send(req):
    s, sess = req.app["s"], req["sess"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "Petición inválida"}, status=400)
    slug, text = str(body.get("slug", "")), str(body.get("content", "")).strip()
    tok = await user_token(s, sess)
    if not tok:
        return web.json_response({"error": "Inicia sesión para enviar mensajes"}, status=401)
    ch = find_channel(sess, slug)
    if ch is None or not text or len(text) > 500:
        return web.json_response({"error": "Canal o mensaje inválido"}, status=400)
    if time.time() - sess.get("last_send", 0) < 0.8:
        return web.json_response({"error": "Vas muy rápido"}, status=429)
    sess["last_send"] = time.time()
    if not ch.get("user_id"):
        off = await official_channel(s, sess, slug)
        if off:
            ch["user_id"] = off.get("broadcaster_user_id")
            touch()
    if not ch.get("user_id"):
        return web.json_response({"error": "No se pudo obtener el ID del streamer"}, status=502)
    try:
        async with s.post(f"{API}/chat", json={"broadcaster_user_id": ch["user_id"],
                                               "content": text, "type": "user"},
                          headers={"Authorization": f"Bearer {tok}"}) as r:
            ok = r.status == 200
            return web.json_response({"ok": ok} if ok else {"error": f"Kick respondió {r.status}"},
                                     status=200 if ok else 502)
    except Exception as e:
        return web.json_response({"error": f"No se pudo contactar a Kick: {type(e).__name__}"}, status=502)


async def auth_login(req):
    if not (CID and SEC):
        return web.Response(text="Falta config.json o .env con client_id y client_secret", status=500)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    pending[state] = (verifier, req["sid"], time.time())
    q = urlencode({"client_id": CID, "response_type": "code", "redirect_uri": REDIRECT,
                   "scope": "user:read channel:read chat:write", "code_challenge": challenge,
                   "code_challenge_method": "S256", "state": state})
    raise web.HTTPFound(f"{ID}/oauth/authorize?{q}")


async def auth_callback(req):
    s, sess = req.app["s"], req["sess"]
    item = pending.pop(req.query.get("state", ""), None)
    if not item or item[1] != req["sid"] or "code" not in req.query:
        return web.Response(text="Inicio de sesión cancelado o inválido. Vuelve a intentarlo.", status=400)
    j = await oauth_post(s, {"grant_type": "authorization_code", "client_id": CID, "client_secret": SEC,
                             "redirect_uri": REDIRECT, "code_verifier": item[0], "code": req.query["code"]})
    if "access_token" not in j:
        print("login rechazado:", j)
        return web.Response(text="Kick rechazó el inicio de sesión. Vuelve a intentarlo.", status=400)
    store_tokens(sess, j)
    try:
        async with s.get(f"{API}/users", headers={"Authorization": f"Bearer {j['access_token']}"}) as r:
            d = (await r.json(content_type=None)).get("data") or [{}]
            sess["name"] = d[0].get("name", "usuario")
    except Exception:
        sess["name"] = "usuario"
    touch()
    raise web.HTTPFound("/")


async def auth_logout(req):
    sess = req["sess"]
    sess["tokens"] = {}
    sess["name"] = None
    touch()
    raise web.HTTPFound("/")


# ---------------- insignias personalizadas (solo se ven en esta app) ----------------
USER_RE = re.compile(r"^[a-z0-9\-]{1,40}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def deny():
    return web.json_response({"error": "No autorizado"}, status=403)


def bad(msg):
    return web.json_response({"error": msg}, status=400)


def save_cb():
    save("custom_badges.json", CB)


def sniff_ext(b):
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if b[:4] == b"GIF8":
        return "gif"
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return None


def drop_image(url):
    if url and url.startswith("/cbadges/"):
        try:
            (CB_DIR / url.split("/")[-1]).unlink()
        except Exception:
            pass


async def api_badges(req):
    return web.json_response({"badges": CB["badges"], "users": CB["users"]},
                             headers=NOCACHE)


async def admin_badge_save(req):
    if not is_admin(req["sess"]):
        return deny()
    form = await req.post()
    name = str(form.get("name", "")).strip()[:30]
    label = str(form.get("label", "")).strip()[:4]
    color = str(form.get("color", "#6ee7a8")).strip()
    if not COLOR_RE.match(color):
        color = "#6ee7a8"
    if not name:
        return bad("Falta el nombre de la insignia")
    bid = str(form.get("id", "")).strip()
    cur = CB["badges"].get(bid)
    if cur is None:
        if len(CB["badges"]) >= 100:
            return bad("Máximo 100 insignias")
        bid = secrets.token_hex(4)
        cur = {"n": "", "l": "", "c": color, "u": ""}
    url = cur.get("u", "")
    f = form.get("image")
    if isinstance(f, web.FileField) and f.filename:
        data = f.file.read(150001)
        if len(data) > 150000:
            return bad("La imagen pesa más de 150 KB")
        ext = sniff_ext(data)
        if not ext:
            return bad("Formato no válido (usa PNG, JPG, GIF o WEBP)")
        fname = f"{bid}-{secrets.token_hex(3)}.{ext}"
        (CB_DIR / fname).write_bytes(data)
        drop_image(url)
        url = "/cbadges/" + fname
    elif form.get("remove_image"):
        drop_image(url)
        url = ""
    if not url and not label:
        return bad("Pon un texto/emoji o sube una imagen")
    CB["badges"][bid] = {"n": name, "l": label, "c": color, "u": url}
    save_cb()
    return web.json_response({"ok": True, "id": bid})


async def admin_badge_delete(req):
    if not is_admin(req["sess"]):
        return deny()
    try:
        bid = str((await req.json()).get("id", ""))
    except Exception:
        return bad("Petición inválida")
    b = CB["badges"].pop(bid, None)
    if b:
        drop_image(b.get("u"))
        for u in list(CB["users"]):
            CB["users"][u] = [x for x in CB["users"][u] if x != bid]
            if not CB["users"][u]:
                CB["users"].pop(u)
        save_cb()
    return web.json_response({"ok": True})


async def admin_assign(req):
    if not is_admin(req["sess"]):
        return deny()
    try:
        body = await req.json()
    except Exception:
        return bad("Petición inválida")
    user, bid = norm(body.get("username")), str(body.get("badge", ""))
    remove = bool(body.get("remove"))
    if not USER_RE.match(user):
        return bad("Usuario inválido")
    if bid not in CB["badges"]:
        return bad("Esa insignia no existe")
    lst = CB["users"].get(user, [])
    if remove:
        lst = [x for x in lst if x != bid]
    elif bid not in lst:
        if len(CB["users"]) >= 5000 and user not in CB["users"]:
            return bad("Límite de usuarios alcanzado")
        lst.append(bid)
    if lst:
        CB["users"][user] = lst
    else:
        CB["users"].pop(user, None)
    save_cb()
    return web.json_response({"ok": True})


NOCACHE = {"Cache-Control": "no-cache"}


async def index(req):
    return web.FileResponse(HERE / "index.html", headers=NOCACHE)


async def privacy(req):
    return web.FileResponse(HERE / "privacy.html", headers=NOCACHE)


async def manifest(req):
    return web.FileResponse(STATIC / "manifest.webmanifest",
                            headers={**NOCACHE, "Content-Type": "application/manifest+json"})


async def sw(req):
    return web.FileResponse(STATIC / "sw.js",
                            headers={**NOCACHE, "Content-Type": "application/javascript",
                                     "Service-Worker-Allowed": "/"})


async def assetlinks(req):
    f = STATIC / "assetlinks.json"
    if not f.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Content-Type": "application/json"})


async def health(req):
    return web.json_response({"ok": True, "hubs": len(hubs), "sessions": len(sessions)})


async def session_ctx(app):
    s = app["s"] = ClientSession(timeout=ClientTimeout(total=20))
    badges_task = asyncio.create_task(ensure_badges(s))
    house_task = asyncio.create_task(housekeeping())
    yield
    badges_task.cancel()
    house_task.cancel()
    for hub in list(hubs.values()):
        hub.stop()
    save_history(force=True)
    save_sessions(force=True)
    await s.close()


app = web.Application(middlewares=[session_mw])
app.cleanup_ctx.append(session_ctx)
app.add_routes([web.static("/badges", BADGES_DIR),
                web.static("/icons", STATIC / "icons"),
                web.static("/cbadges", CB_DIR),
                web.get("/", index), web.get("/privacidad", privacy),
                web.get("/manifest.webmanifest", manifest), web.get("/sw.js", sw),
                web.get("/.well-known/assetlinks.json", assetlinks),
                web.get("/health", health),
                web.get("/ws", ws_handler), web.get("/api/state", api_state),
                web.get("/api/add", api_add), web.get("/api/remove", api_remove),
                web.get("/api/live", api_live), web.get("/api/emotes", api_emotes),
                web.post("/api/send", api_send),
                web.get("/api/badges", api_badges),
                web.post("/api/admin/badge", admin_badge_save),
                web.post("/api/admin/badge/delete", admin_badge_delete),
                web.post("/api/admin/assign", admin_assign),
                web.get("/auth/login", auth_login), web.get("/callback", auth_callback),
                web.get("/auth/logout", auth_logout)])

if __name__ == "__main__":
    print(f"Escuchando en http://{HOST}:{PORT}")
    if not ADMINS:
        print("AVISO: falta ADMIN_USERS en .env; nadie podrá administrar insignias.")
    if not (CID and SEC):
        print("AVISO: falta KICK_CLIENT_ID / KICK_CLIENT_SECRET; no se podrá iniciar sesión.")
    web.run_app(app, host=HOST, port=PORT, print=None)
