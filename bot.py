"""
UFF Discord Bot v6.1
"""
import re, asyncio, discord, json, os, aiohttp, asyncpg
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

TOKEN            = os.getenv("DISCORD_BOT_TOKEN","")
OWNER_ID         = int(os.getenv("OWNER_DISCORD_ID","0"))
QBB_CHANNEL_ID   = int(os.getenv("QBB_CHANNEL_ID","0"))
BLOXLINK_API_KEY = os.getenv("BLOXLINK_API_KEY","")
DATABASE_URL     = os.getenv("DATABASE_URL","")
UFF_THUMBNAIL    = os.getenv("UFF_THUMBNAIL_URL","")
UFF_BANNER       = os.getenv("UFF_BANNER_URL","")
ZEVORA_LOGO_URL  = os.getenv("ZEVORA_LOGO_URL","")

TRANSACTIONS_CHANNEL_ID = 1262200420151984152
STREAM_CHANNEL_ID       = 1402467692660789259
COACHES_CHANNEL_ID      = 1274811643338948618

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
SUSPENSION_ROLE_ID      = 1262200419564785759

PICKUP_ALLOWED_ROLE_IDS = {1269693904815521994,1404271074623099040,1404271002241728617,
    1429344923865448550,1262200419686285342,1401450124424642561}
STAFF_ROLE_IDS = {1404271002241728617,1429344923865448550,1262200419686285342,
    1401450124424642561,1499141732108079225,1434653599236882574,1502941495722770472}
SUSPENSION_CHANNEL_ID       = 1364423515532427264
SUSPENSION_ALLOWED_USER_IDS = {1414340980110528546,1055321446978691112}
SUSPENSION_ALLOWED_ROLE_IDS = {1513234210054344925,1499141732108079225}
MAX_SUSPENSION_REASONS      = 9

SUSPENSION_REASONS = {
    "exploiting_x1":             ("Exploiting",                24),
    "dodging_screenshare":       ("Dodging Screenshare",        6),
    "Leaking":                   ("Leaking",                    8),
    "possession_of_exploits_x1": ("Possession of Exploits",    12),
    "possession_of_exploits_x2": ("Possession of Exploits",    12),
    "possession_of_exploits_x3": ("Possession of Exploits",    12),
    "possession_of_exploits_x4": ("Possession of Exploits",    12),
    "gameplay_manipulation":     ("Gameplay Manipulation",      8),
    "alting_x1":                 ("Alting",                    12),
    "alting_x2":                 ("Alting",                    12),
    "alting_x3":                 ("Alting",                    12),
    "alting_x4":                 ("Alting",                    12),
    "disbanding":                ("Disbanding",                 4),
    "distributing_exploits_x1":  ("Distributing Exploits",     60),
    "distributing_alts_x1":      ("Distributing Alt Accounts", 35),
    "framing_x1":                ("Framing",                   12),
    "obstruction_of_justice_x1": ("Obstruction of Justice",     8),
}
SPECIAL_SUSPENSION_REASONS  = {"ineligible_until_ss":"Ineligible Until Screenshare"}
EXTRA_DEMAND_GRANT_USER_IDS = {1055321446978691112,391036854084042762}

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
    if _db_pool is None or getattr(_db_pool,"_closed",False):
        _db_pool = await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=5)
    return _db_pool

