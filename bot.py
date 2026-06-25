"""
UFF Discord Bot — Python
United Flag Football League — Pickup Ranked System + Transactions

CHANGES FROM PREVIOUS VERSION:
  - Data persisted in Railway PostgreSQL (no more resets on redeploy)
  - /set_team  simplified: only takes team_role. Name pulled from role name,
    logo pulled from role icon. All transactions go to TRANSACTIONS_CHANNEL_ID.
  - Logo is now embed thumbnail (inside embed), NOT a plain message before it.
  - Promote/demote coach responses are ephemeral (only visible to the user).
  - assign_hc   → gives Discord role HEAD_COACH_ROLE_ID
  - promote_coach → gives ASSISTANT_COACH_ROLE_ID, removes HEAD_COACH_ROLE_ID
  - demote_coach  → removes ASSISTANT_COACH_ROLE_ID
  - /coaches shows team logo as thumbnail for each team listed

LATEST UPDATE:
  - Team/league logo thumbnails now always fall back: team logo_url -> ZEVORA_LOGO_URL -> guild icon,
    so thumbnails never render blank even if a team has no logo set.
  - /coaches is now a single vertical list (no more two-column layout) and uses the
    league logo (ZEVORA_LOGO_URL) as its thumbnail instead of a team logo.
  - Transaction pings (team role mention) are now actually sent as a separate plain-text
    message AFTER the embed (previously the ping variable was built but never sent).
    This applies to /release, /demand, /promote_coach, /demote_coach, and offer-accept (signing).
  - All non-staff command responses are now ephemeral. Where a command also produces a
    real public record (pickup results, disband notice), the ephemeral ack now stays
    private to the user while the actual result is still posted publicly to the channel.
  - Staff roles updated: removed Moderators (1404271074623099040); added Co-Founder,
    Zevora, and Advisory roles.
  - Added ZEVORA_LOGO_URL env var for the league logo (set in Railway).

TRANSACTIONS FLOW:
  /set_team            — [Staff] Register a Discord role as a UFF team
  /set_team_image      — [Staff] Override the team logo URL for a team role
  /assign_hc           — [Staff] Assign a head coach to a team
  /offer               — [HC/AHC/Staff] Send a player a roster offer via DM (12h window)
  /release             — [HC/AHC/Staff] Release a player (auto-detected team)
  /demand      — Player demands their own release (1 lifetime demand)
  /grant_extra_demand  — [Owner IDs only] Give a player an extra demand token
  /promote_coach       — [HC/Staff] Promote a player to AHC
  /demote_coach        — [HC/Staff] Demote the AHC back to player
  /disband             — [HC/Staff] Remove all players and coaches from a team
  /roster              — [Public] View a team's roster
  /coaches             — [Public] View all head coaches across the league

BLOXLINK:
  Requires BLOXLINK_API_KEY env var.
  Everyone in the league is verified, so Roblox username + headshot always resolves.

PICKUP FLOW:
  1. /pickup_ranked  → challenger selects opponent + teams + game link
  2. Bot DMs opponent with Accept / Decline buttons (NO public post yet)
  3. Opponent clicks Accept → public matchup embed posts to pickup channel
  4. /pickup_results → submit screenshot + winner → ELO updated, results embed posted

  /pickup_casual or /casual_pickup → same flow, no ELO

  /suspension → staff-only. Pick a player + up to 5 reasons (stackable).
  /unsuspend  → staff-only. Clear a suspension and post a clearance notice.

DATABASE SETUP (run once on Railway):
  The bot auto-creates the required table on startup via init_db().
  Just add a PostgreSQL plugin on Railway and set DATABASE_URL env var.
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import aiohttp
from datetime import datetime, timedelta
import asyncpg

# ─────────────────────────────────────────────────────────────────────
# ENVIRONMENT VARIABLES  (set these in Railway)
# ─────────────────────────────────────────────────────────────────────
TOKEN               = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID            = int(os.getenv("OWNER_DISCORD_ID", "0"))
SECRET_KEY          = os.getenv("SECRET_KEY", "")
QBB_CHANNEL_ID      = int(os.getenv("QBB_CHANNEL_ID", "0"))
BLOXLINK_API_KEY    = os.getenv("BLOXLINK_API_KEY", "")
DATABASE_URL        = os.getenv("DATABASE_URL", "")   # set by Railway PostgreSQL plugin

UFF_THUMBNAIL  = os.getenv("UFF_THUMBNAIL_URL", "")
UFF_BANNER     = os.getenv("UFF_BANNER_URL",    "")
ZEVORA_LOGO_URL = os.getenv("ZEVORA_LOGO_URL",  "")   # league logo — used by /coaches and as a fallback thumbnail

# Channel where ALL transactions (signings, releases, demands, etc.) are posted
TRANSACTIONS_CHANNEL_ID = 1262200420151984152

UFF_FOOTER   = "United Flag Football League"
UFF_COLOR    = 0xF0C040
CASUAL_COLOR = 0x5865F2

GUILD_ID = 1262200419564785755   # UFF server — instant command sync

# ─────────────────────────────────────────────────────────────────────
# ROSTER CONFIG
# ─────────────────────────────────────────────────────────────────────
MAX_ROSTER = 20

# ─────────────────────────────────────────────────────────────────────
# ELO CONFIG
# ─────────────────────────────────────────────────────────────────────
STARTING_ELO     = 900
WIN_ELO          = 100
LOSS_ELO         = 100
COOLDOWN_MINUTES = 30

# ─────────────────────────────────────────────────────────────────────
# COACH ROLE IDS  (assigned/removed by bot automatically)
# ─────────────────────────────────────────────────────────────────────
HEAD_COACH_ROLE_ID       = 1262200419615117330
ASSISTANT_COACH_ROLE_ID  = 1267278742389264406

# ─────────────────────────────────────────────────────────────────────
# ROLES ALLOWED TO START A PICKUP
# ─────────────────────────────────────────────────────────────────────
PICKUP_ALLOWED_ROLE_IDS = {
    1269693904815521994,
    1404271074623099040,
    1404271002241728617,
    1429344923865448550,
    1262200419686285342,
    1401450124424642561,
}

# Staff roles that can run ANY transaction on ANY team
STAFF_ROLE_IDS = {
    1404271002241728617,  # League Boards
    1429344923865448550,  # Operations Director
    1262200419686285342,  # Commissioner
    1401450124424642561,  # Founder
    1499141732108079225,  # Co-Founder
    1434653599236882574,  # Zevora
    1502941495722770472,  # Advisory
}

# ─────────────────────────────────────────────────────────────────────
# SUSPENSIONS
# ─────────────────────────────────────────────────────────────────────
SUSPENSION_CHANNEL_ID = 1364423515532427264

SUSPENSION_ALLOWED_USER_IDS = {
    1414340980110528546,
    1055321446978691112,
}
SUSPENSION_ALLOWED_ROLE_IDS = {
    1513234210054344925,
    1499141732108079225,
}

MAX_SUSPENSION_REASONS = 5

SUSPENSION_REASONS = {
    "exploiting_x1":                ("Exploiting",                  24),
    "dodging_screenshare_x1":       ("Dodging Screenshare",          6),
    "illegally_playing_x1":         ("Illegally Playing",            2),
    "illegally_playing_x2":         ("Illegally Playing",            2),
    "illegally_playing_x3":         ("Illegally Playing",            2),
    "illegally_playing_x4":         ("Illegally Playing",            2),
    "possession_of_exploits_x1":    ("Possession of Exploits",      12),
    "possession_of_exploits_x2":    ("Possession of Exploits",      12),
    "possession_of_exploits_x3":    ("Possession of Exploits",      12),
    "possession_of_exploits_x4":    ("Possession of Exploits",      12),
    "gameplay_manipulation_x1":     ("Gameplay Manipulation",        8),
    "alting_x1":                    ("Alting",                      12),
    "alting_x2":                    ("Alting",                      12),
    "alting_x3":                    ("Alting",                      12),
    "alting_x4":                    ("Alting",                      12),
    "disbanding_x1":                ("Disbanding",                   4),
    "distributing_exploits_x1":     ("Distributing Exploits",       40),
    "distributing_alts_x1":         ("Distributing Alt Accounts",   25),
    "framing_x1":                   ("Framing",                     12),
    "obstruction_of_justice_x1":    ("Obstruction of Justice",       8),
}

SPECIAL_SUSPENSION_REASONS = {
    "ineligible_until_ss": "Ineligible Until Screenshare",
}

# User IDs allowed to grant extra demand tokens
EXTRA_DEMAND_GRANT_USER_IDS = {
    1055321446978691112,
    391036854084042762,
}


def _base_label(key: str) -> str:
    return SUSPENSION_REASONS[key][0]


def _build_suspension_summary(selected_reasons):
    normal_keys  = [r for r in selected_reasons if r in SUSPENSION_REASONS]
    special_keys = [r for r in selected_reasons if r in SPECIAL_SUSPENSION_REASONS]
    total_games  = sum(SUSPENSION_REASONS[r][1] for r in normal_keys)

    label_games: dict[str, list[int]] = {}
    for r in normal_keys:
        label = _base_label(r)
        label_games.setdefault(label, []).append(SUSPENSION_REASONS[r][1])

    lines = []
    for label, game_list in label_games.items():
        count    = len(game_list)
        per_game = game_list[0]
        subtotal = per_game * count
        if count > 1:
            lines.append(f"• **{label}** — {per_game} games ×{count} = **{subtotal} games**")
        else:
            lines.append(f"• **{label}** — {per_game} games")

    reason_lines = "\n".join(lines)
    status_lines = ""
    if special_keys:
        status_labels = [SPECIAL_SUSPENSION_REASONS[r] for r in special_keys]
        status_lines  = "**Status:** " + ", ".join(f"`{s}`" for s in status_labels)

    return total_games, reason_lines, status_lines


# ─────────────────────────────────────────────────────────────────────
# TEAMS  (20 official UFF teams)
# ─────────────────────────────────────────────────────────────────────
TEAMS = [
    ("Shiroishi Samurai",          "SHI",  0x4d4d4e),
    ("Vicksburg Vortex",           "VIC",  0x230552),
    ("Salt Lake City Sentinels",   "SLC",  0xdb0e16),
    ("Nashville Nightmares",       "NSH",  0x5b00c4),
    ("Warwick Warhawks",           "WAR",  0x27833d),
    ("Sunny Isle Sea Serpents",    "SISS", 0x00b6ba),
    ("Los Angeles Golden Knights", "LGK",  0xf5be23),
    ("Michigan Mustangs",          "MMS",  0xfe001f),
    ("Portsmouth Panthers",        "PORT", 0x01a1f2),
    ("Columbus Colts",             "COL",  0x184da7),
    ("Milwaukee Rams",             "MIL",  0xc5aa76),
    ("Salisbury Falcons",          "SALI", 0x052270),
    ("Savannah Raiders",           "SAV",  0xbb0620),
    ("Highridge Huskies",          "HIG",  0x767878),
    ("Deltabay Dolphins",          "DTB",  0x0099fc),
    ("Seattle Skyclaws",           "SEA",  0x004a8b),
    ("Alabama Bloom",              "AL",   0xf7adad),
    ("Oklahoma City Owls",         "OKC",  0x67112a),
    ("Myrtle Beach Hammerheads",   "MYB",  0x215792),
    ("Windy City Warriors",        "WC",   0x4126a5),
]


# ─────────────────────────────────────────────────────────────────────
# POSTGRESQL  — persistent data layer
# ─────────────────────────────────────────────────────────────────────
_db_pool: asyncpg.Pool | None = None


async def get_db() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _db_pool


async def init_db():
    """Create the single key-value table if it doesn't exist yet."""
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS uff_store (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


async def db_get(key: str) -> dict | list | None:
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM uff_store WHERE key = $1", key)
    if row is None:
        return None
    return json.loads(row["value"])


async def db_set(key: str, value: dict | list) -> None:
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO uff_store (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, key, json.dumps(value, default=str))


# ─────────────────────────────────────────────────────────────────────
# DATA HELPERS  (async, PostgreSQL-backed)
# Each top-level key in the old JSON becomes its own DB row.
# ─────────────────────────────────────────────────────────────────────
async def load_data() -> dict:
    keys = ["players", "matches", "pending", "casual_pending",
            "suspensions", "teams", "demand_used", "extra_demands", "offers"]
    data = {}
    for k in keys:
        val = await db_get(k)
        if val is None:
            # sensible defaults
            if k in ("matches", "suspensions"):
                val = []
            else:
                val = {}
        data[k] = val
    return data


async def save_data(data: dict) -> None:
    for key, value in data.items():
        await db_set(key, value)


# ─────────────────────────────────────────────────────────────────────
# GAME / RANK HELPERS
# ─────────────────────────────────────────────────────────────────────
def get_player(data, user_id: int):
    uid = str(user_id)
    if uid not in data["players"]:
        data["players"][uid] = {
            "elo": STARTING_ELO, "wins": 0, "losses": 0,
            "last_game": None, "username": ""
        }
    return data["players"][uid]


def get_rank(elo: int):
    if elo >= 2100: return "Amethyst III", "💎", 0xA040E8
    if elo >= 1900: return "Amethyst II",  "💎", 0xA040E8
    if elo >= 1700: return "Amethyst I",   "💎", 0xA040E8
    if elo >= 1500: return "Gold III",     "🥇", 0xF0C040
    if elo >= 1300: return "Gold II",      "🥇", 0xF0C040
    if elo >= 1100: return "Gold I",       "🥇", 0xF0C040
    if elo >= 900:  return "Iron III",     "⚙️",  0x8090A0
    if elo >= 700:  return "Iron II",      "⚙️",  0x8090A0
    return                 "Iron I",       "⚙️",  0x8090A0


def on_cooldown(data, user_id: int):
    p = get_player(data, user_id)
    if not p["last_game"]:
        return False, None
    last = datetime.fromisoformat(p["last_game"])
    diff = last + timedelta(minutes=COOLDOWN_MINUTES) - datetime.utcnow()
    if diff.total_seconds() > 0:
        m = int(diff.total_seconds() // 60)
        s = int(diff.total_seconds() % 60)
        return True, f"{m}m {s}s"
    return False, None


def is_admin(interaction: discord.Interaction) -> bool:
    return (
        interaction.user.id == OWNER_ID
        or interaction.user.guild_permissions.administrator
    )


def is_staff(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return True
    user_role_ids = {r.id for r in interaction.user.roles}
    return bool(user_role_ids & STAFF_ROLE_IDS)


def can_issue_suspension(interaction: discord.Interaction) -> bool:
    if interaction.user.id in SUSPENSION_ALLOWED_USER_IDS:
        return True
    user_role_ids = {role.id for role in interaction.user.roles}
    return bool(user_role_ids & SUSPENSION_ALLOWED_ROLE_IDS) or is_admin(interaction)


def apply_branding(embed: discord.Embed) -> discord.Embed:
    if UFF_THUMBNAIL:
        embed.set_thumbnail(url=UFF_THUMBNAIL)
    return embed


async def get_channel_safe(guild: discord.Guild, channel_id: int):
    if not channel_id:
        return None
    ch = guild.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden):
        return None


async def get_pickup_channel(guild: discord.Guild):
    return await get_channel_safe(guild, QBB_CHANNEL_ID)


async def get_suspension_channel(guild: discord.Guild):
    return await get_channel_safe(guild, SUSPENSION_CHANNEL_ID)


async def get_transactions_channel(guild: discord.Guild):
    """All transactions go to the single shared transactions channel."""
    return await get_channel_safe(guild, TRANSACTIONS_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────
# BLOXLINK HELPERS
# ─────────────────────────────────────────────────────────────────────
async def bloxlink_lookup(discord_id: int, guild_id: int) -> dict:
    if not BLOXLINK_API_KEY:
        return {"error": "BLOXLINK_API_KEY not set."}

    url     = f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    headers = {"Authorization": BLOXLINK_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return {"error": f"Bloxlink returned HTTP {resp.status}"}
                body = await resp.json()

        roblox_id = body.get("robloxID")
        if not roblox_id:
            return {"error": "No Roblox account linked via Bloxlink."}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://users.roblox.com/v1/users/{roblox_id}",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                udata = await resp.json() if resp.status == 200 else {}

        username = udata.get("name", f"ID:{roblox_id}")

        avatar_url = ""
        thumb_api = (
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={roblox_id}&size=150x150&format=Png&isCircular=false"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(thumb_api, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    adata = await resp.json()
                    avatar_url = adata.get("data", [{}])[0].get("imageUrl", "")

        return {"roblox_username": username, "roblox_id": int(roblox_id), "avatar_url": avatar_url}

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────
# TRANSACTION HELPERS
# ─────────────────────────────────────────────────────────────────────
def get_team_by_role(data: dict, role_id: int) -> dict | None:
    return data["teams"].get(str(role_id))


def get_team_for_hc(data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    uid = str(user_id)
    for role_id, team in data["teams"].items():
        if team.get("head_coach_id") == uid:
            return role_id, team
    return None, None


def get_team_for_ahc(data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    uid = str(user_id)
    for role_id, team in data["teams"].items():
        if team.get("ahc_id") == uid:
            return role_id, team
    return None, None


def get_team_for_user(data: dict, user_id: int) -> tuple[str, dict] | tuple[None, None]:
    uid = str(user_id)
    for role_id, team in data["teams"].items():
        if team.get("head_coach_id") == uid:
            return role_id, team
    for role_id, team in data["teams"].items():
        if team.get("ahc_id") == uid:
            return role_id, team
    return None, None


def get_team_role(guild: discord.Guild, role_id_str: str) -> discord.Role | None:
    try:
        return guild.get_role(int(role_id_str))
    except (ValueError, TypeError):
        return None


def _info_block(team: dict) -> str:
    roster_size = len(team.get("roster", []))
    hc_id    = team.get("head_coach_id")
    hc_name  = team.get("head_coach_name", "vacant")
    hc_rbx   = team.get("head_coach_roblox", "")
    ahc_id   = team.get("ahc_id")
    ahc_name = team.get("ahc_name", "vacant")
    ahc_rbx  = team.get("ahc_roblox", "")

    lines = [f"roster: {roster_size}/{MAX_ROSTER}"]
    if hc_id:
        hc_rbx_str = f"`{hc_rbx}`" if hc_rbx else ""
        lines.append(f"head coach: <@{hc_id}> (@{hc_name}) ✓ {hc_rbx_str}".strip())
    else:
        lines.append("head coach: vacant")
    if ahc_id:
        ahc_rbx_str = f"`{ahc_rbx}`" if ahc_rbx else ""
        lines.append(f"assistant coach: <@{ahc_id}> (@{ahc_name}) ✓ {ahc_rbx_str}".strip())
    else:
        lines.append("assistant coach: vacant")
    return "\n".join(lines)


def _set_team_thumbnail(embed: discord.Embed, team: dict, guild: discord.Guild):
    """Set thumbnail to team logo (inside the embed). Falls back to the league
    logo (ZEVORA_LOGO_URL), then the guild icon, so a thumbnail is always shown."""
    logo = team.get("logo_url", "")
    if logo and logo.startswith("http"):
        embed.set_thumbnail(url=logo)
    elif ZEVORA_LOGO_URL:
        embed.set_thumbnail(url=ZEVORA_LOGO_URL)
    elif guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)


# ─────────────────────────────────────────────────────────────────────
# EMBED BUILDERS
# ─────────────────────────────────────────────────────────────────────
def build_ranked_pickup_embed(challenger, opponent, your_team, opponent_team,
                               game_link, match_id, guild, data, accepted=False):
    p1 = get_player(data, challenger.id)
    p2 = get_player(data, opponent.id)
    r1, emoji1, _ = get_rank(p1["elo"])
    r2, emoji2, _ = get_rank(p2["elo"])

    embed = discord.Embed(title="ranked pickup matchup", color=UFF_COLOR)
    embed.add_field(name=f"🟡 {challenger.display_name}",
        value=f"<@{challenger.id}>\n**{your_team}**\nRank: `{emoji1} {r1}`", inline=True)
    embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    embed.add_field(name=f"🔵 {opponent.display_name}",
        value=f"<@{opponent.id}>\n**{opponent_team}**\nRank: `{emoji2} {r2}`", inline=True)
    embed.add_field(name="🔗 game link", value=f"[**Click here to join →**]({game_link})", inline=False)
    if UFF_BANNER:
        embed.set_image(url=UFF_BANNER)
    if UFF_THUMBNAIL:
        embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    status = "✅ LIVE" if accepted else "⏳ Pending acceptance..."
    embed.set_footer(text=f"Challenge issued by {challenger.display_name} | {status} • /pickup_results when done")
    embed.timestamp = datetime.utcnow()
    return embed


def build_casual_pickup_embed(challenger, opponent, your_team, opponent_team,
                               game_link, guild, accepted=False):
    embed = discord.Embed(title="casual pickup matchup", color=CASUAL_COLOR)
    embed.add_field(name=f"🟡 {challenger.display_name}",
        value=f"<@{challenger.id}>\n**{your_team}**", inline=True)
    embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    embed.add_field(name=f"🔵 {opponent.display_name}",
        value=f"<@{opponent.id}>\n**{opponent_team}**", inline=True)
    embed.add_field(name="🔗 game link", value=f"[**Click here to join →**]({game_link})", inline=False)
    if UFF_BANNER:
        embed.set_image(url=UFF_BANNER)
    if UFF_THUMBNAIL:
        embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    status = "✅ LIVE" if accepted else "⏳ Pending acceptance..."
    embed.set_footer(text=f"Casual pickup issued by {challenger.display_name} | {status}")
    embed.timestamp = datetime.utcnow()
    return embed


async def build_transaction_embed(action: str, player: discord.Member, team: dict,
                                   team_role, guild: discord.Guild,
                                   color: int = UFF_COLOR) -> discord.Embed:
    """
    Transaction embed: team logo as thumbnail (inside embed), player headshot as image.
    """
    embed = discord.Embed(title=action.lower(), color=color)

    blox          = await bloxlink_lookup(player.id, guild.id)
    roblox_name   = blox.get("roblox_username", "Unknown")
    roblox_avatar = blox.get("avatar_url", "")

    role_str = team_role.mention if team_role else f"**{team['name']}**"
    embed.description = (
        f"{role_str} have **{action.lower()}** <@{player.id}> (@{player.name})\n"
        f"`{roblox_name}`"
    )
    embed.add_field(name="\u200b", value=_info_block(team), inline=False)

    # Team logo as thumbnail (top-right corner, INSIDE the embed)
    _set_team_thumbnail(embed, team, guild)
    # Player Roblox headshot as the large image at the bottom of the embed
    if roblox_avatar:
        embed.set_image(url=roblox_avatar)

    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    return embed


async def build_coach_transaction_embed(action: str, player: discord.Member, team: dict,
                                         team_role, guild: discord.Guild,
                                         color: int = UFF_COLOR) -> discord.Embed:
    """Embed for coach promotions / demotions."""
    embed = discord.Embed(title=action.lower(), color=color)

    blox          = await bloxlink_lookup(player.id, guild.id)
    roblox_name   = blox.get("roblox_username", "Unknown")
    roblox_avatar = blox.get("avatar_url", "")

    is_promotion = "promot" in action.lower()
    role_label   = "assistant coach" if is_promotion else "regular player"
    role_str     = team_role.mention if team_role else f"**{team['name']}**"

    embed.description = (
        f"{role_str} have **{action.lower()}** <@{player.id}> (@{player.name})\n"
        f"`{roblox_name}` to {role_label}!"
    )
    embed.add_field(name="\u200b", value=_info_block(team), inline=False)

    _set_team_thumbnail(embed, team, guild)
    if roblox_avatar:
        embed.set_image(url=roblox_avatar)

    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    return embed


async def post_transaction(guild: discord.Guild, team: dict, embed: discord.Embed,
                            team_role, followup=None, interaction=None,
                            ephemeral_msg="", ephemeral: bool = True):
    """
    Post transaction embed to the shared transactions channel.
    Logo is already set as the embed thumbnail — no plain message needed for that.

    The team role ping is sent as its own plain-text message AFTER the embed, so the
    player headshot / embed shows first and the role ping (which actually triggers a
    notification) follows it, outside the embed.
    """
    ch       = await get_transactions_channel(guild)
    ping_str = team_role.mention if team_role else ""

    if ch:
        await ch.send(embed=embed)
        if ping_str:
            await ch.send(content=ping_str)
        if followup and ephemeral_msg:
            await followup.send(ephemeral_msg, ephemeral=ephemeral)
        elif interaction and ephemeral_msg:
            try:
                await interaction.response.send_message(ephemeral_msg, ephemeral=ephemeral)
            except discord.InteractionResponded:
                await interaction.followup.send(ephemeral_msg, ephemeral=ephemeral)
    else:
        if followup:
            await followup.send(embed=embed)
        elif interaction:
            try:
                await interaction.response.send_message(embed=embed)
            except discord.InteractionResponded:
                await interaction.followup.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# OFFER VIEW  (player accepts/ignores within 12h)
# ─────────────────────────────────────────────────────────────────────
class OfferView(discord.ui.View):
    def __init__(self, offer_id, team_role_id, team_name, team_logo,
                 hc_id, player_id, guild_id):
        super().__init__(timeout=43200)  # 12 hours
        self.offer_id     = offer_id
        self.team_role_id = team_role_id
        self.team_name    = team_name
        self.team_logo    = team_logo
        self.hc_id        = hc_id
        self.player_id    = player_id
        self.guild_id     = guild_id
        self.responded    = False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        data = await load_data()
        data.get("offers", {}).pop(self.offer_id, None)
        await save_data(data)

    @discord.ui.button(label="✅  Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("❌ This offer isn't for you.", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("This offer has already been answered.", ephemeral=True)
            return
        self.responded = True
        self.stop()
        for item in self.children:
            item.disabled = True

        data  = await load_data()
        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Could not find the server.", ephemeral=True)
            return

        rid  = str(self.team_role_id)
        team = data["teams"].get(rid)
        if not team:
            await interaction.response.edit_message(content="❌ That team no longer exists.", view=self)
            return

        roster = team.setdefault("roster", [])
        if len(roster) >= MAX_ROSTER:
            await interaction.response.edit_message(
                content=f"❌ **{team['name']}** is at the roster cap ({MAX_ROSTER}/{MAX_ROSTER}).", view=self)
            return
        if str(interaction.user.id) in [r["id"] for r in roster]:
            await interaction.response.edit_message(content=f"❌ You're already on **{team['name']}**.", view=self)
            return

        try:
            player = guild.get_member(self.player_id) or await guild.fetch_member(self.player_id)
        except discord.NotFound:
            await interaction.response.edit_message(content="❌ Could not find you in the server.", view=self)
            return

        blox          = await bloxlink_lookup(player.id, guild.id)
        roblox_name   = blox.get("roblox_username", "Unknown")
        roblox_avatar = blox.get("avatar_url", "")

        roster.append({"id": str(player.id), "name": player.display_name,
                       "roblox": roblox_name, "role": "Player"})
        data.get("offers", {}).pop(self.offer_id, None)
        await save_data(data)

        team_role = guild.get_role(int(rid))
        color     = team.get("color", UFF_COLOR)
        role_str  = team_role.mention if team_role else f"**{team['name']}**"

        # Give the player their team Discord role
        if team_role:
            try:
                await player.add_roles(team_role, reason=f"Signed to {team['name']}")
            except discord.Forbidden:
                pass

        embed = discord.Embed(title="signed", color=color)
        embed.description = (
            f"{role_str} have **signed** <@{player.id}> (@{player.name})\n"
            f"`{roblox_name}`"
        )
        embed.add_field(name="\u200b", value=_info_block(team), inline=False)
        _set_team_thumbnail(embed, team, guild)
        if roblox_avatar:
            embed.set_image(url=roblox_avatar)
        embed.set_footer(text=UFF_FOOTER)
        embed.timestamp = datetime.utcnow()

        ch = await get_transactions_channel(guild)
        if ch:
            # Headshot/embed posts first, then the team role ping as its own
            # plain-text message AFTER it (outside the embed) so it actually notifies.
            await ch.send(embed=embed)
            if team_role:
                await ch.send(content=team_role.mention)

        accepted_embed = discord.Embed(
            title="✅ Offer Accepted!",
            description=f"You accepted the offer from **{self.team_name}**!\n\nWelcome to the team.",
            color=0x57F287
        )
        if self.team_logo:
            accepted_embed.set_thumbnail(url=self.team_logo)
        accepted_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=accepted_embed, view=self)

        if self.hc_id:
            try:
                hc_member = guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id))
                notify = discord.Embed(title="✅ Offer Accepted",
                    description=f"**{player.display_name}** accepted your offer to join **{self.team_name}**.",
                    color=0x57F287)
                notify.set_footer(text=UFF_FOOTER)
                await hc_member.send(embed=notify)
            except Exception:
                pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("❌ This offer isn't for you.", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("This offer has already been answered.", ephemeral=True)
            return
        self.responded = True
        self.stop()
        for item in self.children:
            item.disabled = True

        data = await load_data()
        data.get("offers", {}).pop(self.offer_id, None)
        await save_data(data)

        declined_embed = discord.Embed(title="❌ Offer Declined",
            description=f"You declined the offer from **{self.team_name}**.", color=0xED4245)
        if self.team_logo:
            declined_embed.set_thumbnail(url=self.team_logo)
        declined_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=declined_embed, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild and self.hc_id:
            try:
                hc_member = guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id))
                target    = guild.get_member(self.player_id)
                target_name = target.display_name if target else "The player"
                notify = discord.Embed(title="❌ Offer Declined",
                    description=f"**{target_name}** declined your offer to join **{self.team_name}**.",
                    color=0xED4245)
                notify.set_footer(text=UFF_FOOTER)
                await hc_member.send(embed=notify)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# RANKED PICKUP VIEW
# ─────────────────────────────────────────────────────────────────────
class RankedPickupView(discord.ui.View):
    def __init__(self, match_id, challenger_id, opponent_id, challenger_name,
                 opponent_name, challenger_team, opponent_team, game_link, guild_id):
        super().__init__(timeout=1800)
        self.match_id = match_id; self.challenger_id = challenger_id
        self.opponent_id = opponent_id; self.challenger_name = challenger_name
        self.opponent_name = opponent_name; self.challenger_team = challenger_team
        self.opponent_team = opponent_team; self.game_link = game_link
        self.guild_id = guild_id; self.responded = False

    async def on_timeout(self):
        for item in self.children: item.disabled = True

    @discord.ui.button(label="✅  Accept Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True
        await interaction.message.edit(view=self)

        data  = await load_data()
        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Could not find the server.", ephemeral=True); return
        try:
            challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent   = guild.get_member(self.opponent_id)   or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not find one of the players.", ephemeral=True); return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True); return

        embed  = build_ranked_pickup_embed(challenger, opponent, self.challenger_team,
                    self.opponent_team, self.game_link, self.match_id, guild, data, accepted=True)
        header = (f"@everyone  **Ranked Pickup**\n"
                  f"**{challenger.display_name}** vs **{opponent.display_name}** is hosting a ranked pickup!")
        ch = await get_pickup_channel(guild)
        if ch:
            await ch.send(content=header, embed=embed)
        else:
            try: await challenger.send(content="⚠️ Pickup channel not set. Here's the matchup:", embed=embed)
            except discord.Forbidden: pass

        accepted_embed = discord.Embed(title="✅ Challenge Accepted!",
            description=f"You accepted **{self.challenger_name}**'s challenge!\n\nThe matchup has been posted.\n🔗 [Join the game]({self.game_link})",
            color=0x57F287)
        accepted_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=accepted_embed, view=self)
        try:
            notify = discord.Embed(title="✅ Challenge Accepted!",
                description=f"**{self.opponent_name}** accepted your pickup challenge!\n\nThe matchup has been posted.\n🔗 [Join the game]({self.game_link})\n\nUse `/pickup_results` when the game is over.",
                color=0x57F287)
            notify.set_footer(text=UFF_FOOTER)
            await challenger.send(embed=notify)
        except discord.Forbidden: pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True

        data = await load_data()
        if self.match_id in data.get("pending", {}):
            del data["pending"][self.match_id]; await save_data(data)

        declined_embed = discord.Embed(title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s pickup challenge.", color=0xED4245)
        declined_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=declined_embed, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild:
            try:
                challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                notify = discord.Embed(title="❌ Challenge Declined",
                    description=f"**{self.opponent_name}** declined your pickup challenge.\nNo match was recorded.",
                    color=0xED4245)
                notify.set_footer(text=UFF_FOOTER)
                await challenger.send(embed=notify)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden): pass


# ─────────────────────────────────────────────────────────────────────
# CASUAL PICKUP VIEW
# ─────────────────────────────────────────────────────────────────────
class CasualPickupView(discord.ui.View):
    def __init__(self, match_id, challenger_id, opponent_id, challenger_name,
                 opponent_name, challenger_team, opponent_team, game_link, guild_id):
        super().__init__(timeout=1800)
        self.match_id = match_id; self.challenger_id = challenger_id
        self.opponent_id = opponent_id; self.challenger_name = challenger_name
        self.opponent_name = opponent_name; self.challenger_team = challenger_team
        self.opponent_team = opponent_team; self.game_link = game_link
        self.guild_id = guild_id; self.responded = False

    async def on_timeout(self):
        for item in self.children: item.disabled = True

    @discord.ui.button(label="✅  Accept Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True
        await interaction.message.edit(view=self)

        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Could not find the server.", ephemeral=True); return
        try:
            challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent   = guild.get_member(self.opponent_id)   or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not find one of the players.", ephemeral=True); return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True); return

        data = await load_data()
        if self.match_id in data.get("casual_pending", {}):
            del data["casual_pending"][self.match_id]; await save_data(data)

        embed  = build_casual_pickup_embed(challenger, opponent, self.challenger_team,
                    self.opponent_team, self.game_link, guild, accepted=True)
        header = (f"@here  **Casual Pickup**\n"
                  f"**{challenger.display_name}** vs **{opponent.display_name}** is hosting a casual pickup!")
        ch = await get_pickup_channel(guild)
        if ch: await ch.send(content=header, embed=embed)
        else:
            try: await challenger.send(content="⚠️ Pickup channel not set:", embed=embed)
            except discord.Forbidden: pass

        accepted_embed = discord.Embed(title="✅ Challenge Accepted!",
            description=f"You accepted **{self.challenger_name}**'s casual pickup!\n\nThe matchup has been posted.\n🔗 [Join the game]({self.game_link})",
            color=0x57F287)
        accepted_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=accepted_embed, view=self)
        try:
            notify = discord.Embed(title="✅ Challenge Accepted!",
                description=f"**{self.opponent_name}** accepted your casual pickup!\n\nThe matchup has been posted.\n🔗 [Join the game]({self.game_link})",
                color=0x57F287)
            notify.set_footer(text=UFF_FOOTER)
            await challenger.send(embed=notify)
        except discord.Forbidden: pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True

        data = await load_data()
        if self.match_id in data.get("casual_pending", {}):
            del data["casual_pending"][self.match_id]; await save_data(data)

        declined_embed = discord.Embed(title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s casual pickup.", color=0xED4245)
        declined_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=declined_embed, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild:
            try:
                challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                notify = discord.Embed(title="❌ Challenge Declined",
                    description=f"**{self.opponent_name}** declined your casual pickup.", color=0xED4245)
                notify.set_footer(text=UFF_FOOTER)
                await challenger.send(embed=notify)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden): pass


# ─────────────────────────────────────────────────────────────────────
# SHARED CASUAL LOGIC
# ─────────────────────────────────────────────────────────────────────
async def _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team):
    user_role_ids = {role.id for role in interaction.user.roles}
    if not (user_role_ids & PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.response.send_message("❌ You don't have the required role to start a casual pickup.", ephemeral=True)
        return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't challenge yourself!", ephemeral=True); return
    if opponent.bot:
        await interaction.response.send_message("❌ Can't challenge a bot!", ephemeral=True); return

    data     = await load_data()
    match_id = f"casual_{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("casual_pending", {})[match_id] = {
        "challenger_id": str(interaction.user.id), "opponent_id": str(opponent.id),
        "challenger_name": interaction.user.display_name, "opponent_name": opponent.display_name,
        "challenger_team": your_team, "opponent_team": opponent_team,
        "game_link": game_link, "timestamp": datetime.utcnow().isoformat(),
        "match_id": match_id, "guild_id": interaction.guild.id
    }
    await save_data(data)

    dm_embed = discord.Embed(title="🏈 You've Been Challenged to a Casual Pickup!",
        description=(f"**{interaction.user.display_name}** wants to play a casual (unranked) pickup against you.\n"
                     f"Accept or decline below. This request expires in **30 minutes**."), color=CASUAL_COLOR)
    dm_embed.add_field(name=f"🟡 {interaction.user.display_name}", value=f"`{interaction.user.name}`\n**{your_team}**", inline=True)
    dm_embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    dm_embed.add_field(name=f"🔵 {opponent.display_name}", value=f"`{opponent.name}`\n**{opponent_team}**", inline=True)
    dm_embed.add_field(name="🔗 Game Link", value=f"[**Click here to join →**]({game_link})", inline=False)
    dm_embed.add_field(name="\u200b", value="⚠️ This is **NOT** a ranked matchup — no ELO will be affected.", inline=False)
    if UFF_BANNER: dm_embed.set_image(url=UFF_BANNER)
    if UFF_THUMBNAIL: dm_embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild and interaction.guild.icon: dm_embed.set_thumbnail(url=interaction.guild.icon.url)
    dm_embed.set_footer(text=f"Casual pickup challenge issued by {interaction.user.display_name} | {UFF_FOOTER}")
    dm_embed.timestamp = datetime.utcnow()

    view = CasualPickupView(match_id=match_id, challenger_id=interaction.user.id, opponent_id=opponent.id,
        challenger_name=interaction.user.display_name, opponent_name=opponent.display_name,
        challenger_team=your_team, opponent_team=opponent_team,
        game_link=game_link, guild_id=interaction.guild.id)
    try:
        await opponent.send(embed=dm_embed, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Could not DM **{opponent.display_name}** — they have DMs disabled.", ephemeral=True)
        data.get("casual_pending", {}).pop(match_id, None); await save_data(data); return

    confirm_embed = discord.Embed(title="📨 Casual Pickup Sent!",
        description=(f"Your casual pickup challenge has been sent to **{opponent.display_name}** via DM.\n\n"
                     f"The matchup will be posted publicly only **if they accept**.\nNo ELO changes will occur."),
        color=0x57F287)
    confirm_embed.set_footer(text=f"{UFF_FOOTER} • 30-minute response window")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# SUSPENSION UI
# ─────────────────────────────────────────────────────────────────────
def _make_suspension_options():
    options = []
    seen_bases: dict[str, int] = {}
    for key, (label, games) in SUSPENSION_REASONS.items():
        seen_bases[label] = seen_bases.get(label, 0) + 1
        instance = seen_bases[label]
        if instance == 1:
            display = f"{label} — {games} games"
            desc    = f"Adds {games} games"
        else:
            display = f"{label} (×{instance}) — +{games} games"
            desc    = f"Stack: adds another {games} games"
        options.append(discord.SelectOption(label=display, value=key, description=desc))
    for key, label in SPECIAL_SUSPENSION_REASONS.items():
        options.append(discord.SelectOption(label=label, value=key, description="Status only — no games added"))
    return options[:25]


class SuspensionReasonSelect(discord.ui.Select):
    def __init__(self):
        options = _make_suspension_options()
        super().__init__(placeholder=f"Select up to {MAX_SUSPENSION_REASONS} reasons...",
                         min_values=1, max_values=min(MAX_SUSPENSION_REASONS, len(options)), options=options)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        view.selected_reasons = self.values
        total_games, reason_lines, status_lines = _build_suspension_summary(view.selected_reasons)
        preview = f"**Suspension preview — {view.target.display_name}**\n\n"
        if reason_lines: preview += reason_lines + "\n\n"
        if status_lines: preview += status_lines + "\n\n"
        preview += f"**Total: {total_games} games**\n\nClick **Confirm & Post** to publish."
        await interaction.response.edit_message(content=preview, view=view)


class SuspensionConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Confirm & Post", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        if not view.selected_reasons:
            await interaction.response.send_message("❌ Select at least one reason before confirming.", ephemeral=True); return

        total_games, reason_lines, status_lines = _build_suspension_summary(view.selected_reasons)
        embed = discord.Embed(title="🚫 player suspension", color=0xED4245)
        embed.add_field(name="Player", value=f"<@{view.target.id}> ({view.target.display_name})", inline=False)
        if reason_lines: embed.add_field(name="Reason(s)", value=reason_lines, inline=False)
        if status_lines: embed.add_field(name="Additional Status", value=status_lines, inline=False)
        embed.add_field(name="Total Games Suspended", value=f"**{total_games} games**", inline=False)
        if view.target.avatar: embed.set_thumbnail(url=view.target.avatar.url)
        embed.set_footer(text=f"Issued by {interaction.user.display_name} | {UFF_FOOTER}")
        embed.timestamp = datetime.utcnow()

        ch = await get_suspension_channel(interaction.guild)
        for item in view.children: item.disabled = True

        if ch:
            await ch.send(embed=embed)
            await interaction.response.edit_message(content=f"✅ Suspension posted to {ch.mention}.", view=view)
            normal_keys  = [r for r in view.selected_reasons if r in SUSPENSION_REASONS]
            special_keys = [r for r in view.selected_reasons if r in SPECIAL_SUSPENSION_REASONS]
            data = await load_data()
            data.setdefault("suspensions", []).append({
                "player_id": str(view.target.id), "player_name": view.target.display_name,
                "reason_keys": normal_keys, "reasons": [_base_label(r) for r in normal_keys],
                "status_flags": [SPECIAL_SUSPENSION_REASONS[r] for r in special_keys],
                "total_games": total_games, "issued_by": str(interaction.user.id),
                "issued_by_name": interaction.user.display_name,
                "date": datetime.utcnow().isoformat(), "cleared": False,
            })
            await save_data(data)
        else:
            await interaction.response.edit_message(content="❌ Could not find the suspension channel.", view=view)


class SuspensionCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        for item in view.children: item.disabled = True
        await interaction.response.edit_message(content="❌ Suspension cancelled — nothing was posted.", view=view)
        view.stop()


class SuspensionView(discord.ui.View):
    def __init__(self, target: discord.Member, issuer_id: int):
        super().__init__(timeout=300)
        self.target = target; self.issuer_id = issuer_id; self.selected_reasons = []
        self.add_item(SuspensionReasonSelect())
        self.add_item(SuspensionConfirmButton())
        self.add_item(SuspensionCancelButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.issuer_id:
            await interaction.response.send_message("❌ Only the staff member who started this can use these controls.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children: item.disabled = True


# ─────────────────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await init_db()
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    await bot.tree.sync()
    print(f"✅ UFF Bot online — {bot.user}")
    print(f"   Guild                : {GUILD_ID}")
    print(f"   Pickup Channel       : {QBB_CHANNEL_ID or 'not set'}")
    print(f"   Transactions Channel : {TRANSACTIONS_CHANNEL_ID}")
    print(f"   Suspension Channel   : {SUSPENSION_CHANNEL_ID}")
    print(f"   Bloxlink API         : {'set' if BLOXLINK_API_KEY else 'NOT SET — Roblox lookups disabled'}")
    print(f"   Database             : {'connected' if DATABASE_URL else 'NOT SET — add DATABASE_URL'}")
    print(f"   Zevora League Logo   : {'set' if ZEVORA_LOGO_URL else 'NOT SET — falling back to guild icon'}")


# ═════════════════════════════════════════════════════════════════════
# TRANSACTION COMMANDS
# ═════════════════════════════════════════════════════════════════════

@bot.tree.command(name="set_team", description="[Staff] Register a Discord role as a UFF team")
@app_commands.describe(
    team_role="The Discord role that represents this team (name and icon are pulled from the role automatically)"
)
@app_commands.default_permissions(administrator=True)
async def set_team(interaction: discord.Interaction, team_role: discord.Role):
    if not is_staff(interaction):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True); return

    # Pull name and logo directly from the Discord role
    team_name = team_role.name
    icon      = team_role.display_icon
    if icon is None:
        logo_url = ""
    elif isinstance(icon, discord.PartialEmoji):
        logo_url = str(icon.url) if icon.url else ""
    else:
        # discord.Asset
        logo_url = str(icon.url)

    data = await load_data()
    rid  = str(team_role.id)
    existing = data["teams"].get(rid, {})
    data["teams"][rid] = {
        "name":                    team_name,
        "role_id":                 rid,
        "transactions_channel_id": str(TRANSACTIONS_CHANNEL_ID),
        "head_coach_id":           existing.get("head_coach_id"),
        "head_coach_name":         existing.get("head_coach_name"),
        "head_coach_roblox":       existing.get("head_coach_roblox", ""),
        "ahc_id":                  existing.get("ahc_id"),
        "ahc_name":                existing.get("ahc_name"),
        "ahc_roblox":              existing.get("ahc_roblox", ""),
        "logo_url":                logo_url or existing.get("logo_url", ""),
        "roster":                  existing.get("roster", []),
        "color":                   team_role.color.value or UFF_COLOR,
    }
    await save_data(data)

    embed = discord.Embed(title="team registered", color=UFF_COLOR)
    embed.description = (
        f"**{team_name}** is now registered!\n"
        f"Role: {team_role.mention} | Transactions → <#{TRANSACTIONS_CHANNEL_ID}>"
    )
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    else:
        embed.description += (
            "\n\n⚠️ No logo could be pulled from this role automatically (your server may not "
            "have the role-icon feature unlocked). Use `/set_team_image` to set one manually."
        )
    embed.set_footer(text=UFF_FOOTER)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set_team_image", description="[Staff] Override the logo URL for a team")
@app_commands.describe(team_role="The team's Discord role",
                        logo_url="Direct image URL for the team logo")
@app_commands.default_permissions(administrator=True)
async def set_team_image(interaction: discord.Interaction, team_role: discord.Role, logo_url: str):
    if not is_staff(interaction):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
    data = await load_data()
    rid  = str(team_role.id)
    if rid not in data["teams"]:
        await interaction.response.send_message(
            f"❌ {team_role.mention} isn't registered yet. Use `/set_team` first.", ephemeral=True); return
    data["teams"][rid]["logo_url"] = logo_url
    await save_data(data)
    embed = discord.Embed(title="team logo updated", color=UFF_COLOR)
    embed.set_thumbnail(url=logo_url)
    embed.description = f"Logo for **{data['teams'][rid]['name']}** updated."
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="assign_hc", description="[Staff] Assign a head coach to a team")
@app_commands.describe(team_role="The team's Discord role", player="The member to make head coach")
@app_commands.default_permissions(administrator=True)
async def assign_hc(interaction: discord.Interaction, team_role: discord.Role, player: discord.Member):
    if not is_staff(interaction):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
    data = await load_data()
    rid  = str(team_role.id)
    if rid not in data["teams"]:
        await interaction.response.send_message(
            f"❌ {team_role.mention} isn't registered. Use `/set_team` first.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    blox        = await bloxlink_lookup(player.id, interaction.guild.id)
    roblox_name = blox.get("roblox_username", "")

    team = data["teams"][rid]
    team["head_coach_id"]     = str(player.id)
    team["head_coach_name"]   = player.name
    team["head_coach_roblox"] = roblox_name

    roster = team.setdefault("roster", [])
    if str(player.id) not in [r["id"] for r in roster]:
        roster.append({"id": str(player.id), "name": player.display_name,
                       "roblox": roblox_name, "role": "Head Coach"})
    else:
        for r in roster:
            if r["id"] == str(player.id): r["role"] = "Head Coach"
    await save_data(data)

    # Give the Head Coach Discord role
    hc_role = interaction.guild.get_role(HEAD_COACH_ROLE_ID)
    if hc_role:
        try:
            await player.add_roles(hc_role, reason="Assigned as Head Coach via /assign_hc")
        except discord.Forbidden:
            pass

    await interaction.followup.send(
        f"✅ **{player.display_name}** is now head coach of **{team['name']}**.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /offer
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="offer", description="Send a player a roster offer via DM (they have 12h to accept)")
@app_commands.describe(player="The player to offer a roster spot to")
async def offer(interaction: discord.Interaction, player: discord.Member):
    data = await load_data()
    rid, team = get_team_for_user(data, interaction.user.id)

    if not team:
        if is_staff(interaction):
            await interaction.response.send_message(
                "❌ Staff: use `/assign_hc` to assign yourself as HC first.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ You're not registered as head coach or assistant coach of any team.", ephemeral=True)
        return

    if player.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't offer yourself.", ephemeral=True); return
    if player.bot:
        await interaction.response.send_message("❌ Can't offer a bot.", ephemeral=True); return

    roster = team.setdefault("roster", [])
    if len(roster) >= MAX_ROSTER:
        await interaction.response.send_message(
            f"❌ **{team['name']}** is at the roster cap ({MAX_ROSTER}/{MAX_ROSTER}).", ephemeral=True); return
    if str(player.id) in [r["id"] for r in roster]:
        await interaction.response.send_message(
            f"❌ {player.display_name} is already on **{team['name']}**.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)

    team_role  = get_team_role(interaction.guild, rid)
    hc_id      = team.get("head_coach_id")
    team_logo  = team.get("logo_url", "")
    team_name  = team["name"]
    team_color = team.get("color", UFF_COLOR)
    offer_id   = f"offer_{rid}_{player.id}_{int(datetime.utcnow().timestamp())}"

    data.setdefault("offers", {})[offer_id] = {
        "team_role_id": rid, "team_name": team_name,
        "player_id": str(player.id), "hc_id": hc_id,
        "timestamp": datetime.utcnow().isoformat(), "guild_id": str(interaction.guild.id),
    }
    await save_data(data)

    dm_embed = discord.Embed(
        title=f"offer from the {team_name}",
        description=f"you have been offered a roster spot on the **{team_name}**!",
        color=team_color
    )
    hc_line = "*vacant*"
    if hc_id:
        hc_rbx = team.get("head_coach_roblox", "")
        hc_rbx_str = f"`{hc_rbx}`" if hc_rbx else ""
        hc_line = f"<@{hc_id}> {hc_rbx_str}".strip()
    dm_embed.add_field(name="head coach:", value=hc_line, inline=False)
    dm_embed.add_field(name="\u200b", value="you have **12 hours** to accept or ignore this offer", inline=False)
    if team_logo:
        dm_embed.set_thumbnail(url=team_logo)
    elif ZEVORA_LOGO_URL:
        dm_embed.set_thumbnail(url=ZEVORA_LOGO_URL)
    elif interaction.guild.icon:
        dm_embed.set_thumbnail(url=interaction.guild.icon.url)
    dm_embed.set_footer(text=UFF_FOOTER)
    dm_embed.timestamp = datetime.utcnow()

    view = OfferView(offer_id=offer_id, team_role_id=int(rid), team_name=team_name,
                     team_logo=team_logo, hc_id=hc_id, player_id=player.id,
                     guild_id=interaction.guild.id)
    try:
        await player.send(embed=dm_embed, view=view)
    except discord.Forbidden:
        data.get("offers", {}).pop(offer_id, None); await save_data(data)
        await interaction.followup.send(
            f"❌ Could not DM **{player.display_name}** — they have DMs disabled.", ephemeral=True)
        return

    await interaction.followup.send(
        f"✅ Offer sent to **{player.display_name}** via DM. They have 12 hours to accept or ignore.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /release
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="release", description="Release a player from your team")
@app_commands.describe(player="The player to release")
async def release(interaction: discord.Interaction, player: discord.Member):
    data    = await load_data()
    rid, team = get_team_for_user(data, interaction.user.id)

    if not team and not is_staff(interaction):
        await interaction.response.send_message(
            "❌ You're not registered as head coach or assistant coach of any team.", ephemeral=True); return

    if not team and is_staff(interaction):
        found_rid, found_team = None, None
        for r, t in data["teams"].items():
            if str(player.id) in [x["id"] for x in t.get("roster", [])]:
                found_rid, found_team = r, t; break
        if not found_team:
            await interaction.response.send_message(
                f"❌ {player.display_name} isn't on any registered team.", ephemeral=True); return
        rid, team = found_rid, found_team

    roster = team.setdefault("roster", [])
    before = len(roster)
    team["roster"] = [r for r in roster if r["id"] != str(player.id)]
    if len(team["roster"]) == before:
        await interaction.response.send_message(
            f"❌ {player.display_name} isn't on **{team['name']}**.", ephemeral=True); return

    await save_data(data)
    await interaction.response.defer(ephemeral=True, thinking=True)

    team_role = get_team_role(interaction.guild, rid)
    # Remove the team Discord role from the released player
    if team_role and team_role in player.roles:
        try:
            await player.remove_roles(team_role, reason=f"Released from {team['name']}")
        except discord.Forbidden:
            pass

    embed = await build_transaction_embed("released", player, team, team_role, interaction.guild, color=0xED4245)
    await post_transaction(interaction.guild, team, embed, team_role, followup=interaction.followup,
                           ephemeral_msg=f"✅ Released **{player.display_name}** from **{team['name']}**.")


# ─────────────────────────────────────────────────────────────────────
# /demand
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="demand", description="Demand from your current team 1 demand per player")
async def demand(interaction: discord.Interaction):
    data = await load_data()
    uid  = str(interaction.user.id)

    found_rid, found_team = None, None
    for r, t in data["teams"].items():
        if uid in [x["id"] for x in t.get("roster", [])]:
            found_rid, found_team = r, t; break

    if not found_team:
        await interaction.response.send_message("❌ You aren't on any registered team.", ephemeral=True); return

    demand_used   = data.get("demand_used", {})
    extra_demands = data.get("extra_demands", {})
    extra_tokens  = extra_demands.get(uid, 0)

    if demand_used.get(uid, False) and extra_tokens <= 0:
        await interaction.response.send_message(
            "❌ You've already used your demand release. Players are granted **1 demand ** only.\n"
            "If you paid for an extra demand, ask a league admin to grant it with `/grant_extra_demand`.",
            ephemeral=True); return

    if demand_used.get(uid, False):
        data["extra_demands"][uid] = extra_tokens - 1
    else:
        data.setdefault("demand_used", {})[uid] = True

    found_team["roster"] = [r for r in found_team.get("roster", []) if r["id"] != uid]
    await save_data(data)

    await interaction.response.defer(ephemeral=True, thinking=True)

    blox          = await bloxlink_lookup(interaction.user.id, interaction.guild.id)
    roblox_name   = blox.get("roblox_username", "Unknown")
    roblox_avatar = blox.get("avatar_url", "")
    team_role     = get_team_role(interaction.guild, found_rid)

    # Remove the team Discord role from the player
    if team_role and team_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(team_role, reason=f"Demand release from {found_team['name']}")
        except discord.Forbidden:
            pass

    role_str = team_role.mention if team_role else f"**{found_team['name']}**"

    embed = discord.Embed(title="demand release", color=0xED4245)
    embed.description = (
        f"<@{interaction.user.id}> (@{interaction.user.name}) `{roblox_name}` "
        f"has demanded a release from the {role_str}!"
    )
    embed.add_field(name="\u200b", value=_info_block(found_team), inline=False)
    _set_team_thumbnail(embed, found_team, interaction.guild)
    if roblox_avatar:
        embed.set_image(url=roblox_avatar)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()

    await post_transaction(interaction.guild, found_team, embed, team_role, followup=interaction.followup,
                           ephemeral_msg=f"✅ You have demanded from **{found_team['name']}** This will be posted in transactions channel.")


# ─────────────────────────────────────────────────────────────────────
# /grant_extra_demand
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="grant_extra_demand", description="[Owner] Grant a player an extra demand release token")
@app_commands.describe(player="The player to grant an extra demand to",
                        amount="Number of extra demands to grant (default 1)")
async def grant_extra_demand(interaction: discord.Interaction, player: discord.Member, amount: int = 1):
    if interaction.user.id not in EXTRA_DEMAND_GRANT_USER_IDS and not is_admin(interaction):
        await interaction.response.send_message("❌ You don't have permission to grant extra demands.", ephemeral=True); return
    data = await load_data()
    uid  = str(player.id)
    data.setdefault("extra_demands", {})
    data["extra_demands"][uid] = data["extra_demands"].get(uid, 0) + amount
    await save_data(data)
    total = data["extra_demands"][uid]
    await interaction.response.send_message(
        f"✅ Granted **{amount}** extra demand token(s) to **{player.display_name}**.\n"
        f"They now have **{total}** extra demand token(s) banked.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /promote_coach  — ephemeral confirmation; public transaction embed
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="promote_coach", description="Promote a player to Assistant Head Coach")
@app_commands.describe(player="The player to promote to AHC")
async def promote_coach(interaction: discord.Interaction, player: discord.Member):
    data = await load_data()
    rid, team = get_team_for_hc(data, interaction.user.id)

    if not team and is_staff(interaction):
        for r, t in data["teams"].items():
            if str(player.id) in [x["id"] for x in t.get("roster", [])]:
                rid, team = r, t; break

    if not team:
        await interaction.response.send_message(
            "❌ You're not registered as head coach of any team. Only HCs and staff can promote coaches.",
            ephemeral=True); return

    roster = team.setdefault("roster", [])
    if str(player.id) not in [r["id"] for r in roster]:
        await interaction.response.send_message(
            f"❌ {player.display_name} must be on the roster first. Use `/offer` to add them.",
            ephemeral=True); return

    # Defer ephemeral so only the command runner sees the "thinking" indicator
    await interaction.response.defer(ephemeral=True)
    blox        = await bloxlink_lookup(player.id, interaction.guild.id)
    roblox_name = blox.get("roblox_username", "")

    team["ahc_id"]     = str(player.id)
    team["ahc_name"]   = player.name
    team["ahc_roblox"] = roblox_name
    for r in roster:
        if r["id"] == str(player.id): r["role"] = "Assistant Head Coach"
    await save_data(data)

    # Give AHC Discord role; remove HC role if they had it
    ahc_role = interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID)
    hc_role  = interaction.guild.get_role(HEAD_COACH_ROLE_ID)
    try:
        if ahc_role: await player.add_roles(ahc_role, reason="Promoted to AHC via /promote_coach")
        if hc_role and hc_role in player.roles:
            await player.remove_roles(hc_role, reason="Promoted to AHC — removing HC role")
    except discord.Forbidden:
        pass

    team_role = get_team_role(interaction.guild, rid)
    embed = await build_coach_transaction_embed("assistant coach promotion", player, team,
                                                team_role, interaction.guild,
                                                color=team.get("color", UFF_COLOR))
    # Post the public transaction embed, then send ephemeral confirmation to the command runner
    await post_transaction(
        interaction.guild, team, embed, team_role,
        followup=interaction.followup,
        ephemeral_msg=f"✅ **{player.display_name}** promoted to AHC of **{team['name']}**.",
        ephemeral=True
    )


# ─────────────────────────────────────────────────────────────────────
# /demote_coach  — ephemeral confirmation; public transaction embed
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="demote_coach", description="Demote the Assistant Head Coach back to player")
@app_commands.describe(player="The AHC to demote")
async def demote_coach(interaction: discord.Interaction, player: discord.Member):
    data = await load_data()
    rid, team = get_team_for_hc(data, interaction.user.id)

    if not team and is_staff(interaction):
        for r, t in data["teams"].items():
            if t.get("ahc_id") == str(player.id):
                rid, team = r, t; break

    if not team:
        await interaction.response.send_message(
            "❌ You're not registered as head coach of any team. Only HCs and staff can demote coaches.",
            ephemeral=True); return

    if team.get("ahc_id") != str(player.id):
        await interaction.response.send_message(
            f"❌ {player.display_name} is not the AHC of **{team['name']}**.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    team["ahc_id"] = None; team["ahc_name"] = None; team["ahc_roblox"] = ""
    for r in team.get("roster", []):
        if r["id"] == str(player.id): r["role"] = "Player"
    await save_data(data)

    # Remove AHC Discord role
    ahc_role = interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID)
    try:
        if ahc_role and ahc_role in player.roles:
            await player.remove_roles(ahc_role, reason="Demoted from AHC via /demote_coach")
    except discord.Forbidden:
        pass

    team_role = get_team_role(interaction.guild, rid)
    embed = await build_coach_transaction_embed("assistant coach demotion", player, team,
                                                team_role, interaction.guild, color=0xED4245)
    await post_transaction(
        interaction.guild, team, embed, team_role,
        followup=interaction.followup,
        ephemeral_msg=f"✅ **{player.display_name}** demoted from AHC of **{team['name']}**.",
        ephemeral=True
    )


# ─────────────────────────────────────────────────────────────────────
# /disband
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="disband", description="Disband a team — removes all players and coaches")
@app_commands.describe(
    confirm="Type DISBAND to confirm",
    team_role="(Staff only) Target team if you're not the HC — HCs don't need this"
)
async def disband(interaction: discord.Interaction, confirm: str, team_role: discord.Role = None):
    if confirm.upper() != "DISBAND":
        await interaction.response.send_message(
            "❌ You must type `DISBAND` exactly in the confirm field.", ephemeral=True); return

    data = await load_data()
    rid, team = get_team_for_hc(data, interaction.user.id)

    if not team and is_staff(interaction) and team_role:
        rid  = str(team_role.id)
        team = get_team_by_role(data, team_role.id)

    if not team:
        await interaction.response.send_message(
            "❌ You're not registered as head coach of any team. Staff must also provide `team_role`.",
            ephemeral=True); return

    team_name    = team["name"]
    former_roster = list(team.get("roster", []))
    former_size  = len(former_roster)
    team["roster"] = []
    team["head_coach_id"] = None; team["head_coach_name"] = None; team["head_coach_roblox"] = ""
    team["ahc_id"] = None; team["ahc_name"] = None; team["ahc_roblox"] = ""
    await save_data(data)

    tr = get_team_role(interaction.guild, rid)

    # Strip the team Discord role from every former member
    if tr:
        for member_data in former_roster:
            try:
                member = interaction.guild.get_member(int(member_data["id"])) or \
                         await interaction.guild.fetch_member(int(member_data["id"]))
                if tr in member.roles:
                    await member.remove_roles(tr, reason=f"{team_name} disbanded")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    embed = discord.Embed(title="team disbanded", color=0xED4245)
    embed.description = (f"**{team_name}** has been disbanded.\n"
                         f"All **{former_size}** player(s) and coaches have been removed.")
    if tr: embed.add_field(name="Team", value=tr.mention, inline=True)
    _set_team_thumbnail(embed, team, interaction.guild)
    embed.set_footer(text=f"Disbanded by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp = datetime.utcnow()

    ch = await get_transactions_channel(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        await interaction.response.send_message(
            f"✅ **{team_name}** has been disbanded. Posted to {ch.mention}.", ephemeral=True)
    else:
        # No transactions channel configured — keep the actual announcement public in the
        # current channel, but the ack to the command runner stays ephemeral.
        await interaction.response.send_message(
            f"✅ **{team_name}** has been disbanded. (No transactions channel configured — posting here.)",
            ephemeral=True)
        await interaction.channel.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# /roster
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="roster", description="View a team's current roster")
@app_commands.describe(team_role="The team's Discord role")
async def roster_cmd(interaction: discord.Interaction, team_role: discord.Role):
    data = await load_data()
    team = get_team_by_role(data, team_role.id)
    if not team:
        await interaction.response.send_message(
            f"❌ {team_role.mention} isn't a registered team.", ephemeral=True); return

    roster = team.get("roster", [])
    color  = team.get("color", UFF_COLOR)
    embed  = discord.Embed(title=f"{team['name'].lower()} roster",
                           description=f"roster: **{len(roster)}/{MAX_ROSTER}**", color=color)

    hc_id  = team.get("head_coach_id")
    hc_rbx = team.get("head_coach_roblox", "")
    if hc_id:
        hc_rbx_str = f"`{hc_rbx}`" if hc_rbx else ""
        embed.add_field(name="head coach:",
            value=f"<@{hc_id}> (@{team.get('head_coach_name', '')}) ✓ {hc_rbx_str}".strip(), inline=False)

    ahc_lines = []
    pl_lines  = []
    for r in roster:
        rbx  = f"`{r['roblox']}`" if r.get("roblox") else ""
        line = f"<@{r['id']}> (@{r['name']}) ✓ {rbx}".strip() if rbx else f"<@{r['id']}> (@{r['name']})"
        role = r.get("role", "Player")
        if role == "Assistant Head Coach":
            ahc_lines.append(f"🥈 {line}")
        elif role != "Head Coach":
            pl_lines.append(f"• {line}")

    if ahc_lines:
        embed.add_field(name="assistant head coach:", value="\n".join(ahc_lines), inline=False)
    if pl_lines:
        embed.add_field(name="players:", value="\n".join(pl_lines), inline=False)
    elif not roster:
        embed.add_field(name="players:", value="*No players on roster yet.*", inline=False)

    _set_team_thumbnail(embed, team, interaction.guild)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /coaches  — single vertical list, league logo (Zevora) as thumbnail
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="coaches", description="View all head coaches across the league")
async def coaches_cmd(interaction: discord.Interaction):
    data  = await load_data()
    teams = data.get("teams", {})
    embed = discord.Embed(title="head coaches", color=UFF_COLOR)

    if not teams:
        embed.description = "*No teams registered yet.*"
    else:
        lines = []
        for rid, team in teams.items():
            hc_id   = team.get("head_coach_id")
            hc_name = team.get("head_coach_name", "")
            hc_rbx  = team.get("head_coach_roblox", "")

            if hc_id:
                hc_rbx_str = f"`{hc_rbx}`" if hc_rbx else ""
                hc_str = f"<@{hc_id}> (@{hc_name}) ✓ {hc_rbx_str}".strip()
            else:
                hc_str = "*vacant*"

            team_role    = get_team_role(interaction.guild, rid)
            role_mention = team_role.mention if team_role else team["name"]

            lines.append(f"{role_mention} — {hc_str}")

        # Single vertical list — chunk into multiple full-width fields if it would
        # exceed Discord's 1024-char field limit, but never split into side-by-side columns.
        chunks, current = [], ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 1000:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        if not chunks:
            chunks = ["*None*"]

        for chunk in chunks:
            embed.add_field(name="\u200b", value=chunk, inline=False)

    # League logo (Zevora) as thumbnail — not a team logo
    if ZEVORA_LOGO_URL:
        embed.set_thumbnail(url=ZEVORA_LOGO_URL)
    elif UFF_THUMBNAIL:
        embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)

    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═════════════════════════════════════════════════════════════════════
