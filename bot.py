"""
UFF Discord Bot
United Flag Football League

CHANGES THIS VERSION (bugfix pass):
  - FIX: load_data()/save_data() were doing up to 9 sequential DB round-trips
    (one per key). Any latency there could blow past Discord's 3-second
    interaction-ack window and show "interaction failed" even though the
    bot was still working. Now batched into a single round-trip each.
  - FIX: most slash commands called load_data()/save_data() BEFORE
    acknowledging the interaction. Every command now defers immediately
    and replies via followup, so the ack happens instantly regardless of
    DB speed.
  - FIX: button views (offer accept/decline, pickup accept/decline,
    suspension confirm) now defer immediately and edit the original
    response instead of doing all their work before acking.
  - FIX: removed an accidental full duplicate of the suspension UI / bot
    setup / transaction-command block that had been pasted into the file
    twice.

  Previous version notes:
  - Transaction embeds: compact, no title, description-only layout matching screenshots
  - /set_team_emoji command added
  - /coaches shows: team_emoji role_mention — hc_info (single vertical list)
  - Bloxlink avatar fetch fixed
  - All non-public commands ephemeral
  - Staff roles updated
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import io
import logging
import aiohttp
from datetime import datetime, timedelta
import asyncpg

try:
    from PIL import Image, ImageDraw
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

log = logging.getLogger("zevora")

TOKEN               = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID            = int(os.getenv("OWNER_DISCORD_ID", "0"))
QBB_CHANNEL_ID      = int(os.getenv("QBB_CHANNEL_ID", "0"))
BLOXLINK_API_KEY    = (os.getenv("BLOXLINK_API_KEY") or os.getenv("BLOXLINK_KEY") or "").strip()
DATABASE_URL        = os.getenv("DATABASE_URL", "")
UFF_THUMBNAIL       = os.getenv("UFF_THUMBNAIL_URL", "")
UFF_BANNER          = os.getenv("UFF_BANNER_URL", "")
ZEVORA_LOGO_URL     = os.getenv("ZEVORA_LOGO_URL", "")

TRANSACTIONS_CHANNEL_ID = 1262200420151984152
UFF_FOOTER   = "United Flag Football League"
UFF_COLOR    = 0xF0C040
CASUAL_COLOR = 0x5865F2
GUILD_ID     = 1262200419564785755

MAX_ROSTER       = 20
STARTING_ELO     = 900
WIN_ELO          = 100
LOSS_ELO         = 100
COOLDOWN_MINUTES = 30

HEAD_COACH_ROLE_ID      = 1262200419615117330
ASSISTANT_COACH_ROLE_ID = 1267278742389264406

PICKUP_ALLOWED_ROLE_IDS = {
    1269693904815521994,
    1404271074623099040,
    1404271002241728617,
    1429344923865448550,
    1262200419686285342,
    1401450124424642561,
}

STAFF_ROLE_IDS = {
    1404271002241728617,
    1429344923865448550,
    1262200419686285342,
    1401450124424642561,
    1499141732108079225,
    1434653599236882574,
    1502941495722770472,
}

SUSPENSION_CHANNEL_ID = 1364423515532427264
SUSPENSION_ALLOWED_USER_IDS = {1414340980110528546, 1055321446978691112}
SUSPENSION_ALLOWED_ROLE_IDS = {1513234210054344925, 1499141732108079225}
MAX_SUSPENSION_REASONS = 5

SUSPENSION_REASONS = {
    "exploiting_x1":             ("Exploiting",                 24),
    "dodging_screenshare_x1":    ("Dodging Screenshare",         6),
    "illegally_playing_x1":      ("Illegally Playing",           2),
    "illegally_playing_x2":      ("Illegally Playing",           2),
    "illegally_playing_x3":      ("Illegally Playing",           2),
    "illegally_playing_x4":      ("Illegally Playing",           2),
    "possession_of_exploits_x1": ("Possession of Exploits",     12),
    "possession_of_exploits_x2": ("Possession of Exploits",     12),
    "possession_of_exploits_x3": ("Possession of Exploits",     12),
    "possession_of_exploits_x4": ("Possession of Exploits",     12),
    "gameplay_manipulation_x1":  ("Gameplay Manipulation",       8),
    "alting_x1":                 ("Alting",                     12),
    "alting_x2":                 ("Alting",                     12),
    "alting_x3":                 ("Alting",                     12),
    "alting_x4":                 ("Alting",                     12),
    "disbanding_x1":             ("Disbanding",                  4),
    "distributing_exploits_x1":  ("Distributing Exploits",      40),
    "distributing_alts_x1":      ("Distributing Alt Accounts",  25),
    "framing_x1":                ("Framing",                    12),
    "obstruction_of_justice_x1": ("Obstruction of Justice",      8),
}
SPECIAL_SUSPENSION_REASONS = {"ineligible_until_ss": "Ineligible Until Screenshare"}
EXTRA_DEMAND_GRANT_USER_IDS = {1055321446978691112, 391036854084042762}

TEAMS = [
    ("Shiroishi Samurai","SHI",0x4d4d4e),("Vicksburg Vortex","VIC",0x230552),
    ("Salt Lake City Sentinels","SLC",0xdb0e16),("Nashville Nightmares","NSH",0x5b00c4),
    ("Warwick Warhawks","WAR",0x27833d),("Sunny Isle Sea Serpents","SISS",0x00b6ba),
    ("Los Angeles Golden Knights","LGK",0xf5be23),("Michigan Mustangs","MMS",0xfe001f),
    ("Portsmouth Panthers","PORT",0x01a1f2),("Columbus Colts","COL",0x184da7),
    ("Milwaukee Rams","MIL",0xc5aa76),("Salisbury Falcons","SALI",0x052270),
    ("Savannah Raiders","SAV",0xbb0620),("Highridge Huskies","HIG",0x767878),
    ("Deltabay Dolphins","DTB",0x0099fc),("Seattle Skyclaws","SEA",0x004a8b),
    ("Alabama Bloom","AL",0xf7adad),("Oklahoma City Owls","OKC",0x67112a),
    ("Myrtle Beach Hammerheads","MYB",0x215792),("Windy City Warriors","WC",0x4126a5),
]

# ── DB ────────────────────────────────────────────────────────────────
_db_pool = None

async def get_db():
    global _db_pool
    if _db_pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _db_pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, timeout=10, command_timeout=10
        )
    return _db_pool

async def init_db():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS uff_store (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)

_STORE_KEYS = ["players","matches","pending","casual_pending",
               "suspensions","teams","demand_used","extra_demands","offers"]

async def load_data():
    """
    FIX: previously this fired one SELECT per key (up to 9 separate DB
    round-trips). Now it's a single round-trip for the whole batch, which
    is what was causing slow commands to blow past Discord's 3-second
    interaction-ack window.
    """
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM uff_store WHERE key = ANY($1::text[])",
            _STORE_KEYS,
        )
    found = {r["key"]: json.loads(r["value"]) for r in rows}
    data = {}
    for k in _STORE_KEYS:
        data[k] = found.get(k, [] if k in ("matches", "suspensions") else {})
    return data

async def save_data(data):
    """
    FIX: previously this fired one INSERT/UPDATE per key sequentially.
    Now it's a single pipelined batch on one connection.
    """
    pool = await get_db()
    rows = [(k, json.dumps(v, default=str)) for k, v in data.items()]
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO uff_store(key,value) VALUES($1,$2) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            rows
        )

# ── HELPERS ───────────────────────────────────────────────────────────
def get_player(data, uid):
    k = str(uid)
    if k not in data["players"]:
        data["players"][k] = {"elo":STARTING_ELO,"wins":0,"losses":0,"last_game":None,"username":""}
    return data["players"][k]

def get_rank(elo):
    if elo>=2100: return "Amethyst III","💎",0xA040E8
    if elo>=1900: return "Amethyst II","💎",0xA040E8
    if elo>=1700: return "Amethyst I","💎",0xA040E8
    if elo>=1500: return "Gold III","🥇",0xF0C040
    if elo>=1300: return "Gold II","🥇",0xF0C040
    if elo>=1100: return "Gold I","🥇",0xF0C040
    if elo>=900:  return "Iron III","⚙️",0x8090A0
    if elo>=700:  return "Iron II","⚙️",0x8090A0
    return "Iron I","⚙️",0x8090A0

def on_cooldown(data, uid):
    p = get_player(data, uid)
    if not p["last_game"]: return False, None
    diff = datetime.fromisoformat(p["last_game"]) + timedelta(minutes=COOLDOWN_MINUTES) - datetime.utcnow()
    if diff.total_seconds() > 0:
        return True, f"{int(diff.total_seconds()//60)}m {int(diff.total_seconds()%60)}s"
    return False, None

def is_admin(i): return i.user.id==OWNER_ID or i.user.guild_permissions.administrator
def is_staff(i):
    if is_admin(i): return True
    return bool({r.id for r in i.user.roles} & STAFF_ROLE_IDS)
def can_issue_suspension(i):
    if i.user.id in SUSPENSION_ALLOWED_USER_IDS: return True
    return bool({r.id for r in i.user.roles} & SUSPENSION_ALLOWED_ROLE_IDS) or is_admin(i)

def _valid_embed_url(url, *, allow_attachment=False):
    """Discord only accepts http(s) URLs, or attachment:// when a file is attached."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if allow_attachment and url.startswith("attachment://"):
        name = url[len("attachment://"):]
        return url if name else None
    if url.startswith(("http://", "https://")) and " " not in url:
        return url
    return None

def _safe_set_thumbnail(embed, url, *, allow_attachment=False):
    valid = _valid_embed_url(url, allow_attachment=allow_attachment)
    if valid:
        embed.set_thumbnail(url=valid)

def _safe_set_image(embed, url):
    valid = _valid_embed_url(url)
    if valid:
        embed.set_image(url=valid)

def _league_thumb(guild):
    for candidate in (ZEVORA_LOGO_URL, UFF_THUMBNAIL):
        valid = _valid_embed_url(candidate)
        if valid:
            return valid
    if guild and guild.icon:
        return _valid_embed_url(str(guild.icon.url))
    return None

def _team_logo_url(team, guild=None):
    logo = _valid_embed_url(team.get("logo_url", ""))
    if logo:
        return logo
    rid = team.get("role_id")
    if guild and rid:
        role = guild.get_role(int(rid))
        if role and role.display_icon:
            return _valid_embed_url(str(role.display_icon.url))
    return None

def _role_warn(label):
    return f"\n⚠️ Could not update **{label}** role — check bot role position and Manage Roles permission."

async def get_ch(guild, ch_id):
    if not ch_id: return None
    ch = guild.get_channel(ch_id)
    if ch: return ch
    try: return await guild.fetch_channel(ch_id)
    except: return None