async def init_db():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS uff_store (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

_STORE_KEYS = ["players","matches","pending","casual_pending","suspensions","teams","demand_used","extra_demands","offers"]

async def load_data():
    global _db_pool
    for attempt in range(2):
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT key,value FROM uff_store WHERE key=ANY($1::text[])",_STORE_KEYS)
            found = {r["key"]:json.loads(r["value"]) for r in rows}
            return {k:found.get(k,[] if k in ("matches","suspensions") else {}) for k in _STORE_KEYS}
        except Exception as e:
            print(f"[DB] load_data error (attempt {attempt+1}): {e}"); _db_pool=None
            if attempt==1: raise

async def save_data(data):
    global _db_pool
    for attempt in range(2):
        try:
            pool = await get_db()
            rows = [(k,json.dumps(v,default=str)) for k,v in data.items()]
            async with pool.acquire() as conn:
                await conn.executemany("INSERT INTO uff_store(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",rows)
            return
        except Exception as e:
            print(f"[DB] save_data error (attempt {attempt+1}): {e}"); _db_pool=None
            if attempt==1: raise

# ── HELPERS ───────────────────────────────────────────────────────────
def get_player(data,uid):
    k=str(uid)
    if k not in data["players"]:
        data["players"][k]={"elo":STARTING_ELO,"wins":0,"losses":0,"last_game":None,"username":""}
    return data["players"][k]

def get_rank(elo):
    if elo>=2100: return "Amethyst III","💎",0xA040E8
    if elo>=1900: return "Amethyst II", "💎",0xA040E8
    if elo>=1700: return "Amethyst I",  "💎",0xA040E8
    if elo>=1500: return "Gold III",    "🥇",0xF0C040
    if elo>=1300: return "Gold II",     "🥇",0xF0C040
    if elo>=1100: return "Gold I",      "🥇",0xF0C040
    if elo>=900:  return "Iron III",    "⚙️",0x8090A0
    if elo>=700:  return "Iron II",     "⚙️",0x8090A0
    return "Iron I","⚙️",0x8090A0

def on_cooldown(data,uid):
    p=get_player(data,uid)
    if not p["last_game"]: return False,None
    diff=(datetime.fromisoformat(p["last_game"])+timedelta(minutes=COOLDOWN_MINUTES)-datetime.utcnow())
    if diff.total_seconds()>0:
        return True,f"{int(diff.total_seconds()//60)}m {int(diff.total_seconds()%60)}s"
    return False,None

def is_admin(i): return i.user.id==OWNER_ID or i.user.guild_permissions.administrator
def is_staff(i):
    if is_admin(i): return True
    return bool({r.id for r in i.user.roles}&STAFF_ROLE_IDS)
def can_issue_suspension(i):
    if i.user.id in SUSPENSION_ALLOWED_USER_IDS: return True
    return bool({r.id for r in i.user.roles}&SUSPENSION_ALLOWED_ROLE_IDS) or is_admin(i)

def _league_thumb(guild):
    return ZEVORA_LOGO_URL or UFF_THUMBNAIL or (str(guild.icon.url) if guild and guild.icon else "")

def _role_warn(label):
    return f"\n⚠️ Could not update **{label}** role — check bot role position and Manage Roles permission."

async def get_ch(guild,ch_id):
    if not ch_id: return None
    ch=guild.get_channel(ch_id)
    if ch: return ch
    try: return await guild.fetch_channel(ch_id)
    except: return None

async def get_pickup_ch(guild): return await get_ch(guild,QBB_CHANNEL_ID)
async def get_susp_ch(guild):   return await get_ch(guild,SUSPENSION_CHANNEL_ID)
async def get_tx_ch(guild):     return await get_ch(guild,TRANSACTIONS_CHANNEL_ID)
async def get_stream_ch(guild): return await get_ch(guild,STREAM_CHANNEL_ID)

def _expire_pending(data):
    cutoff=datetime.utcnow()-timedelta(minutes=COOLDOWN_MINUTES)
    for k in [k for k,m in data.get("pending",{}).items() if _ts(m.get("timestamp"))<cutoff]:
        data["pending"].pop(k,None)

def _expire_casual(data):
    cutoff=datetime.utcnow()-timedelta(minutes=COOLDOWN_MINUTES)
    for k in [k for k,m in data.get("casual_pending",{}).items() if _ts(m.get("timestamp"))<cutoff]:
        data["casual_pending"].pop(k,None)

def _ts(iso):
    try: return datetime.fromisoformat(iso)
    except: return datetime.min

# ── NAME / EMOJI HELPERS ──────────────────────────────────────────────
def get_verified_emoji(guild) -> str:
    if guild:
        for e in guild.emojis:
            if e.name.lower()=="verified":
                return str(e)
    return "✅"

def _last_word(name:str) -> str:
    words=name.strip().split()
    return words[-1] if words else name

def _clean_thread_name(text:str) -> str:
    cleaned=re.sub(r"<a?:[^:>]+:\d+>","",text)
    return " ".join(cleaned.split())[:100]

# ── BLOXLINK ──────────────────────────────────────────────────────────
async def bloxlink_lookup(discord_id,guild_id):
    if not BLOXLINK_API_KEY: return {"error":"BLOXLINK_API_KEY not set"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}",
                headers={"Authorization":BLOXLINK_API_KEY},timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status!=200: return {"error":f"Bloxlink HTTP {r.status}"}
                body=await r.json()
        rid=body.get("robloxID")
        if not rid: return {"error":"No Roblox account linked"}
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://users.roblox.com/v1/users/{rid}",timeout=aiohttp.ClientTimeout(total=8)) as r:
                udata=await r.json() if r.status==200 else {}
        username=udata.get("name",str(rid))
        avatar_url=""
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={rid}&size=150x150&format=Png&isCircular=false",
                timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status==200:
                    items=(await r.json()).get("data",[])
                    avatar_url=items[0].get("imageUrl","") if items else ""
        return {"roblox_username":username,"roblox_id":int(rid),"avatar_url":avatar_url}
    except Exception as e: return {"error":str(e)}

# ── TEAM HELPERS ──────────────────────────────────────────────────────
def get_team_by_role(data,role_id): return data["teams"].get(str(role_id))

def get_team_for_hc(data,uid):
    s=str(uid)
    for rid,t in data["teams"].items():
        if t.get("head_coach_id")==s: return rid,t
    return None,None

def get_team_for_user(data,uid,member=None):
    s=str(uid)
    for rid,t in data["teams"].items():
        if t.get("head_coach_id")==s: return rid,t
    for rid,t in data["teams"].items():
        if t.get("ahc_id")==s: return rid,t
    if member is not None:
        coach_ids={HEAD_COACH_ROLE_ID,ASSISTANT_COACH_ROLE_ID}
        if any(r.id in coach_ids for r in member.roles):
            role_ids={str(r.id) for r in member.roles}
            for rid,team in data["teams"].items():
                if rid in role_ids: return rid,team
    return None,None

def get_player_team(data,uid,member=None):
    if member is not None:
        role_ids={str(r.id) for r in member.roles}
        for rid,team in data["teams"].items():
            if rid in role_ids: return rid,team
    s=str(uid)
    for rid,t in data["teams"].items():
        if any(r["id"]==s for r in t.get("roster",[])): return rid,t
    return None,None

def get_role(guild,rid_str):
    try: return guild.get_role(int(rid_str))
    except: return None

def _base_label(k): return SUSPENSION_REASONS[k][0]

def _susp_summary(selected):
    nk=[r for r in selected if r in SUSPENSION_REASONS]
    sk=[r for r in selected if r in SPECIAL_SUSPENSION_REASONS]
    total=sum(SUSPENSION_REASONS[r][1] for r in nk)
    lg={}
    for r in nk:
        lb=_base_label(r); lg.setdefault(lb,[]).append(SUSPENSION_REASONS[r][1])
    lines=[]
    for lb,gl in lg.items():
        c=len(gl); p=gl[0]; sub=p*c
        lines.append(f"• **{lb}** — {p}g ×{c} = **{sub} games**" if c>1 else f"• **{lb}** — {p} games")
    rl="\n".join(lines)
    sl=("**Status:** "+", ".join(f"`{SPECIAL_SUSPENSION_REASONS[r]}`" for r in sk)) if sk else ""
    return total,rl,sl

# ── EMBED BUILDERS ────────────────────────────────────────────────────
def _info_block(team):
    sz=len(team.get("roster",[]))
    hc_id=team.get("head_coach_id"); hc_name=team.get("head_coach_name",""); hc_rbx=team.get("head_coach_roblox","")
    ahc_id=team.get("ahc_id"); ahc_name=team.get("ahc_name",""); ahc_rbx=team.get("ahc_roblox","")
    lines=[f"roster: {sz}/{MAX_ROSTER}"]
    if hc_id:
        rbx=f" {hc_rbx}" if hc_rbx else ""
        lines.append(f"head coach: <@{hc_id}> (@{hc_name}) ✓{rbx}")
    else: lines.append("head coach: vacant")
    if ahc_id:
        rbx=f" {ahc_rbx}" if ahc_rbx else ""
        lines.append(f"assistant coach: <@{ahc_id}> (@{ahc_name}) ✓{rbx}")
    else: lines.append("assistant coach: vacant")
    return "\n".join(lines)

def _draw_gradient(size: int, stops: list[tuple[int,int,int]]):
    """Draw a left-to-right gradient PIL Image from color stops."""
    from PIL import Image
    img = Image.new("RGBA", (size, size))
    px  = img.load()
    n   = len(stops)
    if n == 1:
        r, g, b = stops[0]
        for x in range(size):
            for y in range(size):
                px[x, y] = (r, g, b, 255)
        return img
    for x in range(size):
        t       = x / (size - 1)
        seg     = t * (n - 1)
        i       = min(int(seg), n - 2)
        lt      = seg - i
        r1,g1,b1 = stops[i];  r2,g2,b2 = stops[i+1]
        r = int(r1 + (r2-r1)*lt); g = int(g1 + (g2-g1)*lt); b = int(b1 + (b2-b1)*lt)
        for y in range(size):
            px[x, y] = (r, g, b, 255)
    return img

async def _sample_logo_colors(logo_url: str, n: int = 3) -> list[tuple[int,int,int]] | None:
    """
    Download the team logo and extract the N most visually distinct
    dominant colors from it using k-means-style quantization via Pillow.
    Returns a list of (r,g,b) tuples sorted darkest → lightest,
    or None if the logo can't be fetched/processed.
    Only colors with saturation > 20 and brightness > 20 are kept
    so near-black and near-white outline pixels don't dominate.
    """
    try:
        from PIL import Image
        import io, colorsys
        async with aiohttp.ClientSession() as s:
            async with s.get(logo_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200: return None
                raw = await resp.read()

        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        # Reduce to palette of 16 colors — fast, built-in quantizer
        small = img.resize((64, 64), Image.LANCZOS)
        # Flatten to RGB, discard transparent pixels
        pixels = []
        for px in small.getdata():
            r, g, b, a = px
            if a < 64: continue          # skip transparent
            pixels.append((r, g, b))
        if not pixels: return None

        # Quantize: convert to palette image then read palette
        rgb_img = Image.new("RGB", (len(pixels), 1))
        rgb_img.putdata(pixels)
        palette_img = rgb_img.quantize(colors=16, method=Image.Quantize.FASTOCTREE)
        palette = palette_img.getpalette()[:16*3]
        colors_rgb = [(palette[i*3], palette[i*3+1], palette[i*3+2])
                      for i in range(16)]

        # Filter out near-black, near-white, near-grey
        def _ok(rgb):
            r,g,b = [x/255 for x in rgb]
            h,s,v = colorsys.rgb_to_hsv(r,g,b)
            return s > 0.20 and 0.12 < v < 0.97
        vivid = [c for c in colors_rgb if _ok(c)]
        if not vivid: vivid = colors_rgb  # fallback: use all

        # Pick N most distinct colors by maximising pairwise distance
        def _dist(a, b):
            return sum((a[i]-b[i])**2 for i in range(3)) ** 0.5
        picked = [vivid[0]]
        while len(picked) < n and len(picked) < len(vivid):
            best = max(vivid, key=lambda c: min(_dist(c, p) for p in picked)
                       if c not in picked else -1)
            if best in picked: break
            picked.append(best)

        # Sort darkest → lightest (left side darker, right brighter)
        picked.sort(key=lambda c: sum(c))
        return picked
    except Exception:
        return None

async def _make_team_thumbnail(logo_url: str, color_int: int,
                                corner_emoji_url: str = "",
                                team_name: str = "") -> bytes | None:
    """
    Generate thumbnail PNG bytes (80×80):
      - Background: gradient sampled from the team logo colors.
        If logo color sampling fails, falls back to solid team color.
      - Centre: team logo (65% of square)
      - Corners: team emoji (14×14 px) in all 4 corners, if available
    """
    try:
        from PIL import Image
        import io

        SIZE     = 80
        LOGO_PCT = 0.65
        CORNER   = 14

        # ── Background: sample logo colors for gradient ────────────────
        stops = None
        if logo_url:
            stops = await _sample_logo_colors(logo_url, n=3)
        if not stops:
            r=(color_int>>16)&0xFF; g=(color_int>>8)&0xFF; b=color_int&0xFF
            stops = [(r, g, b)]
        bg = _draw_gradient(SIZE, stops)

        # ── Centre logo ────────────────────────────────────────────────
        if logo_url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(logo_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        if resp.status == 200:
                            logo_img = Image.open(io.BytesIO(await resp.read())).convert("RGBA")
                            lsz      = int(SIZE * LOGO_PCT)
                            logo_img = logo_img.resize((lsz, lsz), Image.LANCZOS)
                            off      = (SIZE - lsz) // 2
                            bg.paste(logo_img, (off, off), logo_img)
            except Exception:
                pass

        # ── Corner emoji (team emoji, not the logo) ────────────────────
        if corner_emoji_url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(corner_emoji_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        if resp.status == 200:
                            ce = Image.open(io.BytesIO(await resp.read())).convert("RGBA")
                            ce = ce.resize((CORNER, CORNER), Image.LANCZOS)
                            for pos in [(0,0),(SIZE-CORNER,0),(0,SIZE-CORNER),(SIZE-CORNER,SIZE-CORNER)]:
                                bg.paste(ce, pos, ce)
            except Exception:
                pass

        buf = io.BytesIO()
        bg.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

def _tx_embed(team, guild, title_line: str, body_line: str, thumb_url: str = "") -> discord.Embed:
    color = team.get("color", UFF_COLOR) if team else UFF_COLOR
    embed = discord.Embed(title=title_line, description=body_line, color=color)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
    # No footer, no timestamp — clean look matching the reference screenshots
    return embed

async def _send_tx(guild, team, title_line: str, body_line: str):
    """Send a clean transaction embed with thumbnail to the transactions channel."""
    ch = await get_tx_ch(guild)
    if not ch:
        return

    color     = team.get("color", UFF_COLOR) if team else UFF_COLOR
    logo      = team.get("logo_url","") if team else ""
    team_name = team.get("name","") if team else ""

    # Corner emoji CDN URL from stored emoji string e.g. <:OKC:1234>
    corner_url = ""
    emoji_str  = team.get("emoji","") if team else ""
    em = re.match(r"<a?:[^:>]+:(\d+)>", emoji_str)
    if em:
        ext        = "gif" if emoji_str.startswith("<a:") else "png"
        corner_url = f"https://cdn.discordapp.com/emojis/{em.group(1)}.{ext}"

    # Generate the thumbnail PNG bytes
    png_bytes = await _make_team_thumbnail(logo, color, corner_url, team_name)

    thumb_url = ""
    if png_bytes:
        import io
        # Upload the image as a standalone message, grab CDN URL, then delete it.
        # This gives us a stable CDN URL to embed as thumbnail without showing
        # the raw file in the transactions channel.
        try:
            tmp = await ch.send(
                file=discord.File(io.BytesIO(png_bytes), filename="thumb.png"),
                silent=True,
            )
            # Get the attachment CDN URL
            if tmp.attachments:
                thumb_url = tmp.attachments[0].url
            await tmp.delete()
        except Exception:
            thumb_url = logo  # fallback to plain logo URL

    if not thumb_url:
        thumb_url = logo  # always fallback to logo if generation failed

    embed = _tx_embed(team, guild, title_line, body_line, thumb_url)
    await ch.send(embed=embed)

async def build_tx_action(action, player, team, guild):
    """Return (title_line, body_line) for a player transaction."""
    verified  = get_verified_emoji(guild)
    team_name = team.get("name","") if team else ""
    last      = _last_word(team_name)
    # No emoji prefix in title — clean look matching the Vikings screenshot
    title_line = f"{team_name} {verified} @{last}"

    blox     = await bloxlink_lookup(player.id, guild.id)
    rbx_name = blox.get("roblox_username","Unknown")
    rbx_part = f" ✓ {rbx_name}" if rbx_name and rbx_name != "Unknown" else ""
    body_line = f"are **{action.lower()}** {player.mention} (@{player.name}){rbx_part}"
    return title_line, body_line

async def build_coach_action(action, player, team, guild):
    """Return (title_line, body_line) for a coach transaction."""
    verified  = get_verified_emoji(guild)
    team_name = team.get("name","") if team else ""
    last      = _last_word(team_name)
    title_line = f"{team_name} {verified} @{last}"

    blox     = await bloxlink_lookup(player.id, guild.id)
    rbx_name = blox.get("roblox_username","Unknown")
    rbx_part = f" ✓ {rbx_name}" if rbx_name and rbx_name != "Unknown" else ""
    role_lbl = "assistant coach" if "promot" in action.lower() else "regular player"
    body_line = f"are **{action.lower()}** {player.mention} (@{player.name}){rbx_part} to {role_lbl}"
    return title_line, body_line

async def post_tx(guild, title_line, body_line, team, followup=None, msg="", ephemeral=True):
    await _send_tx(guild, team, title_line, body_line)
    if followup and msg:
        await followup.send(msg, ephemeral=ephemeral)

# Legacy shims — not called directly anymore but kept for safety
async def post_tx_webhook(guild, team, action_line, followup=None, msg="", ephemeral=True):
    await post_tx(guild, action_line, "", team, followup=followup, msg=msg, ephemeral=ephemeral)

# ── SCHEDULE PARSER ───────────────────────────────────────────────────
def _clean_line(line:str) -> str:
    """Strip markdown, seeding numbers, labels, and Discord formatting from a schedule line."""
    s=line
    s=re.sub(r"\*+","",s)           # remove bold/italic ** *
    s=re.sub(r"__","",s)            # remove underline
    s=re.sub(r"#\d+","",s)          # remove seeding numbers like #4
    s=re.sub(r"\(edited\)","",s,flags=re.IGNORECASE)
    s=re.sub(r"GOTW[^:]*:?","",s,flags=re.IGNORECASE)
    s=re.sub(r"primetime[:\s]*","",s,flags=re.IGNORECASE)
    s=re.sub(r"regular[:\s]*","",s,flags=re.IGNORECASE)
    s=re.sub(r"match\s+\w+[\s\(\)A-Za-z0-9]*:?","",s,flags=re.IGNORECASE)
    s=re.sub(r"series\s+\w+[\s\(\)A-Za-z0-9]*:?","",s,flags=re.IGNORECASE)
    s=re.sub(r"GOTW","",s,flags=re.IGNORECASE)
    # Remove emoji-only tokens (Unicode emoji sequences)
    s=re.sub(r"[⭐🏈🏆🔥💥🎯]","",s)
    return s.strip()

def _role_from_segment(seg:str, guild:discord.Guild):
    """
    Given one side of a 'vs' split, find the Discord role it refers to.
    Handles: raw <@&ID> mentions, @Name, plain Name.
    """
    seg=seg.strip()
    # Raw Discord role mention
    m=re.search(r"<@&(\d+)>",seg)
    if m:
        role=guild.get_role(int(m.group(1)))
        if role: return role
    # Strip leading @ if present
    candidate=re.sub(r"^@","",seg).strip()
    # Remove trailing seeding digits
    candidate=re.sub(r"\s*\d+\s*$","",candidate).strip()
    if not candidate: return None
    lc=candidate.lower()
    # Exact full name match
    for role in guild.roles:
        if role.name.lower()==lc: return role
    # Last word match ("Spartans" -> "Seminola Spartans")
    cand_last=lc.split()[-1] if lc.split() else ""
    if cand_last:
        for role in guild.roles:
            parts=role.name.lower().split()
            if parts and parts[-1]==cand_last: return role
    # Substring match
    for role in guild.roles:
        if lc in role.name.lower(): return role
    return None

def _parse_schedule_pairs(text:str, guild:discord.Guild):
    """
    Parse ALL 'Team A vs Team B' matchups from a pasted schedule.
    Returns list of (role1, role2) tuples.

    Strategy (two passes):
    1. RAW PASS — scan every line for exactly two <@&ID> mentions separated
       by 'vs'. This handles the copy-pasted Discord source with role mentions
       and works regardless of surrounding emoji/bold/label noise.
    2. TEXT PASS — for any line that survived cleaning and has readable team
       names with 'vs' but no raw mentions, try name-based lookup as fallback.
    """
    pairs=[]
    seen=set()

    for raw_line in text.splitlines():
        # ── Pass 1: extract raw <@&ID> pairs directly ─────────────────
        # Find all role mention IDs on this line
        mention_ids = re.findall(r"<@&(\d+)>", raw_line)
        if len(mention_ids) >= 2:
            # Check there's a 'vs' between the first and second mention
            # by finding their positions
            pos1 = raw_line.index(f"<@&{mention_ids[0]}>")
            pos2 = raw_line.index(f"<@&{mention_ids[1]}>", pos1 + 1)
            between = raw_line[pos1:pos2]
            if re.search(r"\bvs\.?\b", between, re.IGNORECASE):
                r1 = guild.get_role(int(mention_ids[0]))
                r2 = guild.get_role(int(mention_ids[1]))
                if r1 and r2 and r1 != r2:
                    key = frozenset([r1.id, r2.id])
                    if key not in seen:
                        seen.add(key)
                        pairs.append((r1, r2))
                continue  # handled, skip text pass for this line

        # ── Pass 2: text-based fallback (no raw mentions) ─────────────
        # Strip noise but keep role name text
        s = raw_line
        s = re.sub(r"<a?:[^:>]+:\d+>", "", s)   # strip custom emoji tags
        s = re.sub(r"<@&?\d+>", "", s)            # strip any leftover mentions
        s = re.sub(r"\*+", "", s)                  # strip bold
        s = re.sub(r"#\d+", "", s)                 # strip seeding numbers
        s = re.sub(r"\(edited\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"GOTW[^v]*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"primetime[:\s]*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"regular[:\s]*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"match\s+\S+[^v]*", "", s, flags=re.IGNORECASE)
        s = " ".join(s.split()).strip()
        if not s:
            continue

        parts = re.split(r"\s+vs\.?\s+", s, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            continue
        left, right = parts[0].strip(), parts[1].strip()
        if not left or not right:
            continue
        r1 = _role_from_segment(left, guild)
        r2 = _role_from_segment(right, guild)
        if not r1 or not r2 or r1 == r2:
            continue
        key = frozenset([r1.id, r2.id])
        if key in seen:
            continue
        seen.add(key)
        pairs.append((r1, r2))

    return pairs

# ── OFFER VIEW ────────────────────────────────────────────────────────
class OfferView(discord.ui.View):
    def __init__(self,offer_id,team_role_id,team_name,team_logo,team_emoji,hc_id,player_id,guild_id):
        super().__init__(timeout=43200)
        self.offer_id=offer_id; self.team_role_id=team_role_id; self.team_name=team_name
        self.team_logo=team_logo; self.team_emoji=team_emoji; self.hc_id=hc_id
        self.player_id=player_id; self.guild_id=guild_id; self.responded=False

    async def on_timeout(self):
        for item in self.children: item.disabled=True
        data=await load_data(); data.get("offers",{}).pop(self.offer_id,None); await save_data(data)

    @discord.ui.button(label="✅  Accept",style=discord.ButtonStyle.success)
    async def accept(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.player_id:
            await interaction.response.send_message("❌ This offer isn't for you.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True
        await interaction.response.defer()
        data=await load_data(); guild=bot.get_guild(self.guild_id)
        if not guild:
            await interaction.edit_original_response(content="❌ Server not found.",view=self); return
        rid=str(self.team_role_id); team=data["teams"].get(rid)
        if not team:
            await interaction.edit_original_response(content="❌ Team no longer exists.",view=self); return
        try:
            player=(guild.get_member(self.player_id) or await guild.fetch_member(self.player_id))
        except discord.NotFound:
            await interaction.edit_original_response(content="❌ Couldn't find you in the server.",view=self); return
        cur_rid,cur_team=get_player_team(data,player.id,player)
        if cur_team and cur_rid!=rid:
            await interaction.edit_original_response(
                content=f"❌ You're already on **{cur_team['name']}**. Get released or demand first.",view=self); return
        roster=team.setdefault("roster",[])
        if len(roster)>=MAX_ROSTER:
            await interaction.edit_original_response(content=f"❌ Roster cap ({MAX_ROSTER}) reached.",view=self); return
        if any(r["id"]==str(player.id) for r in roster):
            await interaction.edit_original_response(content="❌ Already on this team.",view=self); return
        blox=await bloxlink_lookup(player.id,guild.id)
        rbx_name=blox.get("roblox_username","Unknown"); rbx_avatar=blox.get("avatar_url","")
        roster.append({"id":str(player.id),"name":player.display_name,"roblox":rbx_name,"role":"Player"})
        data.get("offers",{}).pop(self.offer_id,None); await save_data(data)
        team_role=guild.get_role(int(rid)); role_failed=False
        if team_role:
            try: await player.add_roles(team_role,reason=f"Signed to {team['name']}")
            except discord.Forbidden: role_failed=True
        title_line,body_line=await build_tx_action("signing",player,team,guild)
        await _send_tx(guild,team,title_line,body_line)
        desc=f"You accepted the offer from **{self.team_name}**!\n\nWelcome to the team."
        if role_failed: desc+="\n\n⚠️ Team role couldn't be added automatically."
        ae=discord.Embed(title="✅ Offer Accepted!",description=desc,color=0x57F287)
        if self.team_logo: ae.set_thumbnail(url=self.team_logo)
        ae.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(content=None,embed=ae,view=self)
        if self.hc_id:
            try:
                hc=(guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id)))
                n=discord.Embed(title="✅ Offer Accepted",
                    description=f"**{player.display_name}** accepted your offer to **{self.team_name}**.",color=0x57F287)
                n.set_footer(text=UFF_FOOTER); await hc.send(embed=n)
            except: pass

    @discord.ui.button(label="❌  Decline",style=discord.ButtonStyle.danger)
    async def decline(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.player_id:
            await interaction.response.send_message("❌ This offer isn't for you.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True
        await interaction.response.defer()
        data=await load_data(); data.get("offers",{}).pop(self.offer_id,None); await save_data(data)
        e=discord.Embed(title="❌ Offer Declined",description=f"You declined the offer from **{self.team_name}**.",color=0xED4245)
        if self.team_logo: e.set_thumbnail(url=self.team_logo)
        e.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(embed=e,view=self)
        guild=bot.get_guild(self.guild_id)
        if guild and self.hc_id:
            try:
                hc=(guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id)))
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
        data=await load_data(); data.get("pending",{}).pop(self.match_id,None); await save_data(data)

    @discord.ui.button(label="✅  Accept Challenge",style=discord.ButtonStyle.success)
    async def accept(self,interaction:discord.Interaction,button:discord.ui.Button):
        if interaction.user.id!=self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.",ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.",ephemeral=True); return
        self.responded=True; self.stop()
        for item in self.children: item.disabled=True
        await interaction.response.defer()
        data=await load_data(); guild=bot.get_guild(self.guild_id)
        if not guild:
            await interaction.edit_original_response(content="❌ Server not found.",view=self); return
        try:
            challenger=(guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id))
            opponent=(guild.get_member(self.opponent_id) or await guild.fetch_member(self.opponent_id))
        except discord.NotFound:
            await interaction.edit_original_response(content="❌ Could not find players.",view=self); return
        p1=get_player(data,challenger.id); p2=get_player(data,opponent.id)
        r1,e1,_=get_rank(p1["elo"]); r2,e2,_=get_rank(p2["elo"])
        embed=discord.Embed(title="ranked pickup matchup",color=UFF_COLOR)
        embed.add_field(name=f"🟡 {challenger.display_name}",value=f"{challenger.mention}\n**{self.challenger_team}**\nRank: `{e1} {r1}`",inline=True)
        embed.add_field(name="\u200b",value="**— VS —**",inline=True)
        embed.add_field(name=f"🔵 {opponent.display_name}",value=f"{opponent.mention}\n**{self.opponent_team}**\nRank: `{e2} {r2}`",inline=True)
        embed.add_field(name="🔗 game link",value=f"[Click here →]({self.game_link})",inline=False)
        if UFF_BANNER: embed.set_image(url=UFF_BANNER)
        lt=_league_thumb(guild)
        if lt: embed.set_thumbnail(url=lt)
        embed.set_footer(text=f"✅ LIVE • /pickup_results when done | {UFF_FOOTER}"); embed.timestamp=datetime.utcnow()
        ch=await get_pickup_ch(guild)
        if ch:
            await ch.send(content=f"@everyone **Ranked Pickup** — **{challenger.display_name}** vs **{opponent.display_name}**",
                embed=embed,allowed_mentions=discord.AllowedMentions(everyone=True))
        p2["last_game"]=datetime.utcnow().isoformat()
        data.get("pending",{}).pop(self.match_id,None); await save_data(data)
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
        e=discord.Embed(title="❌ Challenge Declined",description=f"You declined **{self.challenger_name}**'s pickup.",color=0xED4245)
        e.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(embed=e,view=self)
        guild=bot.get_guild(self.guild_id)
        if guild:
            try:
                c=(guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id))
                n=discord.Embed(title="❌ Challenge Declined",description=f"**{self.opponent_name}** declined your pickup.",color=0xED4245)
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
        data=await load_data(); data.get("casual_pending",{}).pop(self.match_id,None); await save_data(data)

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
            challenger=(guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id))
            opponent=(guild.get_member(self.opponent_id) or await guild.fetch_member(self.opponent_id))
        except discord.NotFound:
            await interaction.edit_original_response(content="❌ Could not find players.",view=self); return
        data=await load_data(); data.get("casual_pending",{}).pop(self.match_id,None)
        get_player(data,self.opponent_id)["last_game"]=datetime.utcnow().isoformat(); await save_data(data)
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
        if ch:
            await ch.send(content=f"@here **Casual Pickup** — **{challenger.display_name}** vs **{opponent.display_name}**",
                embed=embed,allowed_mentions=discord.AllowedMentions(everyone=True))
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
        e=discord.Embed(title="❌ Challenge Declined",description=f"You declined **{self.challenger_name}**'s casual.",color=0xED4245)
        e.set_footer(text=UFF_FOOTER)
        await interaction.edit_original_response(embed=e,view=self)
        guild=bot.get_guild(self.guild_id)
        if guild:
            try:
                c=(guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id))
                n=discord.Embed(title="❌ Challenge Declined",description=f"**{self.opponent_name}** declined your casual.",color=0xED4245)
                n.set_footer(text=UFF_FOOTER); await c.send(embed=n)
            except: pass