# PICKUP COMMANDS
# ═════════════════════════════════════════════════════════════════════

@bot.tree.command(name="pickup_ranked", description="Challenge another player to a ranked UFF pickup")
@app_commands.describe(opponent="The player you want to challenge", game_link="Roblox game link for this match",
                        your_team="Your team name", opponent_team="Opponent's team name")
async def pickup_ranked(interaction: discord.Interaction, opponent: discord.Member,
                        game_link: str, your_team: str, opponent_team: str):
    user_role_ids = {role.id for role in interaction.user.roles}
    if not (user_role_ids & PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.response.send_message("❌ You don't have the required role to start a ranked pickup.", ephemeral=True); return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't challenge yourself!", ephemeral=True); return
    if opponent.bot:
        await interaction.response.send_message("❌ Can't challenge a bot!", ephemeral=True); return

    data = await load_data()
    cd, remaining = on_cooldown(data, interaction.user.id)
    if cd:
        e = discord.Embed(title="⏳ cooldown active",
            description=f"You can challenge again in **{remaining}**.\nCooldown: `{COOLDOWN_MINUTES} minutes`.",
            color=0xE84040)
        e.set_footer(text=UFF_FOOTER)
        await interaction.response.send_message(embed=e, ephemeral=True); return

    p1 = get_player(data, interaction.user.id); p1["username"] = interaction.user.display_name
    p2 = get_player(data, opponent.id);         p2["username"] = opponent.display_name

    match_id = f"{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("pending", {})[match_id] = {
        "challenger_id": str(interaction.user.id), "opponent_id": str(opponent.id),
        "challenger_name": interaction.user.display_name, "opponent_name": opponent.display_name,
        "challenger_team": your_team, "opponent_team": opponent_team,
        "game_link": game_link, "timestamp": datetime.utcnow().isoformat(),
        "match_id": match_id, "guild_id": interaction.guild.id
    }
    await save_data(data)

    r1, emoji1, _ = get_rank(p1["elo"])
    r2, emoji2, _ = get_rank(p2["elo"])

    dm_embed = discord.Embed(title="🏈 You've Been Challenged to a Ranked Pickup!",
        description=f"**{interaction.user.display_name}** wants to play a ranked pickup against you.\nAccept or decline below. Expires in **30 minutes**.",
        color=UFF_COLOR)
    dm_embed.add_field(name=f"🟡 {interaction.user.display_name}",
        value=f"`{interaction.user.name}`\n**{your_team}**\nRank: `{emoji1} {r1}`", inline=True)
    dm_embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    dm_embed.add_field(name=f"🔵 {opponent.display_name}",
        value=f"`{opponent.name}`\n**{opponent_team}**\nRank: `{emoji2} {r2}`", inline=True)
    dm_embed.add_field(name="🔗 Game Link", value=f"[**Click here to join →**]({game_link})", inline=False)
    if UFF_BANNER: dm_embed.set_image(url=UFF_BANNER)
    if UFF_THUMBNAIL: dm_embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild and interaction.guild.icon: dm_embed.set_thumbnail(url=interaction.guild.icon.url)
    dm_embed.set_footer(text=f"Challenge issued by {interaction.user.display_name} | {UFF_FOOTER}")
    dm_embed.timestamp = datetime.utcnow()

    view = RankedPickupView(match_id=match_id, challenger_id=interaction.user.id, opponent_id=opponent.id,
        challenger_name=interaction.user.display_name, opponent_name=opponent.display_name,
        challenger_team=your_team, opponent_team=opponent_team,
        game_link=game_link, guild_id=interaction.guild.id)
    try:
        await opponent.send(embed=dm_embed, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Could not DM **{opponent.display_name}** — they have DMs disabled.", ephemeral=True)
        data.get("pending", {}).pop(match_id, None); await save_data(data); return

    confirm_embed = discord.Embed(title="📨 Challenge Sent!",
        description=f"Your ranked pickup challenge has been sent to **{opponent.display_name}** via DM.\n\nThe match will be posted publicly only **if they accept**.",
        color=0x57F287)
    confirm_embed.set_footer(text=f"{UFF_FOOTER} • 30-minute response window")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)