async def get_pickup_ch(guild): return await get_ch(guild, QBB_CHANNEL_ID)
async def get_susp_ch(guild):   return await get_ch(guild, SUSPENSION_CHANNEL_ID)
async def get_tx_ch(guild):     return await get_ch(guild, TRANSACTIONS_CHANNEL_ID)

# ── BLOXLINK ──────────────────────────────────────────────────────────
def _parse_bloxlink_body(body):
    """Extract Roblox ID from Bloxlink v4 response shapes."""
    if not isinstance(body, dict):
        return None
    if body.get("error"):
        return None
    rid = body.get("robloxID") or body.get("RobloxID") or body.get("robloxId")
    if rid is not None:
        return str(rid)
    resolved = body.get("resolved") or body.get("Resolved") or {}
    roblox = resolved.get("roblox") or resolved.get("Roblox") or {}
    rid = roblox.get("id") or roblox.get("Id")
    return str(rid) if rid is not None else None

async def _resolve_roblox_headshot(session, roblox_id):
    """Fetch the CDN headshot URL from Roblox's thumbnails API."""
    if not roblox_id:
        return None
    api_url = (
        f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
        f"?userIds={roblox_id}&size=420x420&format=Png&isCircular=false"
    )
    body, err = await _fetch_json(session, api_url, {})
    if not body or err:
        return None
    for item in body.get("data", []):
        image_url = _valid_embed_url(item.get("imageUrl", ""))
        if image_url and item.get("state") in ("Completed", "Pending"):
            return image_url
    return None

async def _fetch_json(session, url, headers):
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
        try:
            body = await r.json()
        except Exception:
            body = None
        if r.status != 200:
            err = body.get("error") if isinstance(body, dict) else None
            return None, str(err or f"HTTP {r.status}")
        if isinstance(body, dict) and body.get("error"):
            return None, str(body["error"])
        return body, None

async def _roblox_id_from_username(session, username):
    if not username or username in ("Unknown", "None"):
        return None
    try:
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            users = data.get("data", [])
            if users:
                return str(users[0].get("id"))
    except Exception:
        pass
    return None

def _roster_roblox_hint(team, discord_id):
    """Use saved roster/coach Roblox data when Bloxlink API lookup fails."""
    s = str(discord_id)
    if not team:
        return None, None
    if team.get("head_coach_id") == s:
        return team.get("head_coach_roblox"), team.get("head_coach_roblox_id")
    if team.get("ahc_id") == s:
        return team.get("ahc_roblox"), team.get("ahc_roblox_id")
    for entry in team.get("roster", []):
        if entry.get("id") == s:
            return entry.get("roblox"), entry.get("roblox_id")
    return None, None

async def bloxlink_lookup(discord_id, guild_id, team=None):
    if not BLOXLINK_API_KEY:
        result = {"error": "BLOXLINK_API_KEY not set"}
    else:
        headers = {"Authorization": BLOXLINK_API_KEY}
        urls = [
            f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}",
            f"https://api.blox.link/v4/public/discord-to-roblox/{discord_id}",
        ]
        result = {"error": "No Roblox account linked"}
        try:
            async with aiohttp.ClientSession() as session:
                rid = None
                last_err = "No Roblox account linked"
                for url in urls:
                    body, err = await _fetch_json(session, url, headers)
                    if err:
                        last_err = err
                        log.warning("Bloxlink %s -> %s", url, err)
                        continue
                    rid = _parse_bloxlink_body(body)
                    if rid:
                        break
                if rid:
                    body, err = await _fetch_json(session, f"https://users.roblox.com/v1/users/{rid}", {})
                    username = body.get("name", str(rid)) if body and not err else str(rid)
                    avatar_url = await _resolve_roblox_headshot(session, rid) or ""
                    return {
                        "roblox_username": username,
                        "roblox_id": int(rid),
                        "avatar_url": avatar_url,
                    }
                result = {"error": last_err}
        except Exception as e:
            log.exception("Bloxlink lookup failed for %s", discord_id)
            result = {"error": str(e)}

    # Fallback: roster/coach stored Roblox username or ID
    rbx_name, rbx_id = _roster_roblox_hint(team, discord_id)
    if rbx_id:
        rid = str(rbx_id)
        try:
            async with aiohttp.ClientSession() as session:
                avatar_url = await _resolve_roblox_headshot(session, rid) or ""
        except Exception:
            avatar_url = ""
        return {
            "roblox_username": rbx_name or rid,
            "roblox_id": int(rid),
            "avatar_url": avatar_url,
        }
    if rbx_name:
        try:
            async with aiohttp.ClientSession() as session:
                rid = await _roblox_id_from_username(session, rbx_name)
                if rid:
                    avatar_url = await _resolve_roblox_headshot(session, rid) or ""
                    return {
                        "roblox_username": rbx_name,
                        "roblox_id": int(rid),
                        "avatar_url": avatar_url,
                    }
        except Exception:
            pass
    return result

async def _download_image(session, url):
    if not url:
        return None
    try:
        headers = {"User-Agent": "UFF-Discord-Bot/1.0"}
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.read()
                if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8":
                    return data
    except Exception:
        pass
    return None

async def _fetch_avatar_bytes(session, avatar_url, roblox_id=None):
    """Download avatar PNG bytes from a CDN URL or resolve via Roblox ID."""
    avatar_url = _valid_embed_url(avatar_url)
    avatar_bytes = await _download_image(session, avatar_url) if avatar_url else None
    if avatar_bytes:
        return avatar_bytes, avatar_url
    if not roblox_id:
        return None, avatar_url
    headshot_url = await _resolve_roblox_headshot(session, roblox_id)
    if not headshot_url:
        return None, avatar_url
    avatar_bytes = await _download_image(session, headshot_url)
    return avatar_bytes, headshot_url

def _remove_roblox_backdrop(img, tolerance=28):
    """Remove Roblox's solid black headshot backdrop without eating dark clothing."""
    img = img.convert("RGBA")
    w, h = img.size
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        r, g, b, _ = img.getpixel((x, y))
        if max(r, g, b) <= tolerance:
            ImageDraw.floodfill(img, (x, y), (0, 0, 0, 0), thresh=tolerance)
    return img

def _make_composite_png(avatar_bytes, logo_bytes, size=256):
    """Roblox headshot in front with team logo peeking out behind on the right."""
    if not _PIL_OK:
        return None
    avatar = _remove_roblox_backdrop(Image.open(io.BytesIO(avatar_bytes)))
    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    logo_dim = int(size * 0.72)
    logo = logo.resize((logo_dim, logo_dim), Image.Resampling.LANCZOS)
    logo_x = int(size * 0.38)
    logo_y = (size - logo_dim) // 2
    canvas.paste(logo, (logo_x, logo_y), logo)

    av_dim = int(size * 0.92)
    avatar = avatar.resize((av_dim, av_dim), Image.Resampling.LANCZOS)
    av_x = int(size * 0.02)
    av_y = (size - av_dim) // 2
    canvas.paste(avatar, (av_x, av_y), avatar)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out

async def make_composite_file(avatar_url, logo_url, roblox_id=None):
    logo_url = _valid_embed_url(logo_url)
    if not logo_url or not _PIL_OK:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            avatar_bytes, _ = await _fetch_avatar_bytes(session, avatar_url, roblox_id)
            logo_bytes = await _download_image(session, logo_url)
        if not avatar_bytes or not logo_bytes:
            return None
        png = _make_composite_png(avatar_bytes, logo_bytes)
        if not png:
            return None
        return discord.File(png, filename="composite.png")
    except Exception:
        log.exception("Composite thumbnail failed")
        return None

def _player_tx_line(player, blox):
    rbx_id = blox.get("roblox_id")
    if rbx_id:
        return f"{player.mention} `{rbx_id}`"
    rbx_name = blox.get("roblox_username")
    if rbx_name and rbx_name != "Unknown":
        return f"{player.mention} `{rbx_name}`"
    return f"{player.mention} (@{player.name})"

async def _apply_tx_images(embed, blox, team, guild):
    """
    Transaction thumbnail only (top-right):
      1. Composite: Roblox headshot over team logo
      2. Roblox headshot alone
      3. Team logo
    """
    logo_url = _team_logo_url(team, guild)
    avatar_url = _valid_embed_url(blox.get("avatar_url", ""))
    roblox_id = blox.get("roblox_id")

    if not avatar_url and roblox_id:
        try:
            async with aiohttp.ClientSession() as session:
                avatar_url = await _resolve_roblox_headshot(session, roblox_id)
        except Exception:
            log.exception("Headshot lookup failed for Roblox ID %s", roblox_id)

    composite = await make_composite_file(avatar_url, logo_url, roblox_id=roblox_id)
    if composite:
        _safe_set_thumbnail(embed, "attachment://composite.png", allow_attachment=True)
        return composite
    if avatar_url:
        _safe_set_thumbnail(embed, avatar_url)
    elif logo_url:
        _safe_set_thumbnail(embed, logo_url)
    else:
        _safe_set_thumbnail(embed, _league_thumb(guild))
    return None

# ── TEAM HELPERS ──────────────────────────────────────────────────────
def get_team_by_role(data, role_id):
    return data["teams"].get(str(role_id))

def get_team_for_hc(data, uid):
    s = str(uid)
    for rid, t in data["teams"].items():
        if t.get("head_coach_id") == s: return rid, t
    return None, None

def get_team_for_user(data, uid):
    s = str(uid)
    for rid, t in data["teams"].items():
        if t.get("head_coach_id") == s: return rid, t
    for rid, t in data["teams"].items():
        if t.get("ahc_id") == s: return rid, t
    return None, None

def get_role(guild, rid_str):
    try: return guild.get_role(int(rid_str))
    except: return None

def _base_label(k): return SUSPENSION_REASONS[k][0]

def _susp_summary(selected):
    nk = [r for r in selected if r in SUSPENSION_REASONS]
    sk = [r for r in selected if r in SPECIAL_SUSPENSION_REASONS]
    total = sum(SUSPENSION_REASONS[r][1] for r in nk)
    lg = {}
    for r in nk:
        lb = _base_label(r); lg.setdefault(lb,[]).append(SUSPENSION_REASONS[r][1])
    lines = []
    for lb, gl in lg.items():
        c=len(gl); p=gl[0]; sub=p*c
        lines.append(f"• **{lb}** — {p}g ×{c} = **{sub} games**" if c>1 else f"• **{lb}** — {p} games")
    rl = "\n".join(lines)
    sl = ("**Status:** " + ", ".join(f"`{SPECIAL_SUSPENSION_REASONS[r]}`" for r in sk)) if sk else ""
    return total, rl, sl