async def _run_casual(interaction, opponent, game_link):
    await interaction.response.defer(ephemeral=True)
    if (not ({r.id for r in interaction.user.roles}&PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction)):
        await interaction.followup.send("Missing required role.",ephemeral=True); return
    if opponent.id==interaction.user.id:
        await interaction.followup.send("Can't challenge yourself!",ephemeral=True); return
    if opponent.bot:
        await interaction.followup.send("Can't challenge a bot!",ephemeral=True); return
    data=await load_data(); _expire_casual(data)
    uid=str(interaction.user.id); oid=str(opponent.id)
    for m in data.get("casual_pending",{}).values():
        if m["challenger_id"]==uid or m["opponent_id"]==uid:
            await interaction.followup.send("You already have a pending casual.",ephemeral=True); return
        if m["challenger_id"]==oid or m["opponent_id"]==oid:
            await interaction.followup.send(f"**{opponent.display_name}** already has a pending casual.",ephemeral=True); return

    # Auto-detect teams from roles
    your_team_display, _     = _get_team_display(interaction.user, data)
    opponent_team_display, _ = _get_team_display(opponent, data)

    get_player(data,interaction.user.id)["last_game"]=datetime.utcnow().isoformat()
    mid=f"casual_{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("casual_pending",{})[mid]={
        "challenger_id":str(interaction.user.id),"opponent_id":str(opponent.id),
        "challenger_name":interaction.user.display_name,"opponent_name":opponent.display_name,
        "challenger_team":your_team_display,"opponent_team":opponent_team_display,"game_link":game_link,
        "timestamp":datetime.utcnow().isoformat(),"match_id":mid,"guild_id":interaction.guild.id}
    await save_data(data)
    lt=_league_thumb(interaction.guild)
    dm=discord.Embed(title="Casual Pickup Challenge!",
        description=f"**{interaction.user.display_name}** wants a casual. Expires **30 min**.",color=CASUAL_COLOR)
    dm.add_field(name=f"🟡 {interaction.user.display_name}",value=f"`{interaction.user.name}`\n{your_team_display}",inline=True)
    dm.add_field(name="\u200b",value="**— VS —**",inline=True)
    dm.add_field(name=f"🔵 {opponent.display_name}",value=f"`{opponent.name}`\n{opponent_team_display}",inline=True)
    dm.add_field(name="Game Link",value=f"[Click here]({game_link})",inline=False)
    dm.add_field(name="\u200b",value="⚠️ **NOT ranked** — no ELO changes.",inline=False)
    if UFF_BANNER: dm.set_image(url=UFF_BANNER)
    if lt: dm.set_thumbnail(url=lt)
    dm.set_footer(text=UFF_FOOTER); dm.timestamp=datetime.utcnow()
    view=CasualPickupView(mid,interaction.user.id,opponent.id,interaction.user.display_name,
        opponent.display_name,your_team_display,opponent_team_display,game_link,interaction.guild.id)
    try: await opponent.send(embed=dm,view=view)
    except discord.Forbidden:
        data.get("casual_pending",{}).pop(mid,None); await save_data(data)
        await interaction.followup.send(f"Can't DM **{opponent.display_name}**.",ephemeral=True); return
    ack=discord.Embed(title="Casual Sent!",
        description=f"Sent to **{opponent.display_name}**.\n\n🟡 You: {your_team_display}\n🔵 Them: {opponent_team_display}\n\nPosted publicly only if accepted.",
        color=0x57F287)
    await interaction.followup.send(embed=ack,ephemeral=True)