@bot.tree.command(name="pickup_casual", description="Challenge another player to a casual (unranked) pickup — no ELO changes")
@app_commands.describe(opponent="The player you want to challenge", game_link="Roblox game link",
                        your_team="Your team name", opponent_team="Opponent's team name")
async def pickup_casual(interaction, opponent: discord.Member, game_link: str, your_team: str, opponent_team: str):
    await _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team)

@bot.tree.command(name="casual_pickup", description="Challenge another player to a casual (unranked) pickup — no ELO changes")
@app_commands.describe(opponent="The player you want to challenge", game_link="Roblox game link",
                        your_team="Your team name", opponent_team="Opponent's team name")
async def casual_pickup(interaction, opponent: discord.Member, game_link: str, your_team: str, opponent_team: str):
    await _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team)


@bot.tree.command(name="pickup_results", description="Submit ranked pickup match results and scoreboard screenshot")
@app_commands.describe(winner="Who won?", winner_score="Winner's score",
                        loser_score="Loser's score", screenshot="Scoreboard screenshot")
async def pickup_results(interaction: discord.Interaction, winner: discord.Member,
                         winner_score: int, loser_score: int, screenshot: discord.Attachment):
    data    = await load_data()
    uid     = str(interaction.user.id)
    pending = data.get("pending", {})
    match, match_key = None, None
    for key in sorted(pending, key=lambda k: pending[k].get("timestamp", ""), reverse=True):
        m = pending[key]
        if m["challenger_id"] == uid or m["opponent_id"] == uid:
            match, match_key = m, key; break

    if not match:
        await interaction.response.send_message(
            "❌ No pending ranked pickup found. Use `/pickup_ranked` to start one first.", ephemeral=True); return

    c_id = int(match["challenger_id"]); o_id = int(match["opponent_id"])
    if winner.id not in [c_id, o_id]:
        await interaction.response.send_message(
            "❌ Winner must be one of the two players in this match.", ephemeral=True); return

    loser_id   = o_id if winner.id == c_id else c_id
    loser_name = match["opponent_name"] if winner.id == c_id else match["challenger_name"]

    wp = get_player(data, winner.id); lp = get_player(data, loser_id)
    wp["username"] = winner.display_name
    old_w, old_l = wp["elo"], lp["elo"]
    wp["elo"] += WIN_ELO; lp["elo"] = max(0, lp["elo"] - LOSS_ELO)
    wp["wins"] += 1; lp["losses"] += 1
    now = datetime.utcnow().isoformat()
    wp["last_game"] = now; lp["last_game"] = now

    data.setdefault("matches", []).append({
        "winner_id": str(winner.id), "winner_name": winner.display_name,
        "loser_id": str(loser_id), "loser_name": loser_name,
        "winner_score": winner_score, "loser_score": loser_score,
        "challenger_team": match["challenger_team"], "opponent_team": match["opponent_team"], "date": now
    })
    if match_key and match_key in data["pending"]: del data["pending"][match_key]
    await save_data(data)

    wr, we, wcolor = get_rank(wp["elo"])
    lr, le, _      = get_rank(lp["elo"])

    embed = discord.Embed(title="🏆 pickup results", color=wcolor)
    embed.add_field(name="🏆 Winner",
        value=f"<@{winner.id}> **{winner.display_name}**\n> Score: **{winner_score}**\n> ELO: `{old_w}` → `{wp['elo']}` **(+{WIN_ELO})**\n> Rank: `{we} {wr}`", inline=True)
    embed.add_field(name="❌ Loser",
        value=f"<@{loser_id}> **{loser_name}**\n> Score: **{loser_score}**\n> ELO: `{old_l}` → `{lp['elo']}` **(-{LOSS_ELO})**\n> Rank: `{le} {lr}`", inline=True)
    embed.add_field(name="📊 Final Score",
        value=f"**{winner.display_name}** `{winner_score} — {loser_score}` **{loser_name}**", inline=False)
    embed.set_image(url=screenshot.url)
    if UFF_THUMBNAIL: embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild and interaction.guild.icon: embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.set_footer(text=f"{UFF_FOOTER} • Submitted by {interaction.user.display_name}")
    embed.timestamp = datetime.utcnow()

    # Ephemeral ack to whoever ran the command, but the actual results are always
    # posted publicly — to the configured pickup channel if set, otherwise the current channel.
    ch = await get_pickup_channel(interaction.guild)
    target_channel = ch if (ch and ch.id != interaction.channel_id) else interaction.channel
    await interaction.response.send_message("✅ Results posted!", ephemeral=True)
    await target_channel.send(embed=embed)