# ── EMBED BUILDER ─────────────────────────────────────────────────────
def _info_block(team):
    """
    Compact info block with "> " prefix — Discord renders dark left bar.
    """
    sz = len(team.get("roster",[]))
    hc_id   = team.get("head_coach_id")
    hc_name = team.get("head_coach_name","")
    hc_rbx  = team.get("head_coach_roblox","")
    ahc_id  = team.get("ahc_id")
    ahc_name= team.get("ahc_name","")
    ahc_rbx = team.get("ahc_roblox","")

    lines = [f"> roster: {sz}/{MAX_ROSTER}"]
    if hc_id:
        rbx = f" `{hc_rbx}`" if hc_rbx else ""
        lines.append(f"> head coach: <@{hc_id}> (@{hc_name}) ✓{rbx}")
    else:
        lines.append("> head coach: vacant")
    if ahc_id:
        rbx = f" `{ahc_rbx}`" if ahc_rbx else ""
        lines.append(f"> assistant coach: <@{ahc_id}> (@{ahc_name}) ✓{rbx}")
    else:
        lines.append("> assistant coach: vacant")
    return "\n".join(lines)

async def build_tx_embed(action, player, team, team_role, guild, color=UFF_COLOR, blox=None):
    """
    Compact transaction embed matching league style:
      - Description includes player mention + Roblox ID
      - Thumbnail: Roblox headshot + team logo composite (top-right)
      - Image: team logo (bottom)
    Returns (embed, optional discord.File for composite thumbnail).
    """
    if blox is None:
        blox = await bloxlink_lookup(player.id, guild.id, team=team)

    emoji = team.get("emoji", "")
    role_str = team_role.mention if team_role else f"**{team['name']}**"
    prefix = f"{emoji} " if emoji else ""

    embed = discord.Embed(
        description=(
            f"{prefix}{role_str} have **{action.lower()}** {_player_tx_line(player, blox)}!\n\n"
            f"{_info_block(team)}"
        ),
        color=color,
    )
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    file = await _apply_tx_images(embed, blox, team, guild)
    return embed, file

async def build_coach_embed(action, player, team, team_role, guild, color=UFF_COLOR, blox=None):
    if blox is None:
        blox = await bloxlink_lookup(player.id, guild.id, team=team)
    is_promo = "promot" in action.lower()
    role_lbl = "assistant coach" if is_promo else "regular player"

    emoji = team.get("emoji", "")
    role_str = team_role.mention if team_role else f"**{team['name']}**"
    prefix = f"{emoji} " if emoji else ""

    embed = discord.Embed(
        description=(
            f"{prefix}{role_str} have **{action.lower()}** {_player_tx_line(player, blox)} "
            f"to {role_lbl}!\n\n"
            f"{_info_block(team)}"
        ),
        color=color,
    )
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    file = await _apply_tx_images(embed, blox, team, guild)
    return embed, file

async def post_tx(guild, embed, followup=None, interaction=None, msg="", ephemeral=True, file=None):
    # Never send attachment:// refs without the matching file attached.
    if embed.thumbnail and embed.thumbnail.url:
        if embed.thumbnail.url.startswith("attachment://") and not file:
            embed.set_thumbnail(url=None)
        elif not _valid_embed_url(embed.thumbnail.url, allow_attachment=bool(file)):
            embed.set_thumbnail(url=None)
    if embed.image and embed.image.url and not _valid_embed_url(embed.image.url):
        embed.set_image(url=None)

    ch = await get_tx_ch(guild)
    send_kwargs = {"embed": embed}
    if file:
        send_kwargs["file"] = file
    if ch:
        await ch.send(**send_kwargs)
        if followup and msg:
            await followup.send(msg, ephemeral=ephemeral)
        elif interaction and msg:
            try:
                await interaction.response.send_message(msg, ephemeral=ephemeral)
            except discord.InteractionResponded:
                await interaction.followup.send(msg, ephemeral=ephemeral)
    else:
        target = followup or interaction
        if target:
            try:
                await target.send(**send_kwargs)
            except Exception:
                await target.followup.send(**send_kwargs)

async def _cmd_error(interaction, label, exc):
    log.exception("%s failed", label)
    text = f"❌ {label} failed: `{exc}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass

# ── OFFER VIEW ────────────────────────────────────────────────────────
class OfferView(discord.ui.View):
    def __init__(self, offer_id, team_role_id, team_name, team_logo, team_emoji, hc_id, player_id, guild_id):
        super().__init__(timeout=43200)
        self.offer_id=offer_id; self.team_role_id=team_role_id; self.team_name=team_name
        self.team_logo=team_logo; self.team_emoji=team_emoji; self.hc_id=hc_id
        self.player_id=player_id; self.guild_id=guild_id; self.responded=False

    async def on_timeout(self):
        for item in self.children: item.disabled=True
        data=await load_data(); data.get("offers",{}).pop(self.offer_id,None); await save_data(data)

    @discord.ui.button(label="✅  Accept",style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id!=self.player_id:
            await interaction.response.send_message("❌ This offer isn't for you.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True

        # FIX: ack immediately (deferred message-update) before any DB/network work,
        # then edit the original response when everything is ready instead of
        # racing the 3-second interaction window.
        await interaction.response.defer()

        data=await load_data(); guild=bot.get_guild(self.guild_id)
        if not guild:
            await interaction.edit_original_response(content="❌ Server not found.",view=self); return

        rid=str(self.team_role_id); team=data["teams"].get(rid)
        if not team:
            await interaction.edit_original_response(content="❌ Team no longer exists.",view=self); return

        roster=team.setdefault("roster",[])
        if len(roster)>=MAX_ROSTER:
            await interaction.edit_original_response(content=f"❌ Roster cap ({MAX_ROSTER}) reached.",view=self); return
        if str(interaction.user.id) in [r["id"] for r in roster]:
            await interaction.edit_original_response(content="❌ Already on this team.",view=self); return

        try:   player=guild.get_member(self.player_id) or await guild.fetch_member(self.player_id)
        except discord.NotFound:
            await interaction.edit_original_response(content="❌ Couldn't find you in server.",view=self); return

        blox=await bloxlink_lookup(player.id,guild.id,team=team)
        rbx_name=blox.get("roblox_username","Unknown")

        roster.append({
            "id":str(player.id),"name":player.display_name,"roblox":rbx_name,
            "roblox_id":blox.get("roblox_id"),"role":"Player",
        })
        data.get("offers",{}).pop(self.offer_id,None); await save_data(data)

        team_role=guild.get_role(int(rid)); role_str=team_role.mention if team_role else f"**{team['name']}**"
        role_failed=False
        if team_role:
            try: await player.add_roles(team_role,reason=f"Signed to {team['name']}")
            except discord.Forbidden: role_failed=True

        embed,file=await build_tx_embed(
            "signed",player,team,team_role,guild,
            color=team.get("color",UFF_COLOR),blox=blox,
        )

        ch=await get_tx_ch(guild)
        if ch:
            kwargs={"embed":embed}
            if file: kwargs["file"]=file
            await ch.send(**kwargs)

        desc=f"You accepted the offer from **{self.team_name}**!\n\nWelcome to the team."
        if role_failed: desc+="\n\n⚠️ Team role couldn't be added automatically."
        ae=discord.Embed(title="✅ Offer Accepted!",description=desc,color=0x57F287)
        _safe_set_thumbnail(ae, _valid_embed_url(self.team_logo))
        ae.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(content=None,embed=ae,view=self)

        if self.hc_id:
            try:
                hc=guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id))
                n=discord.Embed(title="✅ Offer Accepted",
                    description=f"**{player.display_name}** accepted your offer to **{self.team_name}**.",color=0x57F287)
                n.set_footer(text=UFF_FOOTER); await hc.send(embed=n)
            except: pass

    @discord.ui.button(label="❌  Decline",style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id!=self.player_id:
            await interaction.response.send_message("❌ This offer isn't for you.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True

        await interaction.response.defer()
        data=await load_data(); data.get("offers",{}).pop(self.offer_id,None); await save_data(data)
        e=discord.Embed(title="❌ Offer Declined",
            description=f"You declined the offer from **{self.team_name}**.",color=0xED4245)
        _safe_set_thumbnail(e, _valid_embed_url(self.team_logo))
        e.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(embed=e,view=self)
        guild=bot.get_guild(self.guild_id)
        if guild and self.hc_id:
            try:
                hc=guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id))
                tgt=guild.get_member(self.player_id)
                n=discord.Embed(title="❌ Offer Declined",
                    description=f"**{tgt.display_name if tgt else 'The player'}** declined your offer to **{self.team_name}**.",color=0xED4245)
                n.set_footer(text=UFF_FOOTER); await hc.send(embed=n)
            except: pass