# ── SUSPENSION UI ─────────────────────────────────────────────────────
def _make_susp_opts():
    opts=[]; seen={}
    for k,(lb,g) in SUSPENSION_REASONS.items():
        seen[lb]=seen.get(lb,0)+1; n=seen[lb]
        if n==1: opts.append(discord.SelectOption(label=f"{lb} — {g} games",value=k,description=f"Adds {g} games"))
        else:    opts.append(discord.SelectOption(label=f"{lb} (x{n}) — +{g} games",value=k,description=f"Stack: +{g} games"))
    for k,lb in SPECIAL_SUSPENSION_REASONS.items():
        opts.append(discord.SelectOption(label=lb,value=k,description="Status only"))
    return opts[:25]

class SuspReasonSelect(discord.ui.Select):
    def __init__(self):
        opts=_make_susp_opts()
        super().__init__(placeholder=f"Select up to {MAX_SUSPENSION_REASONS} reasons...",
                         min_values=1,max_values=min(MAX_SUSPENSION_REASONS,len(opts)),options=opts)
    async def callback(self,interaction:discord.Interaction):
        v=self.view; v.selected=self.values
        total,rl,sl=_susp_summary(v.selected)
        p=f"**Suspension preview — {v.target.display_name}**\n\n"
        if rl: p+=rl+"\n\n"
        if sl: p+=sl+"\n\n"
        p+=f"**Total: {total} games**\n\nClick **Confirm & Post**."
        await interaction.response.edit_message(content=p,view=v)

class SuspConfirmBtn(discord.ui.Button):
    def __init__(self): super().__init__(label="Confirm & Post",style=discord.ButtonStyle.success,row=1)
    async def callback(self,interaction:discord.Interaction):
        v=self.view
        if not v.selected:
            await interaction.response.send_message("Select at least one reason.",ephemeral=True); return
        await interaction.response.defer()
        total,rl,sl=_susp_summary(v.selected)
        embed=discord.Embed(title="player suspension",color=0xED4245)
        embed.add_field(name="Player",value=f"<@{v.target.id}> ({v.target.display_name})",inline=False)
        if rl: embed.add_field(name="Reason(s)",value=rl,inline=False)
        if sl: embed.add_field(name="Additional Status",value=sl,inline=False)
        embed.add_field(name="Total Games Suspended",value=f"**{total} games**",inline=False)
        if v.target.avatar: embed.set_thumbnail(url=v.target.avatar.url)
        embed.set_footer(text=f"Issued by {interaction.user.display_name} | {UFF_FOOTER}")
        embed.timestamp=datetime.utcnow()
        for item in v.children: item.disabled=True
        ch=await get_susp_ch(interaction.guild)
        if ch:
            await ch.send(embed=embed)
            susp_role=interaction.guild.get_role(SUSPENSION_ROLE_ID)
            if susp_role:
                try: await v.target.add_roles(susp_role,reason="Suspended")
                except discord.Forbidden: pass
            await interaction.edit_original_response(content=f"Posted to {ch.mention}.",view=v)
            nk=[r for r in v.selected if r in SUSPENSION_REASONS]
            sk=[r for r in v.selected if r in SPECIAL_SUSPENSION_REASONS]
            data=await load_data()
            data.setdefault("suspensions",[]).append({
                "player_id":str(v.target.id),"player_name":v.target.display_name,
                "reason_keys":nk,"reasons":[_base_label(r) for r in nk],
                "status_flags":[SPECIAL_SUSPENSION_REASONS[r] for r in sk],
                "total_games":total,"issued_by":str(interaction.user.id),
                "issued_by_name":interaction.user.display_name,
                "date":datetime.utcnow().isoformat(),"cleared":False})
            await save_data(data)
        else:
            await interaction.edit_original_response(content="Suspension channel not found.",view=v)

class SuspCancelBtn(discord.ui.Button):
    def __init__(self): super().__init__(label="Cancel",style=discord.ButtonStyle.secondary,row=1)
    async def callback(self,interaction:discord.Interaction):
        v=self.view
        for item in v.children: item.disabled=True
        await interaction.response.edit_message(content="Cancelled.",view=v); v.stop()

class SuspView(discord.ui.View):
    def __init__(self,target,issuer_id):
        super().__init__(timeout=300)
        self.target=target; self.issuer_id=issuer_id; self.selected=[]
        self.add_item(SuspReasonSelect()); self.add_item(SuspConfirmBtn()); self.add_item(SuspCancelBtn())
    async def interaction_check(self,interaction:discord.Interaction)->bool:
        if interaction.user.id!=self.issuer_id:
            await interaction.response.send_message("Only the issuer can use these.",ephemeral=True); return False
        return True
    async def on_timeout(self):
        for item in self.children: item.disabled=True

# ── BOT SETUP ─────────────────────────────────────────────────────────
intents=discord.Intents.default(); intents.message_content=True; intents.members=True
bot=commands.Bot(command_prefix="!",intents=intents)

@bot.tree.error
async def on_app_command_error(interaction:discord.Interaction,error:app_commands.AppCommandError):
    import traceback; traceback.print_exc()
    msg="Something went wrong. Try again in a moment."
    try:
        if interaction.response.is_done(): await interaction.followup.send(msg,ephemeral=True)
        else: await interaction.response.send_message(msg,ephemeral=True)
    except: pass

@bot.event
async def on_error(event,*args,**kwargs):
    import traceback; print(f"[ERROR] {event}:"); traceback.print_exc()

@bot.event
async def on_ready():
    await init_db()
    guild_obj=discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj); await bot.tree.sync()
    print(f"UFF Bot v6.1 online: {bot.user}")

# ══════════════════════════════════════════════════════════════════════
# TRANSACTION COMMANDS
# ══════════════════════════════════════════════════════════════════════

@bot.tree.command(name="set_team",description="[Staff] Register a Discord role as a UFF team")
@app_commands.describe(team_role="The team's Discord role")
@app_commands.default_permissions(administrator=True)
async def set_team(interaction:discord.Interaction,team_role:discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    team_name=team_role.name; icon=team_role.display_icon
    logo_url=str(icon.url) if icon and hasattr(icon,"url") else ""
    data=await load_data(); rid=str(team_role.id); ex=data["teams"].get(rid,{})
    data["teams"][rid]={
        "name":team_name,"role_id":rid,
        "head_coach_id":ex.get("head_coach_id"),"head_coach_name":ex.get("head_coach_name"),
        "head_coach_roblox":ex.get("head_coach_roblox",""),
        "ahc_id":ex.get("ahc_id"),"ahc_name":ex.get("ahc_name"),"ahc_roblox":ex.get("ahc_roblox",""),
        "logo_url":logo_url or ex.get("logo_url",""),"emoji":ex.get("emoji",""),
        "roster":ex.get("roster",[]),"color":team_role.color.value or UFF_COLOR}
    await save_data(data)
    embed=discord.Embed(title="team registered",color=UFF_COLOR,
        description=f"**{team_name}** registered!\nRole: {team_role.mention} | Transactions: <#{TRANSACTIONS_CHANNEL_ID}>")
    if logo_url: embed.set_thumbnail(url=logo_url)
    embed.set_footer(text=UFF_FOOTER)
    await interaction.followup.send(embed=embed,ephemeral=True)

@bot.tree.command(name="set_team_image",description="[Staff] Set or override the team logo URL")
@app_commands.describe(team_role="The team's Discord role",logo_url="Direct image URL")
@app_commands.default_permissions(administrator=True)
async def set_team_image(interaction:discord.Interaction,team_role:discord.Role,logo_url:str):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"{team_role.mention} not registered.",ephemeral=True); return
    data["teams"][rid]["logo_url"]=logo_url; await save_data(data)
    embed=discord.Embed(title="logo updated",color=UFF_COLOR,description=f"Logo for **{data['teams'][rid]['name']}** updated.")
    embed.set_thumbnail(url=logo_url)
    await interaction.followup.send(embed=embed,ephemeral=True)