@bot.tree.command(name="pickup_profile", description="View UFF pickup rank and stats")
@app_commands.describe(player="Player to look up (leave blank for yourself)")
async def pickup_profile(interaction: discord.Interaction, player: discord.Member = None):
    target = player or interaction.user
    data   = await load_data()
    p      = get_player(data, target.id)
    elo    = p["elo"]
    rank, emoji, color = get_rank(elo)
    gp = p["wins"] + p["losses"]
    wr = f"{p['wins'] / gp * 100:.1f}%" if gp else "N/A"

    embed = discord.Embed(title=f"{emoji} {target.display_name}", color=color)
    embed.add_field(name="Rank",     value=f"`{emoji} {rank}`", inline=True)
    embed.add_field(name="ELO",      value=f"`{elo}`",          inline=True)
    embed.add_field(name="Wins",     value=f"`{p['wins']}`",    inline=True)
    embed.add_field(name="Losses",   value=f"`{p['losses']}`",  inline=True)
    embed.add_field(name="Win Rate", value=f"`{wr}`",           inline=True)
    if target.avatar: embed.set_thumbnail(url=target.avatar.url)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="pickup_leaderboard", description="UFF pickup ELO leaderboard")
async def pickup_leaderboard(interaction: discord.Interaction):
    data    = await load_data()
    players = data.get("players", {})
    if not players:
        await interaction.response.send_message("No players yet — play some pickups!", ephemeral=True); return
    top    = sorted(players.items(), key=lambda x: x[1]["elo"], reverse=True)[:15]
    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, (uid, p) in enumerate(top):
        rank, emoji, _ = get_rank(p["elo"])
        medal = medals[i] if i < 3 else f"`{i + 1}.`"
        name  = p.get("username") or f"<@{uid}>"
        lines.append(f"{medal} **{name}** — {emoji} `{rank}` | ELO `{p['elo']}` | {p['wins']}W {p['losses']}L")
    embed = discord.Embed(title="UFF Pickup — ELO Leaderboard", description="\n".join(lines), color=UFF_COLOR)
    apply_branding(embed)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="match_history", description="View recent UFF pickup results")