# ── RANKED PICKUP VIEW ────────────────────────────────────────────────
class RankedPickupView(discord.ui.View):
    def __init__(self,match_id,challenger_id,opponent_id,challenger_name,opponent_name,
                 challenger_team,opponent_team,game_link,guild_id):
        super().__init__(timeout=1800)
        self.match_id=match_id; self.challenger_id=challenger_id; self.opponent_id=opponent_id
        self.challenger_name=challenger_name; self.opponent_name=opponent_name
        self.challenger_team=challenger_team; self.opponent_team=opponent_team
        self.game_link=game_link; self.guild_id=guild_id; self.responded=False

    async def on_timeout(self):
        for item in self.children: item.disabled=True

    @discord.ui.button(label="✅  Accept Challenge",style=discord.ButtonStyle.success)
    async def accept(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True

        # FIX: ack first, do the heavy lifting after.
        await interaction.response.defer()

        data=await load_data(); guild=bot.get_guild(self.guild_id)
        if not guild:
            await interaction.edit_original_response(content="❌ Server not found.",view=self); return
        try:
            challenger=guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent=guild.get_member(self.opponent_id) or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.edit_original_response(content="❌ Could not find players.",view=self); return

        p1=get_player(data,challenger.id); p2=get_player(data,opponent.id)
        r1,e1,_=get_rank(p1["elo"]); r2,e2,_=get_rank(p2["elo"])
        embed=discord.Embed(title="ranked pickup matchup",color=UFF_COLOR)
        embed.add_field(name=f"🟡 {challenger.display_name}",
            value=f"{challenger.mention}\n**{self.challenger_team}**\nRank: `{e1} {r1}`",inline=True)
        embed.add_field(name="\u200b",value="**— VS —**",inline=True)
        embed.add_field(name=f"🔵 {opponent.display_name}",
            value=f"{opponent.mention}\n**{self.opponent_team}**\nRank: `{e2} {r2}`",inline=True)
        embed.add_field(name="🔗 game link",value=f"[Click here →]({self.game_link})",inline=False)
        if UFF_BANNER: embed.set_image(url=UFF_BANNER)
        lt=_league_thumb(guild)
        if lt: embed.set_thumbnail(url=lt)
        embed.set_footer(text=f"✅ LIVE • /pickup_results when done | {UFF_FOOTER}")
        embed.timestamp=datetime.utcnow()

        ch=await get_pickup_ch(guild)
        if ch: await ch.send(content=f"@everyone **Ranked Pickup** — **{challenger.display_name}** vs **{opponent.display_name}**",embed=embed)
        else:
            try: await challenger.send(embed=embed)
            except: pass

        ack=discord.Embed(title="✅ Challenge Accepted!",
            description=f"You accepted **{self.challenger_name}**'s challenge!\n🔗 [Join]({self.game_link})",color=0x57F287)
        ack.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(content=None,embed=ack,view=self)
        try:
            n=discord.Embed(title="✅ Challenge Accepted!",
                description=f"**{self.opponent_name}** accepted your challenge!\n🔗 [Join]({self.game_link})\nUse `/pickup_results` when done.",color=0x57F287)
            n.set_footer(text=UFF_FOOTER); await challenger.send(embed=n)
        except: pass

    @discord.ui.button(label="❌  Decline",style=discord.ButtonStyle.danger)
    async def decline(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True
        await interaction.response.defer()
        data=await load_data(); data.get("pending",{}).pop(self.match_id,None); await save_data(data)
        e=discord.Embed(title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s pickup.",color=0xED4245)
        e.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(embed=e,view=self)
        guild=bot.get_guild(self.guild_id)
        if guild:
            try:
                c=guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                n=discord.Embed(title="❌ Challenge Declined",
                    description=f"**{self.opponent_name}** declined your pickup.",color=0xED4245)
                n.set_footer(text=UFF_FOOTER); await c.send(embed=n)
            except: pass

# ── CASUAL PICKUP VIEW ────────────────────────────────────────────────
class CasualPickupView(discord.ui.View):
    def __init__(self,match_id,challenger_id,opponent_id,challenger_name,opponent_name,
                 challenger_team,opponent_team,game_link,guild_id):
        super().__init__(timeout=1800)
        self.match_id=match_id; self.challenger_id=challenger_id; self.opponent_id=opponent_id
        self.challenger_name=challenger_name; self.opponent_name=opponent_name
        self.challenger_team=challenger_team; self.opponent_team=opponent_team
        self.game_link=game_link; self.guild_id=guild_id; self.responded=False

    async def on_timeout(self):
        for item in self.children: item.disabled=True

    @discord.ui.button(label="✅  Accept Challenge",style=discord.ButtonStyle.success)
    async def accept(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True

        await interaction.response.defer()

        guild=bot.get_guild(self.guild_id)
        if not guild:
            await interaction.edit_original_response(content="❌ Server not found.",view=self); return
        try:
            challenger=guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent=guild.get_member(self.opponent_id) or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.edit_original_response(content="❌ Could not find players.",view=self); return

        data=await load_data(); data.get("casual_pending",{}).pop(self.match_id,None); await save_data(data)
        embed=discord.Embed(title="casual pickup matchup",color=CASUAL_COLOR)
        embed.add_field(name=f"🟡 {challenger.display_name}",value=f"{challenger.mention}\n**{self.challenger_team}**",inline=True)
        embed.add_field(name="\u200b",value="**— VS —**",inline=True)
        embed.add_field(name=f"🔵 {opponent.display_name}",value=f"{opponent.mention}\n**{self.opponent_team}**",inline=True)
        embed.add_field(name="🔗 game link",value=f"[Click here →]({self.game_link})",inline=False)
        if UFF_BANNER: embed.set_image(url=UFF_BANNER)
        lt=_league_thumb(guild)
        if lt: embed.set_thumbnail(url=lt)
        embed.set_footer(text=f"✅ LIVE | {UFF_FOOTER}"); embed.timestamp=datetime.utcnow()

        ch=await get_pickup_ch(guild)
        if ch: await ch.send(content=f"@here **Casual Pickup** — **{challenger.display_name}** vs **{opponent.display_name}**",embed=embed)
        else:
            try: await challenger.send(embed=embed)
            except: pass

        ack=discord.Embed(title="✅ Challenge Accepted!",
            description=f"You accepted **{self.challenger_name}**'s casual!\n🔗 [Join]({self.game_link})",color=0x57F287)
        ack.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(content=None,embed=ack,view=self)
        try:
            n=discord.Embed(title="✅ Challenge Accepted!",
                description=f"**{self.opponent_name}** accepted your casual!\n🔗 [Join]({self.game_link})",color=0x57F287)
            n.set_footer(text=UFF_FOOTER); await challenger.send(embed=n)
        except: pass

    @discord.ui.button(label="❌  Decline",style=discord.ButtonStyle.danger)
    async def decline(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True
        await interaction.response.defer()
        data=await load_data(); data.get("casual_pending",{}).pop(self.match_id,None); await save_data(data)
        e=discord.Embed(title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s casual.",color=0xED4245)
        e.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(embed=e,view=self)
        guild=bot.get_guild(self.guild_id)
        if guild:
            try:
                c=guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                n=discord.Embed(title="❌ Challenge Declined",
                    description=f"**{self.opponent_name}** declined your casual.",color=0xED4245)
                n.set_footer(text=UFF_FOOTER); await c.send(embed=n)
            except: pass

async def _run_casual(interaction,opponent,game_link,your_team,opponent_team):
    # FIX: ack immediately, then do validation/DB work and report via followup.
    await interaction.response.defer(ephemeral=True)

    if not ({r.id for r in interaction.user.roles}&PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.followup.send("❌ Missing required role.",ephemeral=True); return
    if opponent.id==interaction.user.id:
        await interaction.followup.send("❌ Can't challenge yourself!",ephemeral=True); return
    if opponent.bot:
        await interaction.followup.send("❌ Can't challenge a bot!",ephemeral=True); return

    data=await load_data()
    mid=f"casual_{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("casual_pending",{})[mid]={
        "challenger_id":str(interaction.user.id),"opponent_id":str(opponent.id),
        "challenger_name":interaction.user.display_name,"opponent_name":opponent.display_name,
        "challenger_team":your_team,"opponent_team":opponent_team,"game_link":game_link,
        "timestamp":datetime.utcnow().isoformat(),"match_id":mid,"guild_id":interaction.guild.id
    }
    await save_data(data)

    lt=_league_thumb(interaction.guild)
    dm=discord.Embed(title="🏈 Casual Pickup Challenge!",
        description=f"**{interaction.user.display_name}** wants a casual pickup. Expires **30 min**.",color=CASUAL_COLOR)
    dm.add_field(name=f"🟡 {interaction.user.display_name}",value=f"`{interaction.user.name}`\n**{your_team}**",inline=True)
    dm.add_field(name="\u200b",value="**— VS —**",inline=True)
    dm.add_field(name=f"🔵 {opponent.display_name}",value=f"`{opponent.name}`\n**{opponent_team}**",inline=True)
    dm.add_field(name="🔗 Game Link",value=f"[Click here →]({game_link})",inline=False)
    dm.add_field(name="\u200b",value="⚠️ **NOT ranked** — no ELO changes.",inline=False)
    if UFF_BANNER: dm.set_image(url=UFF_BANNER)
    if lt: dm.set_thumbnail(url=lt)
    dm.set_footer(text=UFF_FOOTER); dm.timestamp=datetime.utcnow()

    view=CasualPickupView(mid,interaction.user.id,opponent.id,interaction.user.display_name,
        opponent.display_name,your_team,opponent_team,game_link,interaction.guild.id)
    try: await opponent.send(embed=dm,view=view)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ Can't DM **{opponent.display_name}**.",ephemeral=True)
        data.get("casual_pending",{}).pop(mid,None); await save_data(data); return

    ack=discord.Embed(title="📨 Casual Sent!",
        description=f"Challenge sent to **{opponent.display_name}**. Posted publicly only if accepted.",color=0x57F287)
    ack.set_footer(text=f"{UFF_FOOTER} • 30-minute window")
    await interaction.followup.send(embed=ack,ephemeral=True)

# ── SUSPENSION UI ─────────────────────────────────────────────────────
def _make_susp_opts():
    opts=[]; seen={}
    for k,(lb,g) in SUSPENSION_REASONS.items():
        seen[lb]=seen.get(lb,0)+1; n=seen[lb]
        if n==1: opts.append(discord.SelectOption(label=f"{lb} — {g} games",value=k,description=f"Adds {g} games"))
        else:    opts.append(discord.SelectOption(label=f"{lb} (×{n}) — +{g} games",value=k,description=f"Stack: +{g} games"))
    for k,lb in SPECIAL_SUSPENSION_REASONS.items():
        opts.append(discord.SelectOption(label=lb,value=k,description="Status only — no games added"))
    return opts[:25]

class SuspReasonSelect(discord.ui.Select):
    def __init__(self):
        opts=_make_susp_opts()
        super().__init__(placeholder=f"Select up to {MAX_SUSPENSION_REASONS} reasons...",
                         min_values=1,max_values=min(MAX_SUSPENSION_REASONS,len(opts)),options=opts)
    async def callback(self,interaction:discord.Interaction):
        v:SuspView=self.view; v.selected=self.values
        total,rl,sl=_susp_summary(v.selected)
        p=f"**Suspension preview — {v.target.display_name}**\n\n"
        if rl: p+=rl+"\n\n"
        if sl: p+=sl+"\n\n"
        p+=f"**Total: {total} games**\n\nClick **Confirm & Post**."
        await interaction.response.edit_message(content=p,view=v)

class SuspConfirmBtn(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Confirm & Post",style=discord.ButtonStyle.success,row=1)
    async def callback(self,interaction:discord.Interaction):
        v:SuspView=self.view
        if not v.selected:
            await interaction.response.send_message("❌ Select at least one reason.",ephemeral=True); return

        # FIX: ack immediately, then build the embed / hit the DB / post it.
        await interaction.response.defer()

        total,rl,sl=_susp_summary(v.selected)
        embed=discord.Embed(title="🚫 player suspension",color=0xED4245)
        embed.add_field(name="Player",value=f"<@{v.target.id}> ({v.target.display_name})",inline=False)
        if rl: embed.add_field(name="Reason(s)",value=rl,inline=False)
        if sl: embed.add_field(name="Additional Status",value=sl,inline=False)
        embed.add_field(name="Total Games Suspended",value=f"**{total} games**",inline=False)
        if v.target.avatar: embed.set_thumbnail(url=v.target.avatar.url)
        embed.set_footer(text=f"Issued by {interaction.user.display_name} | {UFF_FOOTER}")
        embed.timestamp=datetime.utcnow()
        ch=await get_susp_ch(interaction.guild)
        for item in v.children: item.disabled=True
        if ch:
            await ch.send(embed=embed)
            await interaction.edit_original_response(content=f"✅ Posted to {ch.mention}.",view=v)
            nk=[r for r in v.selected if r in SUSPENSION_REASONS]
            sk=[r for r in v.selected if r in SPECIAL_SUSPENSION_REASONS]
            data=await load_data()
            data.setdefault("suspensions",[]).append({
                "player_id":str(v.target.id),"player_name":v.target.display_name,
                "reason_keys":nk,"reasons":[_base_label(r) for r in nk],
                "status_flags":[SPECIAL_SUSPENSION_REASONS[r] for r in sk],
                "total_games":total,"issued_by":str(interaction.user.id),
                "issued_by_name":interaction.user.display_name,
                "date":datetime.utcnow().isoformat(),"cleared":False,
            })
            await save_data(data)
        else:
            await interaction.edit_original_response(content="❌ Suspension channel not found.",view=v)

class SuspCancelBtn(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel",style=discord.ButtonStyle.secondary,row=1)
    async def callback(self,interaction:discord.Interaction):
        v:SuspView=self.view
        for item in v.children: item.disabled=True
        await interaction.response.edit_message(content="❌ Cancelled.",view=v); v.stop()

class SuspView(discord.ui.View):
    def __init__(self,target:discord.Member,issuer_id:int):
        super().__init__(timeout=300)
        self.target=target; self.issuer_id=issuer_id; self.selected=[]
        self.add_item(SuspReasonSelect()); self.add_item(SuspConfirmBtn()); self.add_item(SuspCancelBtn())
    async def interaction_check(self,interaction:discord.Interaction)->bool:
        if interaction.user.id!=self.issuer_id:
            await interaction.response.send_message("❌ Only the issuer can use these.",ephemeral=True); return False
        return True
    async def on_timeout(self):
        for item in self.children: item.disabled=True

# ── BOT SETUP ─────────────────────────────────────────────────────────
intents=discord.Intents.default(); intents.message_content=True; intents.members=True
bot=commands.Bot(command_prefix="!",intents=intents)

@bot.event
async def on_ready():
    await init_db()
    guild_obj=discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    await bot.tree.sync()
    print(f"✅ UFF Bot online — {bot.user}")
    print(f"   Transactions : {TRANSACTIONS_CHANNEL_ID}")
    print(f"   Bloxlink     : {'SET' if BLOXLINK_API_KEY else 'NOT SET'}")
    print(f"   Zevora Logo  : {'SET' if ZEVORA_LOGO_URL else 'NOT SET'}")
    print(f"   Database     : {'SET' if DATABASE_URL else 'NOT SET'}")
    print(f"   Pillow       : {'OK' if _PIL_OK else 'MISSING — pip install Pillow for avatar+logo thumbnails'}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Slash command error: %s", error)
    msg = f"❌ Command failed: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════
# TRANSACTION COMMANDS
# ══════════════════════════════════════════════════════════════════════

@bot.tree.command(name="set_team",description="[Staff] Register a Discord role as a UFF team")
@app_commands.describe(team_role="The team's Discord role (name + icon pulled automatically)")
@app_commands.default_permissions(administrator=True)
async def set_team(interaction:discord.Interaction,team_role:discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("❌ Staff only.",ephemeral=True); return

    team_name=team_role.name
    icon=team_role.display_icon
    logo_url=""
    if icon is not None:
        logo_url=_valid_embed_url(str(icon.url) if hasattr(icon,"url") else "") or ""

    data=await load_data(); rid=str(team_role.id); ex=data["teams"].get(rid,{})
    saved_logo=logo_url or _valid_embed_url(ex.get("logo_url","")) or ""
    data["teams"][rid]={
        "name":team_name,"role_id":rid,
        "head_coach_id":ex.get("head_coach_id"),"head_coach_name":ex.get("head_coach_name"),
        "head_coach_roblox":ex.get("head_coach_roblox",""),
        "ahc_id":ex.get("ahc_id"),"ahc_name":ex.get("ahc_name"),"ahc_roblox":ex.get("ahc_roblox",""),
        "logo_url":saved_logo,
        "emoji":ex.get("emoji",""),
        "roster":ex.get("roster",[]),
        "color":team_role.color.value or UFF_COLOR,
    }
    await save_data(data)
    embed=discord.Embed(title="team registered",color=UFF_COLOR,
        description=f"**{team_name}** registered!\nRole: {team_role.mention} | Transactions → <#{TRANSACTIONS_CHANNEL_ID}>")
    if saved_logo:
        _safe_set_thumbnail(embed, saved_logo)
    else:
        embed.description+="\n\n⚠️ No role icon found. Use `/set_team_image` to set a logo."
    embed.set_footer(text=UFF_FOOTER)
    await interaction.followup.send(embed=embed,ephemeral=True)


@bot.tree.command(name="set_team_image",description="[Staff] Set or override the team logo URL")
@app_commands.describe(team_role="The team's Discord role",logo_url="Direct image URL for the logo")
@app_commands.default_permissions(administrator=True)
async def set_team_image(interaction:discord.Interaction,team_role:discord.Role,logo_url:str):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("❌ Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"❌ {team_role.mention} not registered.",ephemeral=True); return
    clean_url = _valid_embed_url(logo_url)
    if not clean_url:
        await interaction.followup.send(
            "❌ That doesn't look like a valid image URL. Use a direct `https://` link ending in .png, .jpg, or .webp.",
            ephemeral=True,
        ); return
    data["teams"][rid]["logo_url"]=clean_url
    await save_data(data)
    embed=discord.Embed(title="logo updated",color=UFF_COLOR,description=f"Logo for **{data['teams'][rid]['name']}** updated.")
    _safe_set_thumbnail(embed, data["teams"][rid]["logo_url"])
    await interaction.followup.send(embed=embed,ephemeral=True)


@bot.tree.command(name="set_team_emoji",description="[Staff] Set the emoji shown before a team's name in transactions and /coaches")
@app_commands.describe(team_role="The team's Discord role",emoji="The emoji to use (e.g. 🏈 or a custom server emoji)")
@app_commands.default_permissions(administrator=True)
async def set_team_emoji(interaction:discord.Interaction,team_role:discord.Role,emoji:str):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("❌ Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"❌ {team_role.mention} not registered. Use `/set_team` first.",ephemeral=True); return
    data["teams"][rid]["emoji"]=emoji.strip()
    await save_data(data)
    await interaction.followup.send(
        f"✅ Emoji for **{data['teams'][rid]['name']}** set to {emoji}",ephemeral=True)


@bot.tree.command(name="assign_hc",description="[Staff] Assign a head coach to a team")
@app_commands.describe(team_role="The team's Discord role",player="The member to make head coach")
@app_commands.default_permissions(administrator=True)
async def assign_hc(interaction:discord.Interaction,team_role:discord.Role,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("❌ Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"❌ {team_role.mention} not registered.",ephemeral=True); return

    team=data["teams"][rid]
    blox=await bloxlink_lookup(player.id,interaction.guild.id,team=team)
    rbx=blox.get("roblox_username","")

    team.update(
        head_coach_id=str(player.id), head_coach_name=player.name,
        head_coach_roblox=rbx, head_coach_roblox_id=blox.get("roblox_id"),
    )
    roster=team.setdefault("roster",[])
    if str(player.id) not in [r["id"] for r in roster]:
        roster.append({
            "id":str(player.id),"name":player.display_name,"roblox":rbx,
            "roblox_id":blox.get("roblox_id"),"role":"Head Coach",
        })
    else:
        for r in roster:
            if r["id"]==str(player.id): r["role"]="Head Coach"
    await save_data(data)

    hc_role=interaction.guild.get_role(HEAD_COACH_ROLE_ID); rf=False
    if hc_role:
        try: await player.add_roles(hc_role,reason="Assigned HC via /assign_hc")
        except discord.Forbidden: rf=True

    msg=f"✅ **{player.display_name}** is now head coach of **{team['name']}**."
    if rf: msg+=_role_warn("Head Coach")
    await interaction.followup.send(msg,ephemeral=True)


@bot.tree.command(name="offer",description="Send a player a roster offer via DM (12h window)")
@app_commands.describe(player="The player to offer a roster spot to")
async def offer(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        data=await load_data(); rid,team=get_team_for_user(data,interaction.user.id)
        if not team:
            msg="❌ Staff: assign yourself as HC first." if is_staff(interaction) else "❌ You're not HC or AHC of any team."
            await interaction.followup.send(msg,ephemeral=True); return
        if player.id==interaction.user.id:
            await interaction.followup.send("❌ Can't offer yourself.",ephemeral=True); return
        if player.bot:
            await interaction.followup.send("❌ Can't offer a bot.",ephemeral=True); return

        roster=team.setdefault("roster",[])
        if len(roster)>=MAX_ROSTER:
            await interaction.followup.send(f"❌ Roster cap ({MAX_ROSTER}) reached.",ephemeral=True); return
        if str(player.id) in [r["id"] for r in roster]:
            await interaction.followup.send(f"❌ {player.display_name} is already on the team.",ephemeral=True); return

        oid=f"offer_{rid}_{player.id}_{int(datetime.utcnow().timestamp())}"
        team_logo=_team_logo_url(team, interaction.guild) or ""
        team_emoji=team.get("emoji","")
        hc_id=team.get("head_coach_id")
        data.setdefault("offers",{})[oid]={
            "team_role_id":rid,"team_name":team["name"],"player_id":str(player.id),
            "hc_id":hc_id,"timestamp":datetime.utcnow().isoformat(),"guild_id":str(interaction.guild.id),
        }
        await save_data(data)

        hc_rbx=team.get("head_coach_roblox","")
        hc_line=f"<@{hc_id}> `{hc_rbx}`".strip() if hc_id else "*vacant*"
        emoji=team_emoji+" " if team_emoji else ""
        dm=discord.Embed(title=f"offer from the {team['name']}",
            description=f"You've been offered a roster spot on {emoji}**{team['name']}**!",
            color=team.get("color",UFF_COLOR))
        dm.add_field(name="head coach:",value=hc_line,inline=False)
        dm.add_field(name="\u200b",value="You have **12 hours** to accept or ignore.",inline=False)
        thumb=team_logo or _league_thumb(interaction.guild)
        _safe_set_thumbnail(dm, thumb)
        dm.set_footer(text=UFF_FOOTER); dm.timestamp=datetime.utcnow()

        team_role=get_role(interaction.guild,rid)
        view=OfferView(oid,int(rid),team["name"],team_logo,team_emoji,hc_id,player.id,interaction.guild.id)
        try: await player.send(embed=dm,view=view)
        except discord.Forbidden:
            data.get("offers",{}).pop(oid,None); await save_data(data)
            await interaction.followup.send(f"❌ Can't DM **{player.display_name}**.",ephemeral=True); return

        await interaction.followup.send(f"✅ Offer sent to **{player.display_name}** — 12 hours to accept.",ephemeral=True)
    except Exception as e:
        await _cmd_error(interaction, "/offer", e)


@bot.tree.command(name="release",description="Release a player from your team")
@app_commands.describe(player="The player to release")
async def release(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        data=await load_data(); rid,team=get_team_for_user(data,interaction.user.id)
        if not team and not is_staff(interaction):
            await interaction.followup.send("❌ You're not HC or AHC of any team.",ephemeral=True); return
        if not team and is_staff(interaction):
            for r,t in data["teams"].items():
                if str(player.id) in [x["id"] for x in t.get("roster",[])]:
                    rid,team=r,t; break
            if not team:
                await interaction.followup.send(f"❌ {player.display_name} isn't on any registered team.",ephemeral=True); return

        roster=team.setdefault("roster",[])
        before=len(roster)
        team["roster"]=[r for r in roster if r["id"]!=str(player.id)]
        if len(team["roster"])==before:
            await interaction.followup.send(f"❌ {player.display_name} isn't on **{team['name']}**.",ephemeral=True); return

        await save_data(data)

        team_role=get_role(interaction.guild,rid); rf=False
        if team_role and team_role in player.roles:
            try: await player.remove_roles(team_role,reason=f"Released from {team['name']}")
            except discord.Forbidden: rf=True

        embed,file=await build_tx_embed("released",player,team,team_role,interaction.guild,color=0xED4245)
        msg=f"✅ Released **{player.display_name}** from **{team['name']}**."
        if rf: msg+=_role_warn(team["name"])
        await post_tx(interaction.guild,embed,followup=interaction.followup,msg=msg,file=file)
    except Exception as e:
        await _cmd_error(interaction, "/release", e)


@bot.tree.command(name="demand",description="Demand a release from your current team (1 per player lifetime)")
async def demand(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data=await load_data(); uid=str(interaction.user.id)
        found_rid,found_team=None,None
        for r,t in data["teams"].items():
            if uid in [x["id"] for x in t.get("roster",[])]:
                found_rid,found_team=r,t; break
        if not found_team:
            await interaction.followup.send("❌ You aren't on any registered team.",ephemeral=True); return

        extra=data.get("extra_demands",{}).get(uid,0)
        if data.get("demand_used",{}).get(uid,False) and extra<=0:
            await interaction.followup.send(
                "❌ You've already used your demand. Players get **1 demand** lifetime.\nAsk an admin for `/grant_extra_demand`.",
                ephemeral=True); return

        if data.get("demand_used",{}).get(uid,False):
            data["extra_demands"][uid]=extra-1
        else:
            data.setdefault("demand_used",{})[uid]=True

        found_team["roster"]=[r for r in found_team.get("roster",[]) if r["id"]!=uid]
        await save_data(data)

        blox=await bloxlink_lookup(interaction.user.id,interaction.guild.id,team=found_team)
        team_role=get_role(interaction.guild,found_rid); rf=False
        if team_role and team_role in interaction.user.roles:
            try: await interaction.user.remove_roles(team_role,reason=f"Demand release from {found_team['name']}")
            except discord.Forbidden: rf=True

        emoji=found_team.get("emoji",""); prefix=f"{emoji} " if emoji else ""
        role_str=team_role.mention if team_role else f"**{found_team['name']}**"
        embed=discord.Embed(
            description=(
                f"{_player_tx_line(interaction.user, blox)} has demanded a release "
                f"from {prefix}{role_str}!\n\n"
                f"{_info_block(found_team)}"
            ),
            color=0xED4245,
        )
        embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
        file=await _apply_tx_images(embed, blox, found_team, interaction.guild)

        msg=f"✅ Your demand from **{found_team['name']}** has been posted."
        if rf: msg+=_role_warn(found_team["name"])
        await post_tx(interaction.guild,embed,followup=interaction.followup,msg=msg,file=file)
    except Exception as e:
        await _cmd_error(interaction, "/demand", e)


@bot.tree.command(name="grant_extra_demand",description="[Owner] Grant a player an extra demand token")
@app_commands.describe(player="The player",amount="Number of extra demands (default 1)")
async def grant_extra_demand(interaction:discord.Interaction,player:discord.Member,amount:int=1):
    await interaction.response.defer(ephemeral=True)
    if interaction.user.id not in EXTRA_DEMAND_GRANT_USER_IDS and not is_admin(interaction):
        await interaction.followup.send("❌ No permission.",ephemeral=True); return
    data=await load_data(); uid=str(player.id)
    data.setdefault("extra_demands",{})[uid]=data["extra_demands"].get(uid,0)+amount
    await save_data(data)
    await interaction.followup.send(
        f"✅ Granted **{amount}** extra demand(s) to **{player.display_name}**. Total: **{data['extra_demands'][uid]}**.",ephemeral=True)


@bot.tree.command(name="promote_coach",description="Promote a player to Assistant Head Coach")
@app_commands.describe(player="The player to promote")
async def promote_coach(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        data=await load_data(); rid,team=get_team_for_hc(data,interaction.user.id)
        if not team and is_staff(interaction):
            for r,t in data["teams"].items():
                if str(player.id) in [x["id"] for x in t.get("roster",[])]:
                    rid,team=r,t; break
        if not team:
            await interaction.followup.send("❌ You're not HC of any team. Only HCs and staff can promote.",ephemeral=True); return
        roster=team.setdefault("roster",[])
        if str(player.id) not in [r["id"] for r in roster]:
            await interaction.followup.send(f"❌ {player.display_name} must be on the roster first.",ephemeral=True); return

        blox=await bloxlink_lookup(player.id,interaction.guild.id,team=team)
        team.update(
            ahc_id=str(player.id), ahc_name=player.name,
            ahc_roblox=blox.get("roblox_username",""), ahc_roblox_id=blox.get("roblox_id"),
        )
        for r in roster:
            if r["id"]==str(player.id): r["role"]="Assistant Head Coach"
        await save_data(data)

        ahc_role=interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID)
        hc_role=interaction.guild.get_role(HEAD_COACH_ROLE_ID); rf=False
        try:
            if ahc_role: await player.add_roles(ahc_role,reason="Promoted to AHC")
            if hc_role and hc_role in player.roles: await player.remove_roles(hc_role,reason="Promoted to AHC")
        except discord.Forbidden: rf=True

        team_role=get_role(interaction.guild,rid)
        embed,file=await build_coach_embed(
            "assistant coach promotion",player,team,team_role,interaction.guild,
            color=team.get("color",UFF_COLOR), blox=blox
        )
        msg=f"✅ **{player.display_name}** promoted to AHC of **{team['name']}**."
        if rf: msg+=_role_warn("Assistant Coach")
        await post_tx(interaction.guild,embed,followup=interaction.followup,msg=msg,file=file)
    except Exception as e:
        await _cmd_error(interaction, "/promote_coach", e)


@bot.tree.command(name="demote_coach",description="Demote the Assistant Head Coach back to player")
@app_commands.describe(player="The AHC to demote")
async def demote_coach(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        data=await load_data(); rid,team=get_team_for_hc(data,interaction.user.id)
        if not team and is_staff(interaction):
            for r,t in data["teams"].items():
                if t.get("ahc_id")==str(player.id): rid,team=r,t; break
        if not team:
            await interaction.followup.send("❌ You're not HC of any team.",ephemeral=True); return
        if team.get("ahc_id")!=str(player.id):
            await interaction.followup.send(f"❌ {player.display_name} is not the AHC of **{team['name']}**.",ephemeral=True); return

        team.update(ahc_id=None,ahc_name=None,ahc_roblox="")
        for r in team.get("roster",[]):
            if r["id"]==str(player.id): r["role"]="Player"
        await save_data(data)

        ahc_role=interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID); rf=False
        try:
            if ahc_role and ahc_role in player.roles: await player.remove_roles(ahc_role,reason="Demoted from AHC")
        except discord.Forbidden: rf=True

        blox=await bloxlink_lookup(player.id,interaction.guild.id,team=team)
        team_role=get_role(interaction.guild,rid)
        embed,file=await build_coach_embed(
            "assistant coach demotion",player,team,team_role,interaction.guild,
            color=0xED4245, blox=blox
        )
        msg=f"✅ **{player.display_name}** demoted from AHC of **{team['name']}**."
        if rf: msg+=_role_warn("Assistant Coach")
        await post_tx(interaction.guild,embed,followup=interaction.followup,msg=msg,file=file)
    except Exception as e:
        await _cmd_error(interaction, "/demote_coach", e)


@bot.tree.command(name="disband",description="Disband a team — removes all players and coaches")
@app_commands.describe(confirm="Type DISBAND to confirm",
                        team_role="(Staff only) Target team — HCs don't need this")
async def disband(interaction:discord.Interaction,confirm:str,team_role:discord.Role=None):
    await interaction.response.defer(ephemeral=True)
    if confirm.upper()!="DISBAND":
        await interaction.followup.send("❌ Type `DISBAND` exactly.",ephemeral=True); return
    data=await load_data(); rid,team=get_team_for_hc(data,interaction.user.id)
    if not team and is_staff(interaction) and team_role:
        rid=str(team_role.id); team=get_team_by_role(data,team_role.id)
    if not team:
        await interaction.followup.send("❌ Not HC of any team. Staff must also provide team_role.",ephemeral=True); return

    team_name=team["name"]; former=list(team.get("roster",[])); former_size=len(former)
    team.update(roster=[],head_coach_id=None,head_coach_name=None,head_coach_roblox="",
                ahc_id=None,ahc_name=None,ahc_roblox="")
    await save_data(data)

    tr=get_role(interaction.guild,rid); fail=0
    if tr:
        for md in former:
            try:
                m=interaction.guild.get_member(int(md["id"])) or await interaction.guild.fetch_member(int(md["id"]))
                if tr in m.roles: await m.remove_roles(tr,reason=f"{team_name} disbanded")
            except discord.Forbidden: fail+=1
            except: pass

    embed=discord.Embed(title="team disbanded",color=0xED4245,
        description=f"**{team_name}** has been disbanded.\nAll **{former_size}** players and coaches removed.")
    if tr: embed.add_field(name="Team",value=tr.mention,inline=True)
    logo=team.get("logo_url","") or _league_thumb(interaction.guild)
    if logo: embed.set_thumbnail(url=logo)
    embed.set_footer(text=f"Disbanded by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp=datetime.utcnow()
    note=f"\n⚠️ Couldn't strip team role from {fail} member(s)." if fail else ""
    ch=await get_tx_ch(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        await interaction.followup.send(f"✅ **{team_name}** disbanded. Posted to {ch.mention}.{note}",ephemeral=True)
    else:
        await interaction.followup.send(embed=embed,ephemeral=False)


@bot.tree.command(name="roster",description="View a team's current roster")
@app_commands.describe(team_role="The team's Discord role")
async def roster_cmd(interaction:discord.Interaction,team_role:discord.Role):
    await interaction.response.defer(ephemeral=True)
    try:
        data=await load_data(); team=get_team_by_role(data,team_role.id)
        if not team:
            await interaction.followup.send(f"❌ {team_role.mention} isn't registered.",ephemeral=True); return

        roster=team.get("roster",[]); color=team.get("color",UFF_COLOR)
        emoji=team.get("emoji",""); prefix=f"{emoji} " if emoji else ""
        embed=discord.Embed(title=f"{prefix}{team['name'].lower()} roster",
                            description=f"> roster: **{len(roster)}/{MAX_ROSTER}**",color=color)

        hc_id=team.get("head_coach_id")
        if hc_id:
            rbx=f" `{team.get('head_coach_roblox','')}`" if team.get("head_coach_roblox") else ""
            embed.add_field(name="head coach:",
                value=f"> <@{hc_id}> (@{team.get('head_coach_name','')}) ✓{rbx}",inline=False)

        ahc_lines=[]; pl_lines=[]
        for r in roster:
            rbx=f" `{r['roblox']}`" if r.get("roblox") else ""
            line=f"> <@{r['id']}> (@{r['name']}) ✓{rbx}"
            role=r.get("role","Player")
            if role=="Assistant Head Coach": ahc_lines.append(line)
            elif role!="Head Coach": pl_lines.append(line)

        if ahc_lines: embed.add_field(name="assistant head coach:",value="\n".join(ahc_lines),inline=False)
        if pl_lines:  embed.add_field(name="players:",value="\n".join(pl_lines),inline=False)
        elif not roster: embed.add_field(name="players:",value="> *No players yet.*",inline=False)

        logo=_team_logo_url(team, interaction.guild) or _league_thumb(interaction.guild)
        _safe_set_thumbnail(embed, logo)
        embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
        await interaction.followup.send(embed=embed,ephemeral=True)
    except Exception as e:
        await _cmd_error(interaction, "/roster", e)


@bot.tree.command(name="coaches",description="View all head coaches across the league")
async def coaches_cmd(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); teams=data.get("teams",{})
    embed=discord.Embed(title="head coaches",color=UFF_COLOR)

    if not teams:
        embed.description="*No teams registered yet.*"
    else:
        lines=[]
        for rid,team in teams.items():
            hc_id=team.get("head_coach_id")
            hc_name=team.get("head_coach_name","")
            hc_rbx=team.get("head_coach_roblox","")
            team_emoji=team.get("emoji","")

            if hc_id:
                rbx_str=f" `{hc_rbx}`" if hc_rbx else ""
                hc_str=f"<@{hc_id}> (@{hc_name}) ✓{rbx_str}"
            else:
                hc_str="*vacant*"

            tr=get_role(interaction.guild,rid)
            role_mention=tr.mention if tr else team["name"]
            emoji_prefix=f"{team_emoji} " if team_emoji else ""
            lines.append(f"{emoji_prefix}{role_mention} — {hc_str}")

        chunks=[]; cur=""
        for line in lines:
            candidate=f"{cur}\n{line}" if cur else line
            if len(candidate)>1000:
                chunks.append(cur); cur=line
            else:
                cur=candidate
        if cur: chunks.append(cur)
        for chunk in (chunks or ["*None*"]):
            embed.add_field(name="\u200b",value=chunk,inline=False)

    thumb=_league_thumb(interaction.guild)
    _safe_set_thumbnail(embed, thumb)
    embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
    await interaction.followup.send(embed=embed,ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# PICKUP COMMANDS
# ══════════════════════════════════════════════════════════════════════

@bot.tree.command(name="pickup_ranked",description="Challenge another player to a ranked UFF pickup")
@app_commands.describe(opponent="The player to challenge",game_link="Roblox game link",
                        your_team="Your team name",opponent_team="Opponent's team name")
async def pickup_ranked(interaction:discord.Interaction,opponent:discord.Member,
                        game_link:str,your_team:str,opponent_team:str):
    await interaction.response.defer(ephemeral=True)
    if not ({r.id for r in interaction.user.roles}&PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.followup.send("❌ Missing required role.",ephemeral=True); return
    if opponent.id==interaction.user.id:
        await interaction.followup.send("❌ Can't challenge yourself!",ephemeral=True); return
    if opponent.bot:
        await interaction.followup.send("❌ Can't challenge a bot!",ephemeral=True); return

    data=await load_data(); cd,remaining=on_cooldown(data,interaction.user.id)
    if cd:
        e=discord.Embed(title="⏳ cooldown active",
            description=f"Challenge again in **{remaining}**. Cooldown: `{COOLDOWN_MINUTES} min`.",color=0xE84040)
        e.set_footer(text=UFF_FOOTER)
        await interaction.followup.send(embed=e,ephemeral=True); return

    p1=get_player(data,interaction.user.id); p1["username"]=interaction.user.display_name
    p2=get_player(data,opponent.id); p2["username"]=opponent.display_name
    mid=f"{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("pending",{})[mid]={
        "challenger_id":str(interaction.user.id),"opponent_id":str(opponent.id),
        "challenger_name":interaction.user.display_name,"opponent_name":opponent.display_name,
        "challenger_team":your_team,"opponent_team":opponent_team,"game_link":game_link,
        "timestamp":datetime.utcnow().isoformat(),"match_id":mid,"guild_id":interaction.guild.id
    }
    await save_data(data)

    r1,e1,_=get_rank(p1["elo"]); r2,e2,_=get_rank(p2["elo"])
    lt=_league_thumb(interaction.guild)
    dm=discord.Embed(title="🏈 Ranked Pickup Challenge!",
        description=f"**{interaction.user.display_name}** wants a ranked pickup. Expires **30 min**.",color=UFF_COLOR)
    dm.add_field(name=f"🟡 {interaction.user.display_name}",
        value=f"`{interaction.user.name}`\n**{your_team}**\nRank: `{e1} {r1}`",inline=True)
    dm.add_field(name="\u200b",value="**— VS —**",inline=True)
    dm.add_field(name=f"🔵 {opponent.display_name}",
        value=f"`{opponent.name}`\n**{opponent_team}**\nRank: `{e2} {r2}`",inline=True)
    dm.add_field(name="🔗 Game Link",value=f"[Click here →]({game_link})",inline=False)
    if UFF_BANNER: dm.set_image(url=UFF_BANNER)
    if lt: dm.set_thumbnail(url=lt)
    dm.set_footer(text=f"Challenge by {interaction.user.display_name} | {UFF_FOOTER}")
    dm.timestamp=datetime.utcnow()

    view=RankedPickupView(mid,interaction.user.id,opponent.id,interaction.user.display_name,
        opponent.display_name,your_team,opponent_team,game_link,interaction.guild.id)
    try: await opponent.send(embed=dm,view=view)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ Can't DM **{opponent.display_name}**.",ephemeral=True)
        data.get("pending",{}).pop(mid,None); await save_data(data); return

    ack=discord.Embed(title="📨 Challenge Sent!",
        description=f"Challenge sent to **{opponent.display_name}**. Posted publicly only if accepted.",color=0x57F287)
    ack.set_footer(text=f"{UFF_FOOTER} • 30-minute window")
    await interaction.followup.send(embed=ack,ephemeral=True)


@bot.tree.command(name="pickup_casual",description="Challenge to a casual pickup — no ELO")
@app_commands.describe(opponent="Player to challenge",game_link="Roblox game link",
                        your_team="Your team",opponent_team="Opponent's team")
async def pickup_casual(interaction,opponent:discord.Member,game_link:str,your_team:str,opponent_team:str):
    await _run_casual(interaction,opponent,game_link,your_team,opponent_team)

@bot.tree.command(name="casual_pickup",description="Challenge to a casual pickup — no ELO")
@app_commands.describe(opponent="Player to challenge",game_link="Roblox game link",
                        your_team="Your team",opponent_team="Opponent's team")
async def casual_pickup(interaction,opponent:discord.Member,game_link:str,your_team:str,opponent_team:str):
    await _run_casual(interaction,opponent,game_link,your_team,opponent_team)


@bot.tree.command(name="pickup_results",description="Submit ranked pickup results + screenshot")
@app_commands.describe(winner="Who won?",winner_score="Winner's score",
                        loser_score="Loser's score",screenshot="Scoreboard screenshot")
async def pickup_results(interaction:discord.Interaction,winner:discord.Member,
                         winner_score:int,loser_score:int,screenshot:discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); uid=str(interaction.user.id)
    pending=data.get("pending",{}); match,mkey=None,None
    for k in sorted(pending,key=lambda k:pending[k].get("timestamp",""),reverse=True):
        m=pending[k]
        if m["challenger_id"]==uid or m["opponent_id"]==uid:
            match,mkey=m,k; break
    if not match:
        await interaction.followup.send("❌ No pending ranked pickup. Use `/pickup_ranked` first.",ephemeral=True); return

    c_id=int(match["challenger_id"]); o_id=int(match["opponent_id"])
    if winner.id not in [c_id,o_id]:
        await interaction.followup.send("❌ Winner must be one of the two players.",ephemeral=True); return

    loser_id=o_id if winner.id==c_id else c_id
    loser_name=match["opponent_name"] if winner.id==c_id else match["challenger_name"]
    wp=get_player(data,winner.id); lp=get_player(data,loser_id)
    wp["username"]=winner.display_name
    old_w,old_l=wp["elo"],lp["elo"]
    wp["elo"]+=WIN_ELO; lp["elo"]=max(0,lp["elo"]-LOSS_ELO)
    wp["wins"]+=1; lp["losses"]+=1
    now=datetime.utcnow().isoformat()
    wp["last_game"]=now; lp["last_game"]=now
    data.setdefault("matches",[]).append({
        "winner_id":str(winner.id),"winner_name":winner.display_name,
        "loser_id":str(loser_id),"loser_name":loser_name,
        "winner_score":winner_score,"loser_score":loser_score,
        "challenger_team":match["challenger_team"],"opponent_team":match["opponent_team"],"date":now
    })
    data.get("pending",{}).pop(mkey,None); await save_data(data)

    wr,we,wcolor=get_rank(wp["elo"]); lr,le,_=get_rank(lp["elo"])
    lt=_league_thumb(interaction.guild)
    embed=discord.Embed(title="🏆 pickup results",color=wcolor)
    embed.add_field(name="🏆 Winner",
        value=f"<@{winner.id}> **{winner.display_name}**\n> Score: **{winner_score}**\n> ELO: `{old_w}` → `{wp['elo']}` **(+{WIN_ELO})**\n> Rank: `{we} {wr}`",inline=True)
    embed.add_field(name="❌ Loser",
        value=f"<@{loser_id}> **{loser_name}**\n> Score: **{loser_score}**\n> ELO: `{old_l}` → `{lp['elo']}` **(-{LOSS_ELO})**\n> Rank: `{le} {lr}`",inline=True)
    embed.add_field(name="📊 Final Score",
        value=f"**{winner.display_name}** `{winner_score} — {loser_score}` **{loser_name}**",inline=False)
    embed.set_image(url=screenshot.url)
    if lt: embed.set_thumbnail(url=lt)
    embed.set_footer(text=f"{UFF_FOOTER} • Submitted by {interaction.user.display_name}")
    embed.timestamp=datetime.utcnow()

    ch=await get_pickup_ch(interaction.guild)
    target=ch if (ch and ch.id!=interaction.channel_id) else interaction.channel
    await interaction.followup.send("✅ Results posted!",ephemeral=True)
    await target.send(embed=embed)


@bot.tree.command(name="pickup_profile",description="View UFF pickup rank and stats")
@app_commands.describe(player="Player to look up (blank = yourself)")
async def pickup_profile(interaction:discord.Interaction,player:discord.Member=None):
    await interaction.response.defer(ephemeral=True)
    target=player or interaction.user
    data=await load_data(); p=get_player(data,target.id); elo=p["elo"]
    rank,emoji,color=get_rank(elo); gp=p["wins"]+p["losses"]
    wr=f"{p['wins']/gp*100:.1f}%" if gp else "N/A"
    embed=discord.Embed(title=f"{emoji} {target.display_name}",color=color)
    embed.add_field(name="Rank",value=f"`{emoji} {rank}`",inline=True)
    embed.add_field(name="ELO",value=f"`{elo}`",inline=True)
    embed.add_field(name="Wins",value=f"`{p['wins']}`",inline=True)
    embed.add_field(name="Losses",value=f"`{p['losses']}`",inline=True)
    embed.add_field(name="Win Rate",value=f"`{wr}`",inline=True)
    if target.avatar: embed.set_thumbnail(url=target.avatar.url)
    embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
    await interaction.followup.send(embed=embed,ephemeral=True)


@bot.tree.command(name="pickup_leaderboard",description="UFF pickup ELO leaderboard")
async def pickup_leaderboard(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); players=data.get("players",{})
    if not players:
        await interaction.followup.send("No players yet.",ephemeral=True); return
    top=sorted(players.items(),key=lambda x:x[1]["elo"],reverse=True)[:15]
    medals=["🥇","🥈","🥉"]
    lines=[f"{medals[i] if i<3 else f'`{i+1}.`'} **{p.get('username') or f'<@{uid}>'}** — {emoji} `{rank}` | ELO `{p['elo']}` | {p['wins']}W {p['losses']}L"
           for i,(uid,p) in enumerate(top) for rank,emoji,_ in [get_rank(p["elo"])]]
    embed=discord.Embed(title="UFF Pickup — ELO Leaderboard",description="\n".join(lines),color=UFF_COLOR)
    lt=_league_thumb(interaction.guild)
    if lt: embed.set_thumbnail(url=lt)
    embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
    await interaction.followup.send(embed=embed,ephemeral=True)


@bot.tree.command(name="match_history",description="View recent UFF pickup results")
async def match_history(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); matches=list(reversed(data.get("matches",[])))[:10]
    if not matches:
        await interaction.followup.send("No matches yet.",ephemeral=True); return
    lines=[f"🏆 **{m['winner_name']}** `{m.get('winner_score','?')}–{m.get('loser_score','?')}` {m['loser_name']}" for m in matches]
    embed=discord.Embed(title="📋 UFF Pickup — Recent Results",description="\n".join(lines),color=0x4090E8)
    embed.set_footer(text=f"{UFF_FOOTER} • Last 10 matches"); embed.timestamp=datetime.utcnow()
    await interaction.followup.send(embed=embed,ephemeral=True)


@bot.tree.command(name="teams",description="View all 20 UFF league teams")
async def teams_cmd(interaction:discord.Interaction):
    embed=discord.Embed(title="United Flag Football — All Teams",color=UFF_COLOR)
    embed.add_field(name="Teams 1–10",value="\n".join(f"`{t[1]}` **{t[0]}**" for t in TEAMS[:10]),inline=True)
    embed.add_field(name="Teams 11–20",value="\n".join(f"`{t[1]}` **{t[0]}**" for t in TEAMS[10:]),inline=True)
    lt=_league_thumb(interaction.guild)
    if lt: embed.set_thumbnail(url=lt)
    embed.set_footer(text=f"{UFF_FOOTER} • 20 teams")
    await interaction.response.send_message(embed=embed,ephemeral=True)


# ── ADMIN ─────────────────────────────────────────────────────────────
@bot.tree.command(name="reset_player",description="[Admin] Reset a player's ELO to 900")
@app_commands.describe(player="Player to reset")
@app_commands.default_permissions(administrator=True)
async def reset_player(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send("❌ Admin only.",ephemeral=True); return
    data=await load_data()
    data["players"][str(player.id)]={"elo":STARTING_ELO,"wins":0,"losses":0,"last_game":None,"username":player.display_name}
    await save_data(data)
    await interaction.followup.send(f"✅ Reset **{player.display_name}** ELO to `{STARTING_ELO}`.",ephemeral=True)

@bot.tree.command(name="adjust_elo",description="[Admin] Manually adjust a player's ELO")
@app_commands.describe(player="Target player",amount="ELO to add (negative to subtract)")
@app_commands.default_permissions(administrator=True)
async def adjust_elo(interaction:discord.Interaction,player:discord.Member,amount:int):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send("❌ Admin only.",ephemeral=True); return
    data=await load_data(); p=get_player(data,player.id); old=p["elo"]
    p["elo"]=max(0,p["elo"]+amount); p["username"]=player.display_name
    await save_data(data)
    sign="+" if amount>=0 else ""
    await interaction.followup.send(f"✅ **{player.display_name}** ELO: `{old}` → `{p['elo']}` ({sign}{amount})",ephemeral=True)

@bot.tree.command(name="clear_cooldown",description="[Admin] Clear a player's cooldown")
@app_commands.describe(player="Player to clear")
@app_commands.default_permissions(administrator=True)
async def clear_cooldown(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send("❌ Admin only.",ephemeral=True); return
    data=await load_data(); get_player(data,player.id)["last_game"]=None; await save_data(data)
    await interaction.followup.send(f"✅ Cleared cooldown for **{player.display_name}**.",ephemeral=True)


# ── SUSPENSIONS ───────────────────────────────────────────────────────
@bot.tree.command(name="suspension",description="[Staff] Issue a suspension to a player")
@app_commands.describe(player="The player to suspend")
async def suspension(interaction:discord.Interaction,player:discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message("❌ No permission to issue suspensions.",ephemeral=True); return
    view=SuspView(target=player,issuer_id=interaction.user.id)
    await interaction.response.send_message(
        content=f"**Issuing suspension for {player.display_name}**\nSelect up to {MAX_SUSPENSION_REASONS} reasons.",
        view=view,ephemeral=True)

@bot.tree.command(name="unsuspend",description="[Staff] Clear a player's suspension")
@app_commands.describe(player="The player to unsuspend")
async def unsuspend(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not can_issue_suspension(interaction):
        await interaction.followup.send("❌ No permission to clear suspensions.",ephemeral=True); return
    data=await load_data(); cleared=False
    for s in data.get("suspensions",[]):
        if s.get("player_id")==str(player.id) and not s.get("cleared",False):
            s.update(cleared=True,cleared_by=str(interaction.user.id),
                     cleared_by_name=interaction.user.display_name,
                     cleared_date=datetime.utcnow().isoformat())
            cleared=True
    await save_data(data)
    embed=discord.Embed(title="player unsuspended",color=0x57F287)
    embed.add_field(name="Player",value=f"<@{player.id}> ({player.display_name})",inline=False)
    embed.add_field(name="Status",value="**Cleared** — eligible to play",inline=False)
    if player.avatar: embed.set_thumbnail(url=player.avatar.url)
    embed.set_footer(text=f"Cleared by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp=datetime.utcnow()
    ch=await get_susp_ch(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        note="" if cleared else "\n*(No open records found, but notice posted anyway.)*"
        await interaction.followup.send(f"✅ {player.display_name} unsuspended. Posted to {ch.mention}.{note}",ephemeral=True)
    else:
        await interaction.followup.send("❌ Suspension channel not found.",ephemeral=True)


@bot.tree.command(name="help_uff",description="UFF bot command guide")
async def help_uff(interaction:discord.Interaction):
    embed=discord.Embed(title="📖 United Flag Football — Commands",color=UFF_COLOR)
    embed.add_field(name="📋 Transactions",value=(
        "`/set_team` — [Staff] Register a team role\n"
        "`/set_team_image` — [Staff] Set team logo URL\n"
        "`/set_team_emoji` — [Staff] Set team emoji for transactions/coaches\n"
        "`/assign_hc` — [Staff] Assign a head coach\n"
        "`/offer` — [HC/AHC] Send a DM roster offer (12h)\n"
        "`/release` — [HC/AHC/Staff] Release a player\n"
        "`/demand` — Demand your own release (1 lifetime)\n"
        "`/grant_extra_demand` — [Owner] Grant extra demand token\n"
        "`/promote_coach` — [HC/Staff] Promote to AHC\n"
        "`/demote_coach` — [HC/Staff] Demote AHC\n"
        "`/disband` — [HC/Staff] Disband a team\n"
        "`/roster` — View a team's roster\n"
        "`/coaches` — View all head coaches"
    ),inline=False)
    embed.add_field(name="⚔️ Ranked Pickup",value=(
        "`/pickup_ranked` — Start a ranked pickup\n"
        "`/pickup_results` — Submit results + screenshot\n"
        "`/pickup_profile` — ELO, rank & stats\n"
        "`/pickup_leaderboard` — Top 15 ELO\n"
        "`/match_history` — Last 10 results"
    ),inline=False)
    embed.add_field(name="🎮 Casual",value="`/pickup_casual` or `/casual_pickup`",inline=False)
    embed.add_field(name="🏟️ League",value="`/teams` — All 20 UFF teams",inline=False)
    embed.add_field(name="🛡️ Admin",value="`/reset_player` · `/adjust_elo` · `/clear_cooldown`",inline=False)
    embed.add_field(name="🚫 Suspensions",value="`/suspension` · `/unsuspend`",inline=False)
    embed.add_field(name="📊 Ranks",value=(
        "**Start:** 900 ELO | **Win:** +100 | **Loss:** −100\n"
        "⚙️ Iron I/II/III → 0/700/900\n"
        "🥇 Gold I/II/III → 1100/1300/1500\n"
        "💎 Amethyst I/II/III → 1700/1900/2100"
    ),inline=False)
    lt=_league_thumb(interaction.guild)
    if lt: embed.set_thumbnail(url=lt)
    embed.set_footer(text=f"{UFF_FOOTER} • {COOLDOWN_MINUTES}-min ranked cooldown")
    await interaction.response.send_message(embed=embed,ephemeral=True)


if __name__=="__main__":
    bot.run(TOKEN)