@bot.tree.command(name="set_team_emoji",description="[Staff] Set the emoji shown before a team's name")
@app_commands.describe(team_role="The team's Discord role",emoji="The emoji")
@app_commands.default_permissions(administrator=True)
async def set_team_emoji(interaction:discord.Interaction,team_role:discord.Role,emoji:str):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"{team_role.mention} not registered.",ephemeral=True); return
    data["teams"][rid]["emoji"]=emoji.strip(); await save_data(data)
    await interaction.followup.send(f"Emoji for **{data['teams'][rid]['name']}** set to {emoji}",ephemeral=True)

@bot.tree.command(name="sync_roster",description="[Staff] Sync roster to match who has the team role")
@app_commands.describe(team_role="The team's Discord role")
@app_commands.default_permissions(administrator=True)
async def sync_roster(interaction:discord.Interaction,team_role:discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"{team_role.mention} not registered.",ephemeral=True); return
    team=data["teams"][rid]; old_ids={r["id"] for r in team.get("roster",[])}
    old_data={r["id"]:r for r in team.get("roster",[])}
    new_roster=[]
    for member in team_role.members:
        mid=str(member.id)
        new_roster.append(old_data[mid] if mid in old_data else {"id":mid,"name":member.display_name,"roblox":"","role":"Player"})
    team["roster"]=new_roster
    new_ids={str(m.id) for m in team_role.members}; added=len(new_ids-old_ids); removed=len(old_ids-new_ids)
    hc_id=team.get("head_coach_id"); hc_cleared=False
    if hc_id:
        hc_m=interaction.guild.get_member(int(hc_id))
        if not hc_m or team_role not in hc_m.roles:
            team.update(head_coach_id=None,head_coach_name=None,head_coach_roblox=""); hc_cleared=True
    ahc_id=team.get("ahc_id"); ahc_cleared=False
    if ahc_id:
        ahc_m=interaction.guild.get_member(int(ahc_id))
        if not ahc_m or team_role not in ahc_m.roles:
            team.update(ahc_id=None,ahc_name=None,ahc_roblox=""); ahc_cleared=True
    hc_ro=interaction.guild.get_role(HEAD_COACH_ROLE_ID); ahc_ro=interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID)
    hc_det=False; ahc_det=False
    for member in team_role.members:
        mid=str(member.id)
        if hc_ro and hc_ro in member.roles and not hc_det:
            team["head_coach_id"]=mid; team["head_coach_name"]=member.name
            if not team.get("head_coach_roblox"): team["head_coach_roblox"]=""
            hc_det=True; hc_cleared=False
            for r in team["roster"]:
                if r["id"]==mid: r["role"]="Head Coach"
        elif ahc_ro and ahc_ro in member.roles and not ahc_det:
            team["ahc_id"]=mid; team["ahc_name"]=member.name
            if not team.get("ahc_roblox"): team["ahc_roblox"]=""
            ahc_det=True; ahc_cleared=False
            for r in team["roster"]:
                if r["id"]==mid: r["role"]="Assistant Head Coach"
    await save_data(data)
    notes=[]
    if hc_det:     notes.append("HC auto-detected")
    if ahc_det:    notes.append("AHC auto-detected")
    if hc_cleared: notes.append("HC cleared (left team)")
    if ahc_cleared:notes.append("AHC cleared (left team)")
    note_str="\n"+"\n".join(notes) if notes else ""
    await interaction.followup.send(f"**{team['name']}** synced. {len(new_roster)}/{MAX_ROSTER} | +{added} -{removed}{note_str}",ephemeral=True)

@bot.tree.command(name="assign_hc",description="[Staff] Assign a head coach to a team")
@app_commands.describe(team_role="The team's Discord role",player="The member to make HC")
@app_commands.default_permissions(administrator=True)
async def assign_hc(interaction:discord.Interaction,team_role:discord.Role,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    data=await load_data(); rid=str(team_role.id)
    if rid not in data["teams"]:
        await interaction.followup.send(f"{team_role.mention} not registered.",ephemeral=True); return
    blox=await bloxlink_lookup(player.id,interaction.guild.id); rbx=blox.get("roblox_username","")
    for other_rid,other_team in data["teams"].items():
        if other_rid!=rid and other_team.get("head_coach_id")==str(player.id):
            other_team.update(head_coach_id=None,head_coach_name=None,head_coach_roblox="")
    team=data["teams"][rid]
    team.update(head_coach_id=str(player.id),head_coach_name=player.name,head_coach_roblox=rbx)
    roster=team.setdefault("roster",[])
    if str(player.id) not in [r["id"] for r in roster]:
        roster.append({"id":str(player.id),"name":player.display_name,"roblox":rbx,"role":"Head Coach"})
    else:
        for r in roster:
            if r["id"]==str(player.id): r["role"]="Head Coach"
    await save_data(data)
    hc_role=interaction.guild.get_role(HEAD_COACH_ROLE_ID); rf=False
    if hc_role:
        try: await player.add_roles(hc_role,reason="Assigned HC")
        except discord.Forbidden: rf=True
    msg=f"**{player.display_name}** is now HC of **{team['name']}**."
    if rf: msg+="\nCould not assign HC role - check bot permissions."
    await interaction.followup.send(msg,ephemeral=True)

@bot.tree.command(name="offer",description="Send a player a roster offer via DM (12h window)")
@app_commands.describe(player="The player to offer a roster spot to")
async def offer(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); rid,team=get_team_for_user(data,interaction.user.id,interaction.user)
    if not team:
        await interaction.followup.send("You're not HC or AHC of any team.",ephemeral=True); return
    if player.id==interaction.user.id:
        await interaction.followup.send("Can't offer yourself.",ephemeral=True); return
    if player.bot:
        await interaction.followup.send("Can't offer a bot.",ephemeral=True); return
    roster=team.setdefault("roster",[])
    if len(roster)>=MAX_ROSTER:
        await interaction.followup.send(f"Roster cap ({MAX_ROSTER}) reached.",ephemeral=True); return
    cur_rid,cur_team=get_player_team(data,player.id,player)
    if cur_team:
        await interaction.followup.send(f"**{player.display_name}** is already on **{cur_team['name']}**.",ephemeral=True); return
    oid=f"offer_{rid}_{player.id}_{int(datetime.utcnow().timestamp())}"
    team_logo=team.get("logo_url",""); team_emoji=team.get("emoji",""); hc_id=team.get("head_coach_id")
    data.setdefault("offers",{})[oid]={"team_role_id":rid,"team_name":team["name"],"player_id":str(player.id),
        "hc_id":hc_id,"timestamp":datetime.utcnow().isoformat(),"guild_id":str(interaction.guild.id)}
    await save_data(data)
    hc_rbx=team.get("head_coach_roblox",""); hc_line=f"<@{hc_id}> `{hc_rbx}`".strip() if hc_id else "*vacant*"
    emoji=team_emoji+" " if team_emoji else ""
    dm=discord.Embed(title=f"offer from the {team['name']}",
        description=f"You've been offered a roster spot on {emoji}**{team['name']}**!",color=team.get("color",UFF_COLOR))
    dm.add_field(name="head coach:",value=hc_line,inline=False)
    dm.add_field(name="\u200b",value="You have **12 hours** to accept or ignore.",inline=False)
    thumb=team_logo or _league_thumb(interaction.guild)
    if thumb: dm.set_thumbnail(url=thumb)
    dm.set_footer(text=UFF_FOOTER); dm.timestamp=datetime.utcnow()
    view=OfferView(oid,int(rid),team["name"],team_logo,team_emoji,hc_id,player.id,interaction.guild.id)
    try: await player.send(embed=dm,view=view)
    except discord.Forbidden:
        data.get("offers",{}).pop(oid,None); await save_data(data)
        await interaction.followup.send(f"Can't DM **{player.display_name}**.",ephemeral=True); return
    await interaction.followup.send(f"Offer sent to **{player.display_name}** — 12 hours to accept.",ephemeral=True)

@bot.tree.command(name="release",description="Release a player from your team")
@app_commands.describe(player="The player to release")
async def release(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True,thinking=True)
    data=await load_data(); rid,team=get_team_for_user(data,interaction.user.id,interaction.user)
    if not team and not is_staff(interaction):
        await interaction.followup.send("You're not HC or AHC of any team.",ephemeral=True); return
    if not team and is_staff(interaction):
        rid,team=get_player_team(data,player.id,player)
        if not team:
            await interaction.followup.send(f"{player.display_name} isn't on any team.",ephemeral=True); return
    team_role_obj=get_role(interaction.guild,rid)
    in_db=any(r["id"]==str(player.id) for r in team.get("roster",[]))
    has_role=team_role_obj is not None and team_role_obj in player.roles
    if not in_db and not has_role:
        await interaction.followup.send(f"{player.display_name} isn't on **{team['name']}**.",ephemeral=True); return
    for t in data["teams"].values():
        t["roster"]=[r for r in t.get("roster",[]) if r["id"]!=str(player.id)]
    await save_data(data)
    rf=False
    if team_role_obj and team_role_obj in player.roles:
        try: await player.remove_roles(team_role_obj,reason=f"Released from {team['name']}")
        except discord.Forbidden: rf=True
    title_line,body_line=await build_tx_action("released",player,team,interaction.guild)
    msg=f"Released **{player.display_name}** from **{team['name']}**."
    if rf: msg+="\nCould not remove team role - check bot permissions."
    await post_tx(interaction.guild,title_line,body_line,team,followup=interaction.followup,msg=msg)

@bot.tree.command(name="demand",description="Demand a release from your team (1 lifetime)")
async def demand(interaction:discord.Interaction):
    await interaction.response.defer(ephemeral=True,thinking=True)
    data=await load_data(); uid=str(interaction.user.id)
    found_rid,found_team=get_player_team(data,uid,interaction.user)
    if not found_team:
        await interaction.followup.send("You aren't on any registered team.",ephemeral=True); return
    extra=data.get("extra_demands",{}).get(uid,0)
    if data.get("demand_used",{}).get(uid,False) and extra<=0:
        await interaction.followup.send("Already used your demand. Ask admin for /grant_extra_demand.",ephemeral=True); return
    if data.get("demand_used",{}).get(uid,False): data["extra_demands"][uid]=extra-1
    else: data.setdefault("demand_used",{})[uid]=True
    for t in data["teams"].values():
        t["roster"]=[r for r in t.get("roster",[]) if r["id"]!=uid]
    await save_data(data)
    blox=await bloxlink_lookup(interaction.user.id,interaction.guild.id)
    rbx_name=blox.get("roblox_username","Unknown")
    team_role=get_role(interaction.guild,found_rid); rf=False
    if team_role and team_role in interaction.user.roles:
        try: await interaction.user.remove_roles(team_role,reason=f"Demand from {found_team['name']}")
        except discord.Forbidden: rf=True
    verified  = get_verified_emoji(interaction.guild)
    team_name = found_team.get("name","")
    last      = _last_word(team_name)
    title_line = f"{team_name} {verified} @{last}"
    rbx_part  = f" ✓ {rbx_name}" if rbx_name and rbx_name!="Unknown" else ""
    body_line = f"{interaction.user.mention} (@{interaction.user.name}){rbx_part} has **demanded** a release"
    msg=f"Your demand from **{found_team['name']}** has been posted."
    if rf: msg+="\nCould not remove team role - check bot permissions."
    await post_tx(interaction.guild,title_line,body_line,found_team,followup=interaction.followup,msg=msg)

@bot.tree.command(name="grant_extra_demand",description="[Owner] Grant a player an extra demand token")
@app_commands.describe(player="The player",amount="Number of extra demands (default 1)")
async def grant_extra_demand(interaction:discord.Interaction,player:discord.Member,amount:int=1):
    await interaction.response.defer(ephemeral=True)
    if interaction.user.id not in EXTRA_DEMAND_GRANT_USER_IDS and not is_admin(interaction):
        await interaction.followup.send("No permission.",ephemeral=True); return
    data=await load_data(); uid=str(player.id)
    data.setdefault("extra_demands",{})[uid]=data["extra_demands"].get(uid,0)+amount; await save_data(data)
    await interaction.followup.send(f"Granted **{amount}** extra demand(s) to **{player.display_name}**. Total: **{data['extra_demands'][uid]}**.",ephemeral=True)

@bot.tree.command(name="promote_coach",description="Promote a player to Assistant Head Coach")
@app_commands.describe(player="The player to promote")
async def promote_coach(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); rid,team=get_team_for_hc(data,interaction.user.id)
    if not team and is_staff(interaction):
        for r,t in data["teams"].items():
            if any(x["id"]==str(player.id) for x in t.get("roster",[])): rid,team=r,t; break
    if not team:
        await interaction.followup.send("You're not HC of any team.",ephemeral=True); return
    roster=team.setdefault("roster",[])
    if not any(r["id"]==str(player.id) for r in roster):
        await interaction.followup.send(f"{player.display_name} must be on roster first.",ephemeral=True); return
    blox=await bloxlink_lookup(player.id,interaction.guild.id); rbx=blox.get("roblox_username","")
    team.update(ahc_id=str(player.id),ahc_name=player.name,ahc_roblox=rbx)
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
    title_line,body_line=await build_coach_action("assistant coach promotion",player,team,interaction.guild)
    msg=f"**{player.display_name}** promoted to AHC of **{team['name']}**."
    if rf: msg+="\nCould not update roles - check bot permissions."
    await post_tx(interaction.guild,title_line,body_line,team,followup=interaction.followup,msg=msg)

@bot.tree.command(name="demote_coach",description="Demote the Assistant Head Coach back to player")
@app_commands.describe(player="The AHC to demote")
async def demote_coach(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); rid,team=get_team_for_hc(data,interaction.user.id)
    if not team and is_staff(interaction):
        for r,t in data["teams"].items():
            if t.get("ahc_id")==str(player.id): rid,team=r,t; break
    if not team:
        await interaction.followup.send("You're not HC of any team.",ephemeral=True); return
    if team.get("ahc_id")!=str(player.id):
        await interaction.followup.send(f"{player.display_name} is not AHC of **{team['name']}**.",ephemeral=True); return
    team.update(ahc_id=None,ahc_name=None,ahc_roblox="")
    for r in team.get("roster",[]):
        if r["id"]==str(player.id): r["role"]="Player"
    await save_data(data)
    ahc_role=interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID); rf=False
    try:
        if ahc_role and ahc_role in player.roles: await player.remove_roles(ahc_role,reason="Demoted from AHC")
    except discord.Forbidden: rf=True
    team_role=get_role(interaction.guild,rid)
    title_line,body_line=await build_coach_action("assistant coach demotion",player,team,interaction.guild)
    msg=f"**{player.display_name}** demoted from AHC of **{team['name']}**."
    if rf: msg+="\nCould not remove AHC role - check bot permissions."
    await post_tx(interaction.guild,title_line,body_line,team,followup=interaction.followup,msg=msg)