async def match_history(interaction: discord.Interaction):
    data    = await load_data()
    matches = list(reversed(data.get("matches", [])))[:10]
    if not matches:
        await interaction.response.send_message("No matches recorded yet.", ephemeral=True); return
    lines = [
        f"🏆 **{m['winner_name']}** `{m.get('winner_score','?')}–{m.get('loser_score','?')}` {m['loser_name']}"
        for m in matches
    ]
    embed = discord.Embed(title="📋 UFF Pickup — Recent Results", description="\n".join(lines), color=0x4090E8)
    embed.set_footer(text=f"{UFF_FOOTER} • Last 10 matches")
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="teams", description="View all 20 UFF league teams")
async def teams_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="United Flag Football — All Teams", color=UFF_COLOR)
    embed.add_field(name="Teams 1–10",  value="\n".join(f"`{t[1]}` **{t[0]}**" for t in TEAMS[:10]),  inline=True)
    embed.add_field(name="Teams 11–20", value="\n".join(f"`{t[1]}` **{t[0]}**" for t in TEAMS[10:]), inline=True)
    apply_branding(embed)
    embed.set_footer(text=f"{UFF_FOOTER} • 20 teams")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="reset_player", description="[Admin] Reset a player's ELO to 900")
@app_commands.describe(player="Player to reset")
@app_commands.default_permissions(administrator=True)
async def reset_player(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
    data = await load_data()
    data["players"][str(player.id)] = {
        "elo": STARTING_ELO, "wins": 0, "losses": 0,
        "last_game": None, "username": player.display_name
    }
    await save_data(data)
    await interaction.response.send_message(
        f"✅ Reset **{player.display_name}**'s ELO to `{STARTING_ELO}`.", ephemeral=True)


@bot.tree.command(name="adjust_elo", description="[Admin] Manually adjust a player's ELO")
@app_commands.describe(player="Target player", amount="ELO to add (negative to subtract)")
@app_commands.default_permissions(administrator=True)
async def adjust_elo(interaction: discord.Interaction, player: discord.Member, amount: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
    data = await load_data(); p = get_player(data, player.id); old = p["elo"]
    p["elo"] = max(0, p["elo"] + amount); p["username"] = player.display_name
    await save_data(data)
    sign = "+" if amount >= 0 else ""
    await interaction.response.send_message(
        f"✅ **{player.display_name}** ELO: `{old}` → `{p['elo']}` ({sign}{amount})", ephemeral=True)


@bot.tree.command(name="clear_cooldown", description="[Admin] Clear a player's cooldown")
@app_commands.describe(player="Player to clear")
@app_commands.default_permissions(administrator=True)
async def clear_cooldown(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
    data = await load_data(); get_player(data, player.id)["last_game"] = None; await save_data(data)
    await interaction.response.send_message(
        f"✅ Cleared cooldown for **{player.display_name}**.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /suspension  &  /unsuspend
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="suspension", description="[Staff] Issue a suspension to a player")
@app_commands.describe(player="The player to suspend")
async def suspension(interaction: discord.Interaction, player: discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message("❌ You don't have permission to issue suspensions.", ephemeral=True); return
    view = SuspensionView(target=player, issuer_id=interaction.user.id)
    await interaction.response.send_message(
        content=f"**Issuing suspension for {player.display_name}**\nSelect up to {MAX_SUSPENSION_REASONS} reasons below.",
        view=view, ephemeral=True)


@bot.tree.command(name="unsuspend", description="[Staff] Clear a player's suspension")
@app_commands.describe(player="The player to unsuspend")
async def unsuspend(interaction: discord.Interaction, player: discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message("❌ You don't have permission to clear suspensions.", ephemeral=True); return
    data = await load_data(); cleared_any = False
    for s in data.get("suspensions", []):
        if s.get("player_id") == str(player.id) and not s.get("cleared", False):
            s["cleared"] = True; s["cleared_by"] = str(interaction.user.id)
            s["cleared_by_name"] = interaction.user.display_name
            s["cleared_date"] = datetime.utcnow().isoformat(); cleared_any = True
    await save_data(data)

    embed = discord.Embed(title="player unsuspended", color=0x57F287)
    embed.add_field(name="Player", value=f"<@{player.id}> ({player.display_name})", inline=False)
    embed.add_field(name="Status", value="**Cleared** — eligible to play", inline=False)
    if player.avatar: embed.set_thumbnail(url=player.avatar.url)
    embed.set_footer(text=f"Cleared by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp = datetime.utcnow()

    ch = await get_suspension_channel(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        note = "" if cleared_any else "\n*(No open suspension records found, but notice was posted anyway.)*"
        await interaction.response.send_message(
            f"✅ {player.display_name} has been unsuspended. Posted to {ch.mention}.{note}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Could not find the suspension channel.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /help_uff
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="help_uff", description="UFF bot command guide")
async def help_uff(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 United Flag Football — Commands", color=UFF_COLOR)
    embed.add_field(name="📋 Transactions", value=(
        "`/set_team` — [Staff] Register a role as a team (pulls name + icon from role automatically)\n"
        "`/set_team_image` — [Staff] Override the team logo URL\n"
        "`/assign_hc` — [Staff] Assign a head coach to a team\n"
        "`/offer` — [HC/AHC] Send a player a DM roster offer (12h to accept)\n"
        "`/release` — [HC/AHC/Staff] Release a player from your team\n"
        "`/demand_release` — Demand your own release (1 free lifetime demand)\n"
        "`/grant_extra_demand` — [Owner] Grant a player an extra demand token\n"
        "`/promote_coach` — [HC/Staff] Promote a player to Assistant Head Coach\n"
        "`/demote_coach` — [HC/Staff] Demote the AHC back to player\n"
        "`/disband` — [HC/Staff] Disband a team (clears all players & coaches)\n"
        "`/roster` — View a team's roster\n"
        "`/coaches` — View all head coaches in the league"
    ), inline=False)
    embed.add_field(name="⚔️ Ranked Pickup", value=(
        "`/pickup_ranked` — Challenge to a ranked pickup (ELO affected)\n"
        "`/pickup_results` — Submit results + screenshot after the game\n"
        "`/pickup_profile` — Your ELO, rank & stats\n"
        "`/pickup_leaderboard` — Top 15 ELO rankings\n"
        "`/match_history` — Last 10 results"
    ), inline=False)
    embed.add_field(name="🎮 Casual Pickup",
        value="`/pickup_casual` or `/casual_pickup` — Casual pickup, no ELO changes", inline=False)
    embed.add_field(name="🏟️ League", value="`/teams` — View all 20 UFF teams", inline=False)
    embed.add_field(name="🛡️ Admin", value=(
        "`/reset_player` — Reset ELO to 900\n"
        "`/adjust_elo` — Manually change ELO\n"
        "`/clear_cooldown` — Remove cooldown"
    ), inline=False)
    embed.add_field(name="🚫 Suspensions", value=(
        "`/suspension` — [Staff] Issue a suspension\n"
        "`/unsuspend` — [Staff] Clear a suspension"
    ), inline=False)
    embed.add_field(name="📊 Ranks", value=(
        "**Start:** 900 ELO | **Win:** +100 | **Loss:** −100\n"
        "⚙️ Iron I/II/III → 0/700/900 ELO\n"
        "🥇 Gold I/II/III → 1100/1300/1500 ELO\n"
        "💎 Amethyst I/II/III → 1700/1900/2100 ELO"
    ), inline=False)
    apply_branding(embed)
    embed.set_footer(text=f"{UFF_FOOTER} • {COOLDOWN_MINUTES}-min ranked cooldown")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