@bot.tree.command(name="disband",description="Disband a team — removes all players and coaches")
@app_commands.describe(confirm="Type DISBAND to confirm",team_role="(Staff only) Target team")
async def disband(interaction:discord.Interaction,confirm:str,team_role:discord.Role=None):
    await interaction.response.defer(ephemeral=True)
    if confirm.upper()!="DISBAND":
        await interaction.followup.send("Type DISBAND exactly.",ephemeral=True); return
    data=await load_data(); rid,team=get_team_for_hc(data,interaction.user.id)
    if not team and is_staff(interaction) and team_role:
        rid=str(team_role.id); team=get_team_by_role(data,team_role.id)
    if not team:
        await interaction.followup.send("Not HC. Staff must also provide team_role.",ephemeral=True); return
    team_name=team["name"]; former=list(team.get("roster",[])); former_size=len(former)
    team.update(roster=[],head_coach_id=None,head_coach_name=None,head_coach_roblox="",ahc_id=None,ahc_name=None,ahc_roblox="")
    await save_data(data)
    tr=get_role(interaction.guild,rid); fail=0
    if tr:
        for md in former:
            try:
                m=(interaction.guild.get_member(int(md["id"])) or await interaction.guild.fetch_member(int(md["id"])))
                if tr in m.roles: await m.remove_roles(tr,reason=f"{team_name} disbanded")
            except discord.Forbidden: fail+=1
            except: pass
    embed=discord.Embed(title="team disbanded",color=0xED4245,description=f"**{team_name}** disbanded.\n**{former_size}** players removed.")
    if tr: embed.add_field(name="Team",value=tr.mention,inline=True)
    logo=team.get("logo_url","") or _league_thumb(interaction.guild)
    if logo: embed.set_thumbnail(url=logo)
    embed.set_footer(text=f"Disbanded by {interaction.user.display_name} | {UFF_FOOTER}"); embed.timestamp=datetime.utcnow()
    note=f"\nCouldn't strip role from {fail} member(s)." if fail else ""
    ch=await get_tx_ch(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        await interaction.followup.send(f"**{team_name}** disbanded. Posted to {ch.mention}.{note}",ephemeral=True)
    else:
        await interaction.followup.send(embed=embed,ephemeral=False)

@bot.tree.command(name="roster",description="View a team's current roster")
@app_commands.describe(team_role="The team's Discord role")
async def roster_cmd(interaction:discord.Interaction,team_role:discord.Role):
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); team=get_team_by_role(data,team_role.id)
    if not team:
        await interaction.followup.send(f"{team_role.mention} isn't registered.",ephemeral=True); return
    roster=team.get("roster",[]); color=team.get("color",UFF_COLOR)
    emoji=team.get("emoji",""); prefix=f"{emoji} " if emoji else ""
    embed=discord.Embed(title=f"{prefix}{team['name'].lower()} roster",description=f"roster: **{len(roster)}/{MAX_ROSTER}**",color=color)
    hc_id=team.get("head_coach_id")
    if hc_id:
        rbx=f" `{team.get('head_coach_roblox','')}`" if team.get("head_coach_roblox") else ""
        embed.add_field(name="head coach:",value=f"<@{hc_id}> (@{team.get('head_coach_name','')}) ✓{rbx}",inline=False)
    ahc_lines=[]; pl_lines=[]
    for r in roster:
        rbx=f" `{r['roblox']}`" if r.get("roblox") else ""
        line=f"<@{r['id']}> (@{r['name']}) ✓{rbx}"; role=r.get("role","Player")
        if role=="Assistant Head Coach": ahc_lines.append(line)
        elif role!="Head Coach": pl_lines.append(line)
    if ahc_lines: embed.add_field(name="assistant head coach:",value="\n".join(ahc_lines),inline=False)
    if pl_lines:  embed.add_field(name="players:",value="\n".join(pl_lines),inline=False)
    elif not roster: embed.add_field(name="players:",value="*No players yet.*",inline=False)
    logo=team.get("logo_url","") or _league_thumb(interaction.guild)
    if logo: embed.set_thumbnail(url=logo)
    embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
    await interaction.followup.send(embed=embed,ephemeral=True)

@bot.tree.command(name="coaches",description="View all head coaches across the league")
async def coaches_cmd(interaction:discord.Interaction):
    public=interaction.channel_id==COACHES_CHANNEL_ID
    await interaction.response.defer(ephemeral=not public)
    data=await load_data(); teams=data.get("teams",{})
    embed=discord.Embed(title="head coaches",color=UFF_COLOR)
    if not teams:
        embed.description="*No teams registered yet.*"
    else:
        lines=[]
        for rid,team in teams.items():
            hc_id=team.get("head_coach_id"); hc_name=team.get("head_coach_name",""); hc_rbx=team.get("head_coach_roblox","")
            team_emoji=team.get("emoji","")
            if hc_id:
                rbx_str=f" `{hc_rbx}`" if hc_rbx else ""
                hc_str=f"<@{hc_id}> (@{hc_name}) ✓{rbx_str}"
            else: hc_str="*vacant*"
            tr=get_role(interaction.guild,rid); role_mention=tr.mention if tr else team["name"]
            emoji_prefix=f"{team_emoji} " if team_emoji else ""
            lines.append(f"{emoji_prefix}{role_mention} — {hc_str}")
        chunks=[]; cur=""
        for line in lines:
            candidate=f"{cur}\n{line}" if cur else line
            if len(candidate)>1000: chunks.append(cur); cur=line
            else: cur=candidate
        if cur: chunks.append(cur)
        for chunk in (chunks or ["*None*"]):
            embed.add_field(name="\u200b",value=chunk,inline=False)
    thumb=ZEVORA_LOGO_URL or UFF_THUMBNAIL or _league_thumb(interaction.guild)
    if thumb: embed.set_thumbnail(url=thumb)
    embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
    await interaction.followup.send(embed=embed,ephemeral=not public)

# ══════════════════════════════════════════════════════════════════════
# STREAM POST
# ══════════════════════════════════════════════════════════════════════

@bot.tree.command(name="stream_post",description="[Staff] Post a stream announcement")
@app_commands.describe(season="Season number",series="Series label",
    team1="First team role",team1_record="Team 1 record",
    team2="Second team role",team2_record="Team 2 record",stream_url="Stream link")
async def stream_post(interaction:discord.Interaction,season:str,series:str,
    team1:discord.Role,team1_record:str,team2:discord.Role,team2_record:str,stream_url:str):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    ch=await get_stream_ch(interaction.guild)
    if not ch:
        await interaction.followup.send("Stream channel not found.",ephemeral=True); return
    await ch.send(
        content=f"@here\n**Season {season} - Series {series}**\n{team1.mention} ({team1_record}) vs {team2.mention} ({team2_record})\n{stream_url}",
        allowed_mentions=discord.AllowedMentions(everyone=True,roles=True))
    await interaction.followup.send(f"Stream post sent to {ch.mention}!",ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# PICKUP COMMANDS
# ══════════════════════════════════════════════════════════════════════

def _get_team_display(member: discord.Member, data: dict) -> tuple[str, str]:
    """
    Return (emoji_team_str, team_name) for a member by checking their roles
    against registered teams. Falls back to their display name if not found.
    emoji_team_str = "<:emoji:id> Team Name" or just "Team Name"
    """
    member_role_ids = {str(r.id) for r in member.roles}
    for rid, team in data.get("teams", {}).items():
        if rid in member_role_ids:
            emoji = team.get("emoji", "")
            name  = team.get("name", "")
            display = f"{emoji} {name}".strip() if emoji else name
            return display, name
    return member.display_name, member.display_name

@bot.tree.command(name="pickup_ranked",description="Challenge another player to a ranked UFF pickup")
@app_commands.describe(opponent="The player to challenge", game_link="Roblox game link")
async def pickup_ranked(interaction:discord.Interaction, opponent:discord.Member, game_link:str):
    await interaction.response.defer(ephemeral=True)
    if (not ({r.id for r in interaction.user.roles}&PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction)):
        await interaction.followup.send("Missing required role.",ephemeral=True); return
    if opponent.id==interaction.user.id:
        await interaction.followup.send("Can't challenge yourself!",ephemeral=True); return
    if opponent.bot:
        await interaction.followup.send("Can't challenge a bot!",ephemeral=True); return
    data=await load_data(); _expire_pending(data)
    uid=str(interaction.user.id); oid=str(opponent.id)
    for m in data.get("pending",{}).values():
        if m["challenger_id"]==uid or m["opponent_id"]==uid:
            await interaction.followup.send("You already have an active pending challenge.",ephemeral=True); return
        if m["challenger_id"]==oid or m["opponent_id"]==oid:
            await interaction.followup.send(f"**{opponent.display_name}** already has a pending challenge.",ephemeral=True); return

    # Check cooldown for BOTH players
    cd,remaining=on_cooldown(data,interaction.user.id)
    if cd:
        e=discord.Embed(title="cooldown active",
            description=f"You played recently. You can challenge again in **{remaining}**.",color=0xE84040)
        await interaction.followup.send(embed=e,ephemeral=True); return
    cd2,remaining2=on_cooldown(data,opponent.id)
    if cd2:
        e=discord.Embed(title="opponent on cooldown",
            description=f"**{opponent.display_name}** played recently and can't accept for **{remaining2}**.",color=0xE84040)
        await interaction.followup.send(embed=e,ephemeral=True); return

    # Auto-detect teams from roles
    your_team_display, your_team_name         = _get_team_display(interaction.user, data)
    opponent_team_display, opponent_team_name = _get_team_display(opponent, data)

    p1=get_player(data,interaction.user.id); p1["username"]=interaction.user.display_name
    p2=get_player(data,opponent.id);         p2["username"]=opponent.display_name
    now_iso=datetime.utcnow().isoformat()
    # Set last_game NOW for both — cooldown starts at challenge time, not acceptance
    p1["last_game"]=now_iso
    p2["last_game"]=now_iso
    mid=f"{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("pending",{})[mid]={
        "challenger_id":str(interaction.user.id),"opponent_id":str(opponent.id),
        "challenger_name":interaction.user.display_name,"opponent_name":opponent.display_name,
        "challenger_team":your_team_display,"opponent_team":opponent_team_display,
        "game_link":game_link,"timestamp":now_iso,
        "match_id":mid,"guild_id":interaction.guild.id}
    await save_data(data)
    r1,e1,_=get_rank(p1["elo"]); r2,e2,_=get_rank(p2["elo"]); lt=_league_thumb(interaction.guild)
    dm=discord.Embed(title="Ranked Pickup Challenge!",
        description=f"**{interaction.user.display_name}** wants a ranked pickup. Expires **30 min**.",color=UFF_COLOR)
    dm.add_field(name=f"🟡 {interaction.user.display_name}",
        value=f"`{interaction.user.name}`\n{your_team_display}\nRank: `{e1} {r1}`",inline=True)
    dm.add_field(name="\u200b",value="**— VS —**",inline=True)
    dm.add_field(name=f"🔵 {opponent.display_name}",
        value=f"`{opponent.name}`\n{opponent_team_display}\nRank: `{e2} {r2}`",inline=True)
    dm.add_field(name="Game Link",value=f"[Click here]({game_link})",inline=False)
    if UFF_BANNER: dm.set_image(url=UFF_BANNER)
    if lt: dm.set_thumbnail(url=lt)
    dm.set_footer(text=f"Challenge by {interaction.user.display_name} | {UFF_FOOTER}"); dm.timestamp=datetime.utcnow()
    view=RankedPickupView(mid,interaction.user.id,opponent.id,interaction.user.display_name,
        opponent.display_name,your_team_display,opponent_team_display,game_link,interaction.guild.id)
    try: await opponent.send(embed=dm,view=view)
    except discord.Forbidden:
        data.get("pending",{}).pop(mid,None); await save_data(data)
        await interaction.followup.send(f"Can't DM **{opponent.display_name}**.",ephemeral=True); return
    ack=discord.Embed(title="Challenge Sent!",
        description=f"Sent to **{opponent.display_name}**.\n\n🟡 You: {your_team_display}\n🔵 Them: {opponent_team_display}\n\nPosted publicly only if accepted.",
        color=0x57F287)
    await interaction.followup.send(embed=ack,ephemeral=True)

@bot.tree.command(name="pickup_casual",description="Challenge to a casual pickup — no ELO")
@app_commands.describe(opponent="Player to challenge", game_link="Roblox game link")
async def pickup_casual(interaction, opponent:discord.Member, game_link:str):
    await _run_casual(interaction, opponent, game_link)

@bot.tree.command(name="casual_pickup",description="Challenge to a casual pickup — no ELO")
@app_commands.describe(opponent="Player to challenge", game_link="Roblox game link")
async def casual_pickup(interaction, opponent:discord.Member, game_link:str):
    await _run_casual(interaction, opponent, game_link)

@bot.tree.command(name="pickup_results",description="Submit ranked pickup results + screenshot")
@app_commands.describe(winner="Who won?",loser="Who lost?",winner_score="Winner's score",
                       loser_score="Loser's score",screenshot="Scoreboard screenshot")
async def pickup_results(interaction:discord.Interaction, winner:discord.Member,
                         loser:discord.Member, winner_score:int, loser_score:int,
                         screenshot:discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    if not (is_staff(interaction) or
            interaction.user.id in [winner.id, loser.id]):
        await interaction.followup.send("Only the players or staff can submit results.",ephemeral=True); return
    if winner.id==loser.id:
        await interaction.followup.send("Winner and loser can't be the same person.",ephemeral=True); return

    data=await load_data()

    # Clear any matching pending match
    uid=str(interaction.user.id); pending=data.get("pending",{}); mkey=None
    for k,m in pending.items():
        ids={m.get("challenger_id"),m.get("opponent_id")}
        if str(winner.id) in ids and str(loser.id) in ids:
            mkey=k; break
    if mkey: data["pending"].pop(mkey,None)

    # Update ELO and records
    wp=get_player(data,winner.id); lp=get_player(data,loser.id)
    wp["username"]=winner.display_name; lp["username"]=loser.display_name
    old_w,old_l=wp["elo"],lp["elo"]
    wp["elo"]+=WIN_ELO; lp["elo"]=max(0,lp["elo"]-LOSS_ELO)
    wp["wins"]+=1; lp["losses"]+=1

    # Auto-detect team names for the match record
    winner_team, _ = _get_team_display(winner, data)
    loser_team,  _ = _get_team_display(loser, data)

    data.setdefault("matches",[]).append({
        "winner_id":str(winner.id),"winner_name":winner.display_name,
        "loser_id":str(loser.id),"loser_name":loser.display_name,
        "winner_score":winner_score,"loser_score":loser_score,
        "challenger_team":winner_team,"opponent_team":loser_team,
        "date":datetime.utcnow().isoformat()})
    await save_data(data)

    wr,we,wcolor=get_rank(wp["elo"]); lr,le,_=get_rank(lp["elo"])
    lt=_league_thumb(interaction.guild)
    embed=discord.Embed(title="pickup results",color=wcolor)
    embed.add_field(name="🏆 Winner",
        value=(f"<@{winner.id}> **{winner.display_name}**\n"
               f"{winner_team}\nScore: **{winner_score}**\n"
               f"ELO: `{old_w}` → `{wp['elo']}` **(+{WIN_ELO})**\nRank: `{we} {wr}`"),inline=True)
    embed.add_field(name="❌ Loser",
        value=(f"<@{loser.id}> **{loser.display_name}**\n"
               f"{loser_team}\nScore: **{loser_score}**\n"
               f"ELO: `{old_l}` → `{lp['elo']}` **(-{LOSS_ELO})**\nRank: `{le} {lr}`"),inline=True)
    embed.add_field(name="Final Score",
        value=f"**{winner.display_name}** `{winner_score} — {loser_score}` **{loser.display_name}**",inline=False)
    embed.set_image(url=screenshot.url)
    if lt: embed.set_thumbnail(url=lt)
    embed.set_footer(text=f"{UFF_FOOTER} • Submitted by {interaction.user.display_name}")
    embed.timestamp=datetime.utcnow()
    ch=await get_pickup_ch(interaction.guild)
    target=ch if (ch and ch.id!=interaction.channel_id) else interaction.channel
    await interaction.followup.send("Results posted!",ephemeral=True)
    await target.send(embed=embed)

@bot.tree.command(name="pickup_profile",description="View UFF pickup rank and stats")
@app_commands.describe(player="Player to look up (blank = yourself)")
async def pickup_profile(interaction:discord.Interaction,player:discord.Member=None):
    await interaction.response.defer(ephemeral=True)
    target=player or interaction.user; data=await load_data(); p=get_player(data,target.id); elo=p["elo"]
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
    top=sorted(players.items(),key=lambda x:x[1]["elo"],reverse=True)[:15]; medals=["🥇","🥈","🥉"]
    lines=[f"{medals[i] if i<3 else str(i+1)+'.'} **{p.get('username') or '<@'+uid+'>'}** — {emoji} `{rank}` | ELO `{p['elo']}` | {p['wins']}W {p['losses']}L"
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
    lines=[f"**{m['winner_name']}** `{m.get('winner_score','?')}–{m.get('loser_score','?')}` {m['loser_name']}" for m in matches]
    embed=discord.Embed(title="UFF Pickup — Recent Results",description="\n".join(lines),color=0x4090E8)
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
        await interaction.followup.send("Admin only.",ephemeral=True); return
    data=await load_data()
    data["players"][str(player.id)]={"elo":STARTING_ELO,"wins":0,"losses":0,"last_game":None,"username":player.display_name}
    await save_data(data)
    await interaction.followup.send(f"Reset **{player.display_name}** ELO to `{STARTING_ELO}`.",ephemeral=True)

@bot.tree.command(name="adjust_elo",description="[Admin] Manually adjust a player's ELO, wins, and losses")
@app_commands.describe(player="Target player",
                       elo="ELO to add (negative to subtract, 0 to skip)",
                       wins="Wins to add (negative to subtract, 0 to skip)",
                       losses="Losses to add (negative to subtract, 0 to skip)")
@app_commands.default_permissions(administrator=True)
async def adjust_elo(interaction:discord.Interaction, player:discord.Member,
                     elo:int=0, wins:int=0, losses:int=0):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send("Admin only.",ephemeral=True); return
    data=await load_data(); p=get_player(data,player.id)
    old_elo=p["elo"]; old_w=p["wins"]; old_l=p["losses"]
    p["elo"]    = max(0, p["elo"]    + elo)
    p["wins"]   = max(0, p["wins"]   + wins)
    p["losses"] = max(0, p["losses"] + losses)
    p["username"]=player.display_name
    await save_data(data)
    rank, emoji, _ = get_rank(p["elo"])
    lines = [f"**{player.display_name}** updated:"]
    if elo    != 0: lines.append(f"ELO: `{old_elo}` → `{p['elo']}` ({'+' if elo>=0 else ''}{elo})")
    if wins   != 0: lines.append(f"Wins: `{old_w}` → `{p['wins']}` ({'+' if wins>=0 else ''}{wins})")
    if losses != 0: lines.append(f"Losses: `{old_l}` → `{p['losses']}` ({'+' if losses>=0 else ''}{losses})")
    lines.append(f"Rank: {emoji} `{rank}`")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="clear_cooldown",description="[Admin] Clear a player's cooldown")
@app_commands.describe(player="Player to clear")
@app_commands.default_permissions(administrator=True)
async def clear_cooldown(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send("Admin only.",ephemeral=True); return
    data=await load_data(); get_player(data,player.id)["last_game"]=None; await save_data(data)
    await interaction.followup.send(f"Cleared cooldown for **{player.display_name}**.",ephemeral=True)

# ── SUSPENSIONS ───────────────────────────────────────────────────────
@bot.tree.command(name="suspension",description="[Staff] Issue a suspension to a player")
@app_commands.describe(player="The player to suspend")
async def suspension(interaction:discord.Interaction,player:discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message("No permission.",ephemeral=True); return
    view=SuspView(target=player,issuer_id=interaction.user.id)
    await interaction.response.send_message(
        content=f"**Issuing suspension for {player.display_name}**\nSelect up to {MAX_SUSPENSION_REASONS} reasons.",
        view=view,ephemeral=True)

@bot.tree.command(name="unsuspend",description="[Staff] Clear a player's suspension")
@app_commands.describe(player="The player to unsuspend")
async def unsuspend(interaction:discord.Interaction,player:discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not can_issue_suspension(interaction):
        await interaction.followup.send("No permission.",ephemeral=True); return
    data=await load_data(); cleared=False
    for s in data.get("suspensions",[]):
        if s.get("player_id")==str(player.id) and not s.get("cleared",False):
            s.update(cleared=True,cleared_by=str(interaction.user.id),
                cleared_by_name=interaction.user.display_name,cleared_date=datetime.utcnow().isoformat()); cleared=True
    await save_data(data)
    susp_role=interaction.guild.get_role(SUSPENSION_ROLE_ID); role_removed=False
    if susp_role and susp_role in player.roles:
        try: await player.remove_roles(susp_role,reason="Unsuspended"); role_removed=True
        except discord.Forbidden: pass
    embed=discord.Embed(title="player unsuspended",color=0x57F287)
    embed.add_field(name="Player",value=f"<@{player.id}> ({player.display_name})",inline=False)
    embed.add_field(name="Status",value="**Cleared** — eligible to play",inline=False)
    if player.avatar: embed.set_thumbnail(url=player.avatar.url)
    embed.set_footer(text=f"Cleared by {interaction.user.display_name} | {UFF_FOOTER}"); embed.timestamp=datetime.utcnow()
    ch=await get_susp_ch(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        note="" if cleared else "\n*(No open records found.)*"
        rnote="" if role_removed else "\nSuspension role couldn't be removed."
        await interaction.followup.send(f"{player.display_name} unsuspended. Posted to {ch.mention}.{note}{rnote}",ephemeral=True)
    else:
        await interaction.followup.send("Suspension channel not found.",ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# SCHEDULING THREAD COMMANDS
# ══════════════════════════════════════════════════════════════════════

async def _make_thread(interaction, role1, role2, t1, t2):
    """Create one private scheduling thread for a matchup."""
    e1=(t1.get("emoji","")+" ") if t1 and t1.get("emoji") else ""
    e2=(t2.get("emoji","")+" ") if t2 and t2.get("emoji") else ""
    name1=t1["name"] if t1 else role1.name
    name2=t2["name"] if t2 else role2.name
    tname=_clean_thread_name(f"{e1}{name1} vs {e2}{name2}")
    thread=await interaction.channel.create_thread(name=tname,type=discord.ChannelType.private_thread,reason=f"Scheduling: {tname}")
    to_add=set()
    for uid in [t1.get("head_coach_id") if t1 else None, t1.get("ahc_id") if t1 else None,
                t2.get("head_coach_id") if t2 else None, t2.get("ahc_id") if t2 else None]:
        if not uid: continue
        try:
            m=(interaction.guild.get_member(int(uid)) or await interaction.guild.fetch_member(int(uid)))
            to_add.add(m)
        except: pass
    for sid in STAFF_ROLE_IDS:
        sr=interaction.guild.get_role(sid)
        if sr: to_add.update(sr.members)
    to_add.add(interaction.user)
    for member in to_add:
        try: await thread.add_user(member)
        except: pass
    hc1=t1.get("head_coach_id") if t1 else None; ahc1=t1.get("ahc_id") if t1 else None
    hc2=t2.get("head_coach_id") if t2 else None; ahc2=t2.get("ahc_id") if t2 else None
    def _cv(hc,ahc):
        return f"HC: {'<@'+hc+'>' if hc else 'vacant'}\nAHC: {'<@'+ahc+'>' if ahc else 'vacant'}"
    embed=discord.Embed(title="Scheduling Thread",
        description=f"{role1.mention} **vs** {role2.mention}\n\nUse this thread to schedule.\nRun `/thread_delete` when done.",color=UFF_COLOR)
    embed.add_field(name=name1,value=_cv(hc1,ahc1),inline=True)
    embed.add_field(name=name2,value=_cv(hc2,ahc2),inline=True)
    lt=_league_thumb(interaction.guild)
    if lt: embed.set_thumbnail(url=lt)
    embed.set_footer(text=UFF_FOOTER); embed.timestamp=datetime.utcnow()
    pings=" ".join(f"<@{uid}>" for uid in [hc1,ahc1,hc2,ahc2] if uid)
    await thread.send(content=pings or None,embed=embed,allowed_mentions=discord.AllowedMentions(users=True))
    return thread

@bot.tree.command(name="thread_create",description="[Staff] Create a private scheduling thread for two teams")
@app_commands.describe(team1="First team role",team2="Second team role")
async def thread_create(interaction:discord.Interaction,team1:discord.Role,team2:discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return
    data=await load_data()
    t1=data["teams"].get(str(team1.id)); t2=data["teams"].get(str(team2.id))
    if not t1:
        await interaction.followup.send(f"{team1.mention} not registered.",ephemeral=True); return
    if not t2:
        await interaction.followup.send(f"{team2.mention} not registered.",ephemeral=True); return
    try:
        thread=await _make_thread(interaction,team1,team2,t1,t2)
        await interaction.followup.send(f"Thread created: {thread.mention}",ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Missing Create Private Threads permission.",ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Could not create thread: {e}",ephemeral=True)

@bot.tree.command(name="schedule",
    description="[Staff] Paste the week's schedule — auto-creates all scheduling threads at once")
@app_commands.describe(schedule_text="Paste the full schedule message here")
async def schedule_cmd(interaction:discord.Interaction,schedule_text:str):
    await interaction.response.defer(ephemeral=True)
    if not is_staff(interaction):
        await interaction.followup.send("Staff only.",ephemeral=True); return

    pairs=_parse_schedule_pairs(schedule_text,interaction.guild)

    if not pairs:
        await interaction.followup.send(
            "No matchups found. Make sure each game has **vs** between the team names.",ephemeral=True); return

    data=await load_data()
    created=[]; skipped=[]

    for role1,role2 in pairs:
        t1=data["teams"].get(str(role1.id))
        t2=data["teams"].get(str(role2.id))
        try:
            thread=await _make_thread(interaction,role1,role2,t1,t2)
            created.append(f"• {thread.mention} — **{role1.name}** vs **{role2.name}**")
        except discord.Forbidden:
            skipped.append(f"`{role1.name} vs {role2.name}` — missing Create Private Threads permission")
            break
        except Exception as e:
            skipped.append(f"`{role1.name} vs {role2.name}` — {e}")
        await asyncio.sleep(0.75)

    result=f"Created **{len(created)}** thread(s)."
    if created:
        chunk="\n".join(created)
        result+="\n"+chunk if len(chunk)<=1800 else "\n*(too many to list)*"
    if skipped:
        result+=f"\n\nSkipped **{len(skipped)}**:\n"+"\n".join(skipped[:10])
    await interaction.followup.send(result,ephemeral=True)

@bot.tree.command(name="thread_delete",description="[Staff/HC/AHC] Delete this scheduling thread")
async def thread_delete(interaction:discord.Interaction):
    if not isinstance(interaction.channel,discord.Thread):
        await interaction.response.send_message("Must be used inside a thread.",ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    data=await load_data(); uid=str(interaction.user.id)
    is_hc=any(t.get("head_coach_id")==uid for t in data["teams"].values())
    is_ahc=any(t.get("ahc_id")==uid for t in data["teams"].values())
    if not is_staff(interaction) and not is_hc and not is_ahc:
        await interaction.followup.send("Only staff, HCs, or AHCs can delete threads.",ephemeral=True); return
    thread=interaction.channel
    try: await interaction.followup.send(f"Deleting **{thread.name}**...",ephemeral=True)
    except: pass
    try: await thread.delete()
    except Exception as e: print(f"[thread_delete] Error: {e}")

@bot.tree.command(name="season_reset",
    description="[Admin] Reset all teams, rosters, and coaches — keeps ELO/pickup stats intact")
@app_commands.describe(
    confirm="Type SEASON RESET to confirm — this cannot be undone"
)
@app_commands.default_permissions(administrator=True)
async def season_reset(interaction: discord.Interaction, confirm: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send("Admin only.", ephemeral=True); return
    if confirm.upper() != "SEASON RESET":
        await interaction.followup.send(
            "Type `SEASON RESET` exactly in the confirm field.", ephemeral=True); return

    data = await load_data()

    # Count what we're clearing for the summary
    team_count   = len(data.get("teams", {}))
    total_roster = sum(len(t.get("roster",[])) for t in data.get("teams",{}).values())

    # Wipe all team registrations, rosters, and coach assignments
    data["teams"]        = {}
    # Clear pending offers and pending matches — they reference teams that no longer exist
    data["offers"]       = {}
    data["pending"]      = {}
    data["casual_pending"] = {}
    # Reset demand tracking so everyone gets their demand back next season
    data["demand_used"]  = {}
    data["extra_demands"] = {}
    # NOTE: "players" (ELO/pickup stats), "matches", and "suspensions" are kept intact

    await save_data(data)

    embed = discord.Embed(
        title="🔄 Season Reset Complete",
        color=UFF_COLOR,
        description=(
            f"All transaction data has been cleared for the new season.\n\n"
            f"**Cleared:**\n"
            f"• `{team_count}` team registrations\n"
            f"• `{total_roster}` roster entries\n"
            f"• All head coach / AHC assignments\n"
            f"• All pending offers and challenges\n"
            f"• All demand release tokens (everyone gets a fresh one)\n\n"
            f"**Preserved:**\n"
            f"• ELO ratings and pickup records\n"
            f"• Match history\n"
            f"• Suspension records\n\n"
            f"Run `/set_team` for each rebranded team to re-register them."
        )
    )
    embed.set_footer(text=f"Reset by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp = datetime.utcnow()

    ch = await get_tx_ch(interaction.guild)
    if ch:
        await ch.send(embed=embed)
    await interaction.followup.send(
        f"✅ Season reset complete. `{team_count}` teams and `{total_roster}` roster entries cleared.\n"
        f"ELO and pickup stats preserved. Posted summary to {ch.mention if ch else 'transactions channel'}.",
        ephemeral=True
    )

if __name__=="__main__":
    bot.run(TOKEN)
