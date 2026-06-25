"""
UFF Discord Bot — Python
United Flag Football League — Pickup Ranked System + Transactions

KEY CHANGES IN THIS VERSION:
  - Transaction embed layout matches reference screenshots:
      * Compact, not tall
      * Team logo shown via embed.set_image (bottom) — acts as the team badge
      * Roblox headshot shown via embed.set_thumbnail (top-right corner)
      * Info block uses "> " prefix for each line → Discord renders a dark left border
      * Author line: team emoji role ping + "have signed/released/etc @user (roblox_id)"
  - Bloxlink avatar fetch fixed: uses correct thumbnails.roblox.com v1 endpoint
    with proper JSON unwrapping so headshots actually appear
  - Pillow compositing removed — was causing failures silently; headshot/logo are
    separate embed fields which is more reliable and matches the reference layout
  - ZEVORA_LOGO_URL env var used as league logo in /coaches and as fallback thumbnail
  - /coaches is a single vertical list
  - All non-transaction user-facing responses are ephemeral
  - Staff roles updated (Co-Founder, Zevora, Advisory added; Moderators removed)
  - Role grant/removal failures now surface a visible warning to staff

TRANSACTIONS FLOW:
  /set_team            — [Staff] Register a Discord role as a UFF team
  /set_team_image      — [Staff] Override the team logo URL
  /assign_hc           — [Staff] Assign a head coach to a team
  /offer               — [HC/AHC/Staff] Send a player a roster offer via DM (12h)
  /release             — [HC/AHC/Staff] Release a player (auto-detected team)
  /demand              — Player demands their own release (1 lifetime demand)
  /grant_extra_demand  — [Owner IDs only] Give a player an extra demand token
  /promote_coach       — [HC/Staff] Promote a player to AHC
  /demote_coach        — [HC/Staff] Demote the AHC back to player
  /disband             — [HC/Staff] Remove all players and coaches from a team
  /roster              — [Public] View a team's roster
  /coaches             — [Public] View all head coaches across the league

PICKUP FLOW:
  /pickup_ranked  → DM opponent → Accept/Decline → public post → /pickup_results
  /pickup_casual or /casual_pickup → same flow, no ELO

SUSPENSIONS:
  /suspension  — staff-only, up to 5 stackable reasons
  /unsuspend   — staff-only, posts clearance notice

DATABASE:
  PostgreSQL via Railway. Auto-creates table on startup.
  Set DATABASE_URL env var (Railway PostgreSQL plugin sets this automatically).

RAILWAY ENV VARS NEEDED:
  DISCORD_BOT_TOKEN, OWNER_DISCORD_ID, QBB_CHANNEL_ID, BLOXLINK_API_KEY,
  DATABASE_URL, ZEVORA_LOGO_URL, UFF_THUMBNAIL_URL (optional), UFF_BANNER_URL (optional)
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
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────────────────────────────
TOKEN               = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID            = int(os.getenv("OWNER_DISCORD_ID", "0"))
SECRET_KEY          = os.getenv("SECRET_KEY", "")
QBB_CHANNEL_ID      = int(os.getenv("QBB_CHANNEL_ID", "0"))
BLOXLINK_API_KEY    = os.getenv("BLOXLINK_API_KEY", "")
DATABASE_URL        = os.getenv("DATABASE_URL", "")

UFF_THUMBNAIL   = os.getenv("UFF_THUMBNAIL_URL", "")
UFF_BANNER      = os.getenv("UFF_BANNER_URL", "")
ZEVORA_LOGO_URL = os.getenv("ZEVORA_LOGO_URL", "")  # league logo for /coaches + fallback

TRANSACTIONS_CHANNEL_ID = 1262200420151984152

UFF_FOOTER   = "United Flag Football League"
UFF_COLOR    = 0xF0C040
CASUAL_COLOR = 0x5865F2

GUILD_ID = 1262200419564785755

# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────
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

# Staff roles — can run any transaction on any team
STAFF_ROLE_IDS = {
    1404271002241728617,  # League Boards
    1429344923865448550,  # Operations Director
    1262200419686285342,  # Commissioner
    1401450124424642561,  # Founder
    1499141732108079225,  # Co-Founder
    1434653599236882574,  # Zevora
    1502941495722770472,  # Advisory
}

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

SPECIAL_SUSPENSION_REASONS = {
    "ineligible_until_ss": "Ineligible Until Screenshare",
}

EXTRA_DEMAND_GRANT_USER_IDS = {
    1055321446978691112,
    391036854084042762,
}

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
# POSTGRESQL
# ─────────────────────────────────────────────────────────────────────
_db_pool: asyncpg.Pool | None = None


async def get_db() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _db_pool


async def init_db():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS uff_store (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


async def db_get(key: str):
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM uff_store WHERE key = $1", key)
    return json.loads(row["value"]) if row else None


async def db_set(key: str, value) -> None:
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO uff_store (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, json.dumps(value, default=str)
        )


async def load_data() -> dict:
    keys = ["players", "matches", "pending", "casual_pending",
            "suspensions", "teams", "demand_used", "extra_demands", "offers"]
    data = {}
    for k in keys:
        val = await db_get(k)
        if val is None:
            val = [] if k in ("matches", "suspensions") else {}
        data[k] = val
    return data


async def save_data(data: dict) -> None:
    for key, value in data.items():
        await db_set(key, value)


# ─────────────────────────────────────────────────────────────────────
# RANK / PLAYER HELPERS
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
    return interaction.user.id == OWNER_ID or interaction.user.guild_permissions.administrator


def is_staff(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return True
    return bool({r.id for r in interaction.user.roles} & STAFF_ROLE_IDS)


def can_issue_suspension(interaction: discord.Interaction) -> bool:
    if interaction.user.id in SUSPENSION_ALLOWED_USER_IDS:
        return True
    return bool({r.id for r in interaction.user.roles} & SUSPENSION_ALLOWED_ROLE_IDS) or is_admin(interaction)


def apply_branding(embed: discord.Embed) -> discord.Embed:
    thumb = ZEVORA_LOGO_URL or UFF_THUMBNAIL
    if thumb:
        embed.set_thumbnail(url=thumb)
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


async def get_pickup_channel(guild):
    return await get_channel_safe(guild, QBB_CHANNEL_ID)


async def get_suspension_channel(guild):
    return await get_channel_safe(guild, SUSPENSION_CHANNEL_ID)


async def get_transactions_channel(guild):
    return await get_channel_safe(guild, TRANSACTIONS_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────
# BLOXLINK — fixed avatar fetch
# ─────────────────────────────────────────────────────────────────────
async def bloxlink_lookup(discord_id: int, guild_id: int) -> dict:
    """
    Returns {"roblox_username": str, "roblox_id": int, "avatar_url": str}
    or {"error": str}.

    The headshot endpoint is:
      GET https://thumbnails.roblox.com/v1/users/avatar-headshot
           ?userIds=<id>&size=150x150&format=Png&isCircular=false
    It returns JSON like:
      {"data": [{"targetId": 123, "state": "Completed", "imageUrl": "https://..."}]}
    We pull data[0].imageUrl.
    """
    if not BLOXLINK_API_KEY:
        return {"error": "BLOXLINK_API_KEY not set."}

    headers = {"Authorization": BLOXLINK_API_KEY}
    bl_url  = f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}"

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Discord → Roblox ID via Bloxlink
            async with session.get(bl_url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return {"error": f"Bloxlink HTTP {resp.status}"}
                body = await resp.json()

        roblox_id = body.get("robloxID")
        if not roblox_id:
            return {"error": "No Roblox account linked via Bloxlink."}

        # 2. Roblox username
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://users.roblox.com/v1/users/{roblox_id}",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                udata = await resp.json() if resp.status == 200 else {}
        username = udata.get("name", str(roblox_id))

        # 3. Headshot — thumbnails endpoint returns JSON, not an image directly
        avatar_url = ""
        thumb_url  = (
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={roblox_id}&size=150x150&format=Png&isCircular=false"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(thumb_url,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    tdata      = await resp.json()
                    items      = tdata.get("data", [])
                    avatar_url = items[0].get("imageUrl", "") if items else ""

        return {
            "roblox_username": username,
            "roblox_id":       int(roblox_id),
            "avatar_url":      avatar_url,
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────
# SUSPENSION HELPERS
# ─────────────────────────────────────────────────────────────────────
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
            lines.append(f"• **{label}** — {per_game}g ×{count} = **{subtotal} games**")
        else:
            lines.append(f"• **{label}** — {per_game} games")

    reason_lines = "\n".join(lines)
    status_lines = ""
    if special_keys:
        status_labels = [SPECIAL_SUSPENSION_REASONS[r] for r in special_keys]
        status_lines  = "**Status:** " + ", ".join(f"`{s}`" for s in status_labels)

    return total_games, reason_lines, status_lines


# ─────────────────────────────────────────────────────────────────────
# TRANSACTION HELPERS
# ─────────────────────────────────────────────────────────────────────
def get_team_by_role(data: dict, role_id: int):
    return data["teams"].get(str(role_id))


def get_team_for_hc(data: dict, user_id: int):
    uid = str(user_id)
    for rid, team in data["teams"].items():
        if team.get("head_coach_id") == uid:
            return rid, team
    return None, None


def get_team_for_user(data: dict, user_id: int):
    uid = str(user_id)
    for rid, team in data["teams"].items():
        if team.get("head_coach_id") == uid:
            return rid, team
    for rid, team in data["teams"].items():
        if team.get("ahc_id") == uid:
            return rid, team
    return None, None


def get_team_role(guild: discord.Guild, role_id_str: str):
    try:
        return guild.get_role(int(role_id_str))
    except (ValueError, TypeError):
        return None


def _role_failure_note(role_label: str) -> str:
    return (
        f"\n⚠️ Could not update the **{role_label}** Discord role — make sure this bot's "
        f"role is positioned ABOVE that role in Server Settings → Roles, and that the bot "
        f"has **Manage Roles** permission."
    )


def _best_thumbnail(guild: discord.Guild) -> str:
    """League logo > UFF thumbnail > guild icon."""
    return ZEVORA_LOGO_URL or UFF_THUMBNAIL or (str(guild.icon.url) if guild and guild.icon else "")


# ─────────────────────────────────────────────────────────────────────
# TRANSACTION EMBED BUILDER
# ─────────────────────────────────────────────────────────────────────
def _info_block_lines(team: dict) -> str:
    """
    Returns the info block as "> " prefixed lines so Discord renders a
    dark vertical left-border bar — matches the reference screenshot.
    """
    roster_size = len(team.get("roster", []))
    hc_id    = team.get("head_coach_id")
    hc_name  = team.get("head_coach_name", "")
    hc_rbx   = team.get("head_coach_roblox", "")
    ahc_id   = team.get("ahc_id")
    ahc_name = team.get("ahc_name", "")
    ahc_rbx  = team.get("ahc_roblox", "")

    lines = [f"> roster: {roster_size}/{MAX_ROSTER}"]

    if hc_id:
        rbx_str = f"`{hc_rbx}`" if hc_rbx else ""
        lines.append(f"> head coach: <@{hc_id}> (@{hc_name}) ✓ {rbx_str}".rstrip())
    else:
        lines.append("> head coach: vacant")

    if ahc_id:
        rbx_str = f"`{ahc_rbx}`" if ahc_rbx else ""
        lines.append(f"> assistant coach: <@{ahc_id}> (@{ahc_name}) ✓ {rbx_str}".rstrip())
    else:
        lines.append("> assistant coach: vacant")

    return "\n".join(lines)


async def build_transaction_embed(
    action: str,
    player: discord.Member,
    team: dict,
    team_role,
    guild: discord.Guild,
    color: int = UFF_COLOR,
) -> discord.Embed:
    """
    Compact transaction embed matching the reference screenshots:
      - Title: action name (e.g. "signed", "released")
      - Description: "{team ping} have {action} {player ping} ({discord_name})\n`roblox_username`"
      - Info block with "> " prefix lines (dark left border)
      - Thumbnail (top-right): player's Roblox headshot
      - Image (bottom): team logo (the big badge-style image)
    """
    blox          = await bloxlink_lookup(player.id, guild.id)
    roblox_name   = blox.get("roblox_username", "Unknown")
    roblox_avatar = blox.get("avatar_url", "")

    role_str = team_role.mention if team_role else f"**{team['name']}**"
    embed = discord.Embed(
        description=(
            f"{role_str} have **{action.lower()}** {player.mention} (@{player.name})\n"
            f"`{roblox_name}`\n\n"
            f"{_info_block_lines(team)}"
        ),
        color=color,
    )
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()

    # Roblox headshot → thumbnail (top-right corner)
    if roblox_avatar:
        embed.set_thumbnail(url=roblox_avatar)
    else:
        # Fallback: team logo or league logo
        logo = team.get("logo_url", "") or _best_thumbnail(guild)
        if logo:
            embed.set_thumbnail(url=logo)

    # Team logo → image at the bottom of the embed
    logo = team.get("logo_url", "")
    if logo:
        embed.set_image(url=logo)

    return embed


async def build_coach_embed(
    action: str,
    player: discord.Member,
    team: dict,
    team_role,
    guild: discord.Guild,
    color: int = UFF_COLOR,
) -> discord.Embed:
    """Same compact layout for coach promotions/demotions."""
    blox          = await bloxlink_lookup(player.id, guild.id)
    roblox_name   = blox.get("roblox_username", "Unknown")
    roblox_avatar = blox.get("avatar_url", "")

    is_promo = "promot" in action.lower()
    role_lbl = "assistant coach" if is_promo else "regular player"
    role_str = team_role.mention if team_role else f"**{team['name']}**"

    embed = discord.Embed(
        description=(
            f"{role_str} have **{action.lower()}** {player.mention} (@{player.name})\n"
            f"`{roblox_name}` to {role_lbl}!\n\n"
            f"{_info_block_lines(team)}"
        ),
        color=color,
    )
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()

    if roblox_avatar:
        embed.set_thumbnail(url=roblox_avatar)
    else:
        logo = team.get("logo_url", "") or _best_thumbnail(guild)
        if logo:
            embed.set_thumbnail(url=logo)

    logo = team.get("logo_url", "")
    if logo:
        embed.set_image(url=logo)

    return embed


async def post_transaction(
    guild: discord.Guild,
    embed: discord.Embed,
    followup=None,
    interaction=None,
    ephemeral_msg: str = "",
    ephemeral: bool = True,
):
    """Post embed to the transactions channel; send ephemeral ack to the command runner."""
    ch = await get_transactions_channel(guild)
    if ch:
        await ch.send(embed=embed)
        if followup and ephemeral_msg:
            await followup.send(ephemeral_msg, ephemeral=ephemeral)
        elif interaction and ephemeral_msg:
            try:
                await interaction.response.send_message(ephemeral_msg, ephemeral=ephemeral)
            except discord.InteractionResponded:
                await interaction.followup.send(ephemeral_msg, ephemeral=ephemeral)
    else:
        # No channel configured — send publicly in current channel
        if followup:
            await followup.send(embed=embed)
        elif interaction:
            try:
                await interaction.response.send_message(embed=embed)
            except discord.InteractionResponded:
                await interaction.followup.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# OFFER VIEW
# ─────────────────────────────────────────────────────────────────────
class OfferView(discord.ui.View):
    def __init__(self, offer_id, team_role_id, team_name, team_logo, hc_id, player_id, guild_id):
        super().__init__(timeout=43200)
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
                content=f"❌ Roster cap reached ({MAX_ROSTER}/{MAX_ROSTER}).", view=self)
            return
        if str(interaction.user.id) in [r["id"] for r in roster]:
            await interaction.response.edit_message(
                content=f"❌ You're already on **{team['name']}**.", view=self)
            return

        try:
            player = guild.get_member(self.player_id) or await guild.fetch_member(self.player_id)
        except discord.NotFound:
            await interaction.response.edit_message(content="❌ Could not find you in the server.", view=self)
            return

        blox        = await bloxlink_lookup(player.id, guild.id)
        roblox_name = blox.get("roblox_username", "Unknown")
        roblox_avatar = blox.get("avatar_url", "")

        roster.append({"id": str(player.id), "name": player.display_name,
                       "roblox": roblox_name, "role": "Player"})
        data.get("offers", {}).pop(self.offer_id, None)
        await save_data(data)

        team_role  = guild.get_role(int(rid))
        role_str   = team_role.mention if team_role else f"**{team['name']}**"
        color      = team.get("color", UFF_COLOR)
        role_failed = False
        if team_role:
            try:
                await player.add_roles(team_role, reason=f"Signed to {team['name']}")
            except discord.Forbidden:
                role_failed = True

        embed = discord.Embed(
            description=(
                f"{role_str} have **signed** {player.mention} (@{player.name})\n"
                f"`{roblox_name}`\n\n"
                f"{_info_block_lines(team)}"
            ),
            color=color,
        )
        embed.set_footer(text=UFF_FOOTER)
        embed.timestamp = datetime.utcnow()
        if roblox_avatar:
            embed.set_thumbnail(url=roblox_avatar)
        else:
            logo = team.get("logo_url", "") or _best_thumbnail(guild)
            if logo:
                embed.set_thumbnail(url=logo)
        if team.get("logo_url"):
            embed.set_image(url=team["logo_url"])

        ch = await get_transactions_channel(guild)
        if ch:
            await ch.send(embed=embed)

        desc = f"You accepted the offer from **{self.team_name}**!\n\nWelcome to the team."
        if role_failed:
            desc += "\n\n⚠️ The team Discord role couldn't be added automatically — ask a coach or staff member."
        accepted_embed = discord.Embed(title="✅ Offer Accepted!", description=desc, color=0x57F287)
        if self.team_logo:
            accepted_embed.set_thumbnail(url=self.team_logo)
        accepted_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=accepted_embed, view=self)

        if self.hc_id:
            try:
                hc_member = guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id))
                n = discord.Embed(title="✅ Offer Accepted",
                    description=f"**{player.display_name}** accepted your offer to join **{self.team_name}**.",
                    color=0x57F287)
                n.set_footer(text=UFF_FOOTER)
                await hc_member.send(embed=n)
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

        e = discord.Embed(title="❌ Offer Declined",
            description=f"You declined the offer from **{self.team_name}**.", color=0xED4245)
        if self.team_logo:
            e.set_thumbnail(url=self.team_logo)
        e.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=e, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild and self.hc_id:
            try:
                hc  = guild.get_member(int(self.hc_id)) or await guild.fetch_member(int(self.hc_id))
                tgt = guild.get_member(self.player_id)
                tgt_name = tgt.display_name if tgt else "The player"
                n = discord.Embed(title="❌ Offer Declined",
                    description=f"**{tgt_name}** declined your offer to join **{self.team_name}**.",
                    color=0xED4245)
                n.set_footer(text=UFF_FOOTER)
                await hc.send(embed=n)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# RANKED PICKUP VIEW
# ─────────────────────────────────────────────────────────────────────
class RankedPickupView(discord.ui.View):
    def __init__(self, match_id, challenger_id, opponent_id, challenger_name,
                 opponent_name, challenger_team, opponent_team, game_link, guild_id):
        super().__init__(timeout=1800)
        self.match_id        = match_id
        self.challenger_id   = challenger_id
        self.opponent_id     = opponent_id
        self.challenger_name = challenger_name
        self.opponent_name   = opponent_name
        self.challenger_team = challenger_team
        self.opponent_team   = opponent_team
        self.game_link       = game_link
        self.guild_id        = guild_id
        self.responded       = False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="✅  Accept Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True
        await interaction.message.edit(view=self)

        data  = await load_data()
        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Server not found.", ephemeral=True); return
        try:
            challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent   = guild.get_member(self.opponent_id)   or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not find one of the players.", ephemeral=True); return

        p1 = get_player(data, challenger.id); p2 = get_player(data, opponent.id)
        r1, e1, _ = get_rank(p1["elo"]); r2, e2, _ = get_rank(p2["elo"])

        embed = discord.Embed(title="ranked pickup matchup", color=UFF_COLOR)
        embed.add_field(name=f"🟡 {challenger.display_name}",
            value=f"{challenger.mention}\n**{self.challenger_team}**\nRank: `{e1} {r1}`", inline=True)
        embed.add_field(name="\u200b", value="**— VS —**", inline=True)
        embed.add_field(name=f"🔵 {opponent.display_name}",
            value=f"{opponent.mention}\n**{self.opponent_team}**\nRank: `{e2} {r2}`", inline=True)
        embed.add_field(name="🔗 game link", value=f"[Click here to join →]({self.game_link})", inline=False)
        if UFF_BANNER: embed.set_image(url=UFF_BANNER)
        thumb = _best_thumbnail(guild)
        if thumb: embed.set_thumbnail(url=thumb)
        embed.set_footer(text=f"✅ LIVE • /pickup_results when done | {UFF_FOOTER}")
        embed.timestamp = datetime.utcnow()

        ch = await get_pickup_channel(guild)
        if ch:
            await ch.send(
                content=f"@everyone **Ranked Pickup** — **{challenger.display_name}** vs **{opponent.display_name}**",
                embed=embed
            )
        else:
            try: await challenger.send(embed=embed)
            except discord.Forbidden: pass

        ack = discord.Embed(title="✅ Challenge Accepted!",
            description=f"You accepted **{self.challenger_name}**'s challenge!\n🔗 [Join the game]({self.game_link})",
            color=0x57F287)
        ack.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=ack, view=self)

        try:
            n = discord.Embed(title="✅ Challenge Accepted!",
                description=f"**{self.opponent_name}** accepted your pickup challenge!\n"
                            f"🔗 [Join the game]({self.game_link})\n\nUse `/pickup_results` when done.",
                color=0x57F287)
            n.set_footer(text=UFF_FOOTER)
            await challenger.send(embed=n)
        except discord.Forbidden: pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True

        data = await load_data()
        data.get("pending", {}).pop(self.match_id, None)
        await save_data(data)

        e = discord.Embed(title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s pickup challenge.", color=0xED4245)
        e.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=e, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild:
            try:
                challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                n = discord.Embed(title="❌ Challenge Declined",
                    description=f"**{self.opponent_name}** declined your pickup challenge.", color=0xED4245)
                n.set_footer(text=UFF_FOOTER)
                await challenger.send(embed=n)
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# CASUAL PICKUP VIEW
# ─────────────────────────────────────────────────────────────────────
class CasualPickupView(discord.ui.View):
    def __init__(self, match_id, challenger_id, opponent_id, challenger_name,
                 opponent_name, challenger_team, opponent_team, game_link, guild_id):
        super().__init__(timeout=1800)
        self.match_id        = match_id
        self.challenger_id   = challenger_id
        self.opponent_id     = opponent_id
        self.challenger_name = challenger_name
        self.opponent_name   = opponent_name
        self.challenger_team = challenger_team
        self.opponent_team   = opponent_team
        self.game_link       = game_link
        self.guild_id        = guild_id
        self.responded       = False

    async def on_timeout(self):
        for item in self.children: item.disabled = True

    @discord.ui.button(label="✅  Accept Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True
        await interaction.message.edit(view=self)

        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Server not found.", ephemeral=True); return
        try:
            challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent   = guild.get_member(self.opponent_id)   or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not find one of the players.", ephemeral=True); return

        data = await load_data()
        data.get("casual_pending", {}).pop(self.match_id, None)
        await save_data(data)

        embed = discord.Embed(title="casual pickup matchup", color=CASUAL_COLOR)
        embed.add_field(name=f"🟡 {challenger.display_name}",
            value=f"{challenger.mention}\n**{self.challenger_team}**", inline=True)
        embed.add_field(name="\u200b", value="**— VS —**", inline=True)
        embed.add_field(name=f"🔵 {opponent.display_name}",
            value=f"{opponent.mention}\n**{self.opponent_team}**", inline=True)
        embed.add_field(name="🔗 game link", value=f"[Click here to join →]({self.game_link})", inline=False)
        if UFF_BANNER: embed.set_image(url=UFF_BANNER)
        thumb = _best_thumbnail(guild)
        if thumb: embed.set_thumbnail(url=thumb)
        embed.set_footer(text=f"✅ LIVE | {UFF_FOOTER}")
        embed.timestamp = datetime.utcnow()

        ch = await get_pickup_channel(guild)
        if ch:
            await ch.send(
                content=f"@here **Casual Pickup** — **{challenger.display_name}** vs **{opponent.display_name}**",
                embed=embed
            )
        else:
            try: await challenger.send(embed=embed)
            except discord.Forbidden: pass

        ack = discord.Embed(title="✅ Challenge Accepted!",
            description=f"You accepted **{self.challenger_name}**'s casual pickup!\n🔗 [Join]({self.game_link})",
            color=0x57F287)
        ack.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=ack, view=self)

        try:
            n = discord.Embed(title="✅ Challenge Accepted!",
                description=f"**{self.opponent_name}** accepted your casual pickup!\n🔗 [Join]({self.game_link})",
                color=0x57F287)
            n.set_footer(text=UFF_FOOTER)
            await challenger.send(embed=n)
        except discord.Forbidden: pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True); return
        if self.responded:
            await interaction.response.send_message("Already answered.", ephemeral=True); return
        self.responded = True; self.stop()
        for item in self.children: item.disabled = True

        data = await load_data()
        data.get("casual_pending", {}).pop(self.match_id, None)
        await save_data(data)

        e = discord.Embed(title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s casual pickup.", color=0xED4245)
        e.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=e, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild:
            try:
                challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                n = discord.Embed(title="❌ Challenge Declined",
                    description=f"**{self.opponent_name}** declined your casual pickup.", color=0xED4245)
                n.set_footer(text=UFF_FOOTER)
                await challenger.send(embed=n)
            except Exception: pass


# ─────────────────────────────────────────────────────────────────────
# SHARED CASUAL PICKUP LOGIC
# ─────────────────────────────────────────────────────────────────────
async def _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team):
    if not ({r.id for r in interaction.user.roles} & PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.response.send_message("❌ You don't have the required role.", ephemeral=True); return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't challenge yourself!", ephemeral=True); return
    if opponent.bot:
        await interaction.response.send_message("❌ Can't challenge a bot!", ephemeral=True); return

    data     = await load_data()
    match_id = f"casual_{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("casual_pending", {})[match_id] = {
        "challenger_id": str(interaction.user.id), "opponent_id": str(opponent.id),
        "challenger_name": interaction.user.display_name, "opponent_name": opponent.display_name,
        "challenger_team": your_team, "opponent_team": opponent_team, "game_link": game_link,
        "timestamp": datetime.utcnow().isoformat(), "match_id": match_id, "guild_id": interaction.guild.id
    }
    await save_data(data)

    dm = discord.Embed(title="🏈 You've Been Challenged to a Casual Pickup!",
        description=(f"**{interaction.user.display_name}** wants a casual pickup.\n"
                     f"Expires in **30 minutes**."), color=CASUAL_COLOR)
    dm.add_field(name=f"🟡 {interaction.user.display_name}", value=f"`{interaction.user.name}`\n**{your_team}**", inline=True)
    dm.add_field(name="\u200b", value="**— VS —**", inline=True)
    dm.add_field(name=f"🔵 {opponent.display_name}", value=f"`{opponent.name}`\n**{opponent_team}**", inline=True)
    dm.add_field(name="🔗 Game Link", value=f"[Click here to join →]({game_link})", inline=False)
    dm.add_field(name="\u200b", value="⚠️ **NOT ranked** — no ELO changes.", inline=False)
    if UFF_BANNER: dm.set_image(url=UFF_BANNER)
    thumb = _best_thumbnail(interaction.guild)
    if thumb: dm.set_thumbnail(url=thumb)
    dm.set_footer(text=f"Issued by {interaction.user.display_name} | {UFF_FOOTER}")
    dm.timestamp = datetime.utcnow()

    view = CasualPickupView(match_id=match_id, challenger_id=interaction.user.id, opponent_id=opponent.id,
        challenger_name=interaction.user.display_name, opponent_name=opponent.display_name,
        challenger_team=your_team, opponent_team=opponent_team, game_link=game_link, guild_id=interaction.guild.id)
    try:
        await opponent.send(embed=dm, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Could not DM **{opponent.display_name}**.", ephemeral=True)
        data.get("casual_pending", {}).pop(match_id, None); await save_data(data); return

    ack = discord.Embed(title="📨 Casual Pickup Sent!",
        description=f"Challenge sent to **{opponent.display_name}** via DM. Posted publicly only if they accept.",
        color=0x57F287)
    ack.set_footer(text=f"{UFF_FOOTER} • 30-minute window")
    await interaction.response.send_message(embed=ack, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# SUSPENSION UI
# ─────────────────────────────────────────────────────────────────────
def _make_suspension_options():
    options = []
    seen: dict[str, int] = {}
    for key, (label, games) in SUSPENSION_REASONS.items():
        seen[label] = seen.get(label, 0) + 1
        n = seen[label]
        if n == 1:
            options.append(discord.SelectOption(label=f"{label} — {games} games", value=key, description=f"Adds {games} games"))
        else:
            options.append(discord.SelectOption(label=f"{label} (×{n}) — +{games} games", value=key, description=f"Stack: +{games} games"))
    for key, label in SPECIAL_SUSPENSION_REASONS.items():
        options.append(discord.SelectOption(label=label, value=key, description="Status only — no games added"))
    return options[:25]


class SuspensionReasonSelect(discord.ui.Select):
    def __init__(self):
        opts = _make_suspension_options()
        super().__init__(placeholder=f"Select up to {MAX_SUSPENSION_REASONS} reasons...",
                         min_values=1, max_values=min(MAX_SUSPENSION_REASONS, len(opts)), options=opts)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        view.selected_reasons = self.values
        total, rl, sl = _build_suspension_summary(view.selected_reasons)
        preview = f"**Suspension preview — {view.target.display_name}**\n\n"
        if rl: preview += rl + "\n\n"
        if sl: preview += sl + "\n\n"
        preview += f"**Total: {total} games**\n\nClick **Confirm & Post** to publish."
        await interaction.response.edit_message(content=preview, view=view)


class SuspensionConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Confirm & Post", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        if not view.selected_reasons:
            await interaction.response.send_message("❌ Select at least one reason.", ephemeral=True); return

        total, rl, sl = _build_suspension_summary(view.selected_reasons)
        embed = discord.Embed(title="🚫 player suspension", color=0xED4245)
        embed.add_field(name="Player", value=f"<@{view.target.id}> ({view.target.display_name})", inline=False)
        if rl: embed.add_field(name="Reason(s)", value=rl, inline=False)
        if sl: embed.add_field(name="Additional Status", value=sl, inline=False)
        embed.add_field(name="Total Games Suspended", value=f"**{total} games**", inline=False)
        if view.target.avatar: embed.set_thumbnail(url=view.target.avatar.url)
        embed.set_footer(text=f"Issued by {interaction.user.display_name} | {UFF_FOOTER}")
        embed.timestamp = datetime.utcnow()

        ch = await get_suspension_channel(interaction.guild)
        for item in view.children: item.disabled = True

        if ch:
            await ch.send(embed=embed)
            await interaction.response.edit_message(content=f"✅ Posted to {ch.mention}.", view=view)
            nk = [r for r in view.selected_reasons if r in SUSPENSION_REASONS]
            sk = [r for r in view.selected_reasons if r in SPECIAL_SUSPENSION_REASONS]
            data = await load_data()
            data.setdefault("suspensions", []).append({
                "player_id": str(view.target.id), "player_name": view.target.display_name,
                "reason_keys": nk, "reasons": [_base_label(r) for r in nk],
                "status_flags": [SPECIAL_SUSPENSION_REASONS[r] for r in sk],
                "total_games": total, "issued_by": str(interaction.user.id),
                "issued_by_name": interaction.user.display_name,
                "date": datetime.utcnow().isoformat(), "cleared": False,
            })
            await save_data(data)
        else:
            await interaction.response.edit_message(content="❌ Suspension channel not found.", view=view)


class SuspensionCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        for item in view.children: item.disabled = True
        await interaction.response.edit_message(content="❌ Cancelled.", view=view)
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
    print(f"   Transactions Channel : {TRANSACTIONS_CHANNEL_ID}")
    print(f"   Bloxlink API         : {'SET' if BLOXLINK_API_KEY else 'NOT SET'}")
    print(f"   Zevora Logo          : {'SET' if ZEVORA_LOGO_URL else 'NOT SET'}")
    print(f"   Database             : {'SET' if DATABASE_URL else 'NOT SET'}")


# ═════════════════════════════════════════════════════════════════════
# TRANSACTION COMMANDS
# ═════════════════════════════════════════════════════════════════════

@bot.tree.command(name="set_team", description="[Staff] Register a Discord role as a UFF team")
@app_commands.describe(team_role="The team's Discord role (name + icon pulled automatically)")
@app_commands.default_permissions(administrator=True)
async def set_team(interaction: discord.Interaction, team_role: discord.Role):
    if not is_staff(interaction):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True); return

    team_name = team_role.name
    icon      = team_role.display_icon
    logo_url  = ""
    if icon is not None:
        logo_url = str(icon.url) if hasattr(icon, "url") else ""

    data = await load_data()
    rid  = str(team_role.id)
    ex   = data["teams"].get(rid, {})
    data["teams"][rid] = {
        "name":    team_name,
        "role_id": rid,
        "head_coach_id":     ex.get("head_coach_id"),
        "head_coach_name":   ex.get("head_coach_name"),
        "head_coach_roblox": ex.get("head_coach_roblox", ""),
        "ahc_id":            ex.get("ahc_id"),
        "ahc_name":          ex.get("ahc_name"),
        "ahc_roblox":        ex.get("ahc_roblox", ""),
        "logo_url":          logo_url or ex.get("logo_url", ""),
        "roster":            ex.get("roster", []),
        "color":             team_role.color.value or UFF_COLOR,
    }
    await save_data(data)

    embed = discord.Embed(title="team registered", color=UFF_COLOR,
        description=f"**{team_name}** registered!\nRole: {team_role.mention} | Transactions → <#{TRANSACTIONS_CHANNEL_ID}>")
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    else:
        embed.description += "\n\n⚠️ No role icon found. Use `/set_team_image` to set a logo."
    embed.set_footer(text=UFF_FOOTER)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="set_team_image", description="[Staff] Set or override the team logo URL")
@app_commands.describe(team_role="The team's Discord role", logo_url="Direct image URL for the team logo")
@app_commands.default_permissions(administrator=True)
async def set_team_image(interaction: discord.Interaction, team_role: discord.Role, logo_url: str):
    if not is_staff(interaction):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
    data = await load_data()
    rid  = str(team_role.id)
    if rid not in data["teams"]:
        await interaction.response.send_message(f"❌ {team_role.mention} not registered. Use `/set_team` first.", ephemeral=True); return
    data["teams"][rid]["logo_url"] = logo_url
    await save_data(data)
    embed = discord.Embed(title="logo updated", color=UFF_COLOR,
        description=f"Logo for **{data['teams'][rid]['name']}** updated.")
    embed.set_thumbnail(url=logo_url)
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
        await interaction.response.send_message(f"❌ {team_role.mention} not registered.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    blox        = await bloxlink_lookup(player.id, interaction.guild.id)
    roblox_name = blox.get("roblox_username", "")

    team = data["teams"][rid]
    team.update(head_coach_id=str(player.id), head_coach_name=player.name, head_coach_roblox=roblox_name)

    roster = team.setdefault("roster", [])
    if str(player.id) not in [r["id"] for r in roster]:
        roster.append({"id": str(player.id), "name": player.display_name, "roblox": roblox_name, "role": "Head Coach"})
    else:
        for r in roster:
            if r["id"] == str(player.id): r["role"] = "Head Coach"
    await save_data(data)

    hc_role     = interaction.guild.get_role(HEAD_COACH_ROLE_ID)
    role_failed = False
    if hc_role:
        try:
            await player.add_roles(hc_role, reason="Assigned as HC via /assign_hc")
        except discord.Forbidden:
            role_failed = True

    msg = f"✅ **{player.display_name}** is now head coach of **{team['name']}**."
    if role_failed: msg += _role_failure_note("Head Coach")
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="offer", description="Send a player a roster offer via DM (12h window)")
@app_commands.describe(player="The player to offer a roster spot to")
async def offer(interaction: discord.Interaction, player: discord.Member):
    data      = await load_data()
    rid, team = get_team_for_user(data, interaction.user.id)

    if not team:
        msg = "❌ Staff: assign yourself as HC first." if is_staff(interaction) else "❌ You're not HC or AHC of any team."
        await interaction.response.send_message(msg, ephemeral=True); return

    if player.id == interaction.user.id:
        await interaction.response.send_message("❌ Can't offer yourself.", ephemeral=True); return
    if player.bot:
        await interaction.response.send_message("❌ Can't offer a bot.", ephemeral=True); return

    roster = team.setdefault("roster", [])
    if len(roster) >= MAX_ROSTER:
        await interaction.response.send_message(f"❌ Roster cap reached ({MAX_ROSTER}).", ephemeral=True); return
    if str(player.id) in [r["id"] for r in roster]:
        await interaction.response.send_message(f"❌ {player.display_name} is already on the team.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)

    offer_id   = f"offer_{rid}_{player.id}_{int(datetime.utcnow().timestamp())}"
    team_logo  = team.get("logo_url", "")
    team_color = team.get("color", UFF_COLOR)
    hc_id      = team.get("head_coach_id")

    data.setdefault("offers", {})[offer_id] = {
        "team_role_id": rid, "team_name": team["name"], "player_id": str(player.id),
        "hc_id": hc_id, "timestamp": datetime.utcnow().isoformat(), "guild_id": str(interaction.guild.id),
    }
    await save_data(data)

    hc_rbx = team.get("head_coach_roblox", "")
    hc_line = f"<@{hc_id}> `{hc_rbx}`".strip() if hc_id else "*vacant*"

    dm = discord.Embed(title=f"offer from the {team['name']}",
        description=f"You've been offered a roster spot on **{team['name']}**!", color=team_color)
    dm.add_field(name="head coach:", value=hc_line, inline=False)
    dm.add_field(name="\u200b", value="You have **12 hours** to accept or ignore.", inline=False)
    thumb = team_logo or _best_thumbnail(interaction.guild)
    if thumb: dm.set_thumbnail(url=thumb)
    dm.set_footer(text=UFF_FOOTER)
    dm.timestamp = datetime.utcnow()

    view = OfferView(offer_id=offer_id, team_role_id=int(rid), team_name=team["name"],
                     team_logo=team_logo, hc_id=hc_id, player_id=player.id, guild_id=interaction.guild.id)
    try:
        await player.send(embed=dm, view=view)
    except discord.Forbidden:
        data.get("offers", {}).pop(offer_id, None); await save_data(data)
        await interaction.followup.send(f"❌ Could not DM **{player.display_name}**.", ephemeral=True); return

    await interaction.followup.send(f"✅ Offer sent to **{player.display_name}** — 12 hours to accept.", ephemeral=True)


@bot.tree.command(name="release", description="Release a player from your team")
@app_commands.describe(player="The player to release")
async def release(interaction: discord.Interaction, player: discord.Member):
    data      = await load_data()
    rid, team = get_team_for_user(data, interaction.user.id)

    if not team and not is_staff(interaction):
        await interaction.response.send_message("❌ You're not HC or AHC of any team.", ephemeral=True); return

    if not team and is_staff(interaction):
        for r, t in data["teams"].items():
            if str(player.id) in [x["id"] for x in t.get("roster", [])]:
                rid, team = r, t; break
        if not team:
            await interaction.response.send_message(f"❌ {player.display_name} isn't on any registered team.", ephemeral=True); return

    roster = team.setdefault("roster", [])
    before = len(roster)
    team["roster"] = [r for r in roster if r["id"] != str(player.id)]
    if len(team["roster"]) == before:
        await interaction.response.send_message(f"❌ {player.display_name} isn't on **{team['name']}**.", ephemeral=True); return

    await save_data(data)
    await interaction.response.defer(ephemeral=True, thinking=True)

    team_role   = get_team_role(interaction.guild, rid)
    role_failed = False
    if team_role and team_role in player.roles:
        try:
            await player.remove_roles(team_role, reason=f"Released from {team['name']}")
        except discord.Forbidden:
            role_failed = True

    embed = await build_transaction_embed("released", player, team, team_role, interaction.guild, color=0xED4245)
    msg   = f"✅ Released **{player.display_name}** from **{team['name']}**."
    if role_failed: msg += _role_failure_note(team["name"])
    await post_transaction(interaction.guild, embed, followup=interaction.followup, ephemeral_msg=msg)


@bot.tree.command(name="demand", description="Demand a release from your current team (1 per player lifetime)")
async def demand(interaction: discord.Interaction):
    data = await load_data()
    uid  = str(interaction.user.id)

    found_rid, found_team = None, None
    for r, t in data["teams"].items():
        if uid in [x["id"] for x in t.get("roster", [])]:
            found_rid, found_team = r, t; break

    if not found_team:
        await interaction.response.send_message("❌ You aren't on any registered team.", ephemeral=True); return

    extra_tokens = data.get("extra_demands", {}).get(uid, 0)
    if data.get("demand_used", {}).get(uid, False) and extra_tokens <= 0:
        await interaction.response.send_message(
            "❌ You've already used your demand release. Players are granted **1 demand** only.\n"
            "Ask a league admin to grant an extra with `/grant_extra_demand`.", ephemeral=True); return

    if data.get("demand_used", {}).get(uid, False):
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

    role_failed = False
    if team_role and team_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(team_role, reason=f"Demand release from {found_team['name']}")
        except discord.Forbidden:
            role_failed = True

    role_str = team_role.mention if team_role else f"**{found_team['name']}**"
    embed = discord.Embed(
        description=(
            f"{interaction.user.mention} (@{interaction.user.name}) `{roblox_name}` "
            f"has demanded a release from {role_str}!\n\n"
            f"{_info_block_lines(found_team)}"
        ),
        color=0xED4245,
    )
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    if roblox_avatar:
        embed.set_thumbnail(url=roblox_avatar)
    else:
        logo = found_team.get("logo_url", "") or _best_thumbnail(interaction.guild)
        if logo: embed.set_thumbnail(url=logo)
    if found_team.get("logo_url"):
        embed.set_image(url=found_team["logo_url"])

    msg = f"✅ Your demand from **{found_team['name']}** has been posted."
    if role_failed: msg += _role_failure_note(found_team["name"])
    await post_transaction(interaction.guild, embed, followup=interaction.followup, ephemeral_msg=msg)


@bot.tree.command(name="grant_extra_demand", description="[Owner] Grant a player an extra demand token")
@app_commands.describe(player="The player", amount="Number of extra demands (default 1)")
async def grant_extra_demand(interaction: discord.Interaction, player: discord.Member, amount: int = 1):
    if interaction.user.id not in EXTRA_DEMAND_GRANT_USER_IDS and not is_admin(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    data = await load_data()
    uid  = str(player.id)
    data.setdefault("extra_demands", {})
    data["extra_demands"][uid] = data["extra_demands"].get(uid, 0) + amount
    await save_data(data)
    await interaction.response.send_message(
        f"✅ Granted **{amount}** extra demand(s) to **{player.display_name}**. "
        f"Total banked: **{data['extra_demands'][uid]}**.", ephemeral=True)


@bot.tree.command(name="promote_coach", description="Promote a player to Assistant Head Coach")
@app_commands.describe(player="The player to promote")
async def promote_coach(interaction: discord.Interaction, player: discord.Member):
    data      = await load_data()
    rid, team = get_team_for_hc(data, interaction.user.id)

    if not team and is_staff(interaction):
        for r, t in data["teams"].items():
            if str(player.id) in [x["id"] for x in t.get("roster", [])]:
                rid, team = r, t; break

    if not team:
        await interaction.response.send_message("❌ You're not HC of any team. Only HCs and staff can promote.", ephemeral=True); return

    roster = team.setdefault("roster", [])
    if str(player.id) not in [r["id"] for r in roster]:
        await interaction.response.send_message(f"❌ {player.display_name} must be on the roster first.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    blox        = await bloxlink_lookup(player.id, interaction.guild.id)
    roblox_name = blox.get("roblox_username", "")

    team.update(ahc_id=str(player.id), ahc_name=player.name, ahc_roblox=roblox_name)
    for r in roster:
        if r["id"] == str(player.id): r["role"] = "Assistant Head Coach"
    await save_data(data)

    ahc_role    = interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID)
    hc_role     = interaction.guild.get_role(HEAD_COACH_ROLE_ID)
    role_failed = False
    try:
        if ahc_role: await player.add_roles(ahc_role, reason="Promoted to AHC")
        else: role_failed = True
        if hc_role and hc_role in player.roles: await player.remove_roles(hc_role, reason="Promoted to AHC")
    except discord.Forbidden:
        role_failed = True

    team_role = get_team_role(interaction.guild, rid)
    embed = await build_coach_embed("assistant coach promotion", player, team, team_role,
                                    interaction.guild, color=team.get("color", UFF_COLOR))
    msg = f"✅ **{player.display_name}** promoted to AHC of **{team['name']}**."
    if role_failed: msg += _role_failure_note("Assistant Coach")
    await post_transaction(interaction.guild, embed, followup=interaction.followup, ephemeral_msg=msg)


@bot.tree.command(name="demote_coach", description="Demote the Assistant Head Coach back to player")
@app_commands.describe(player="The AHC to demote")
async def demote_coach(interaction: discord.Interaction, player: discord.Member):
    data      = await load_data()
    rid, team = get_team_for_hc(data, interaction.user.id)

    if not team and is_staff(interaction):
        for r, t in data["teams"].items():
            if t.get("ahc_id") == str(player.id):
                rid, team = r, t; break

    if not team:
        await interaction.response.send_message("❌ You're not HC of any team.", ephemeral=True); return
    if team.get("ahc_id") != str(player.id):
        await interaction.response.send_message(f"❌ {player.display_name} is not the AHC of **{team['name']}**.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    team.update(ahc_id=None, ahc_name=None, ahc_roblox="")
    for r in team.get("roster", []):
        if r["id"] == str(player.id): r["role"] = "Player"
    await save_data(data)

    ahc_role    = interaction.guild.get_role(ASSISTANT_COACH_ROLE_ID)
    role_failed = False
    try:
        if ahc_role and ahc_role in player.roles:
            await player.remove_roles(ahc_role, reason="Demoted from AHC")
    except discord.Forbidden:
        role_failed = True

    team_role = get_team_role(interaction.guild, rid)
    embed = await build_coach_embed("assistant coach demotion", player, team, team_role,
                                    interaction.guild, color=0xED4245)
    msg = f"✅ **{player.display_name}** demoted from AHC of **{team['name']}**."
    if role_failed: msg += _role_failure_note("Assistant Coach")
    await post_transaction(interaction.guild, embed, followup=interaction.followup, ephemeral_msg=msg)


@bot.tree.command(name="disband", description="Disband a team — removes all players and coaches")
@app_commands.describe(confirm="Type DISBAND to confirm",
                        team_role="(Staff only) Target team — HCs don't need this")
async def disband(interaction: discord.Interaction, confirm: str, team_role: discord.Role = None):
    if confirm.upper() != "DISBAND":
        await interaction.response.send_message("❌ Type `DISBAND` exactly.", ephemeral=True); return

    data      = await load_data()
    rid, team = get_team_for_hc(data, interaction.user.id)

    if not team and is_staff(interaction) and team_role:
        rid  = str(team_role.id)
        team = get_team_by_role(data, team_role.id)

    if not team:
        await interaction.response.send_message("❌ Not HC of any team. Staff must also provide `team_role`.", ephemeral=True); return

    team_name     = team["name"]
    former_roster = list(team.get("roster", []))
    team.update(roster=[], head_coach_id=None, head_coach_name=None, head_coach_roblox="",
                ahc_id=None, ahc_name=None, ahc_roblox="")
    await save_data(data)

    tr = get_team_role(interaction.guild, rid)
    fail_count = 0
    if tr:
        for md in former_roster:
            try:
                m = interaction.guild.get_member(int(md["id"])) or await interaction.guild.fetch_member(int(md["id"]))
                if tr in m.roles:
                    await m.remove_roles(tr, reason=f"{team_name} disbanded")
            except discord.Forbidden:
                fail_count += 1
            except (discord.NotFound, discord.HTTPException):
                pass

    embed = discord.Embed(title="team disbanded", color=0xED4245,
        description=f"**{team_name}** has been disbanded.\nAll **{len(former_roster)}** players and coaches removed.")
    if tr: embed.add_field(name="Team", value=tr.mention, inline=True)
    logo = team.get("logo_url", "") or _best_thumbnail(interaction.guild)
    if logo: embed.set_thumbnail(url=logo)
    embed.set_footer(text=f"Disbanded by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp = datetime.utcnow()

    note = f"\n⚠️ Couldn't remove team role from {fail_count} member(s). Check bot role position." if fail_count else ""

    ch = await get_transactions_channel(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        await interaction.response.send_message(f"✅ **{team_name}** disbanded. Posted to {ch.mention}.{note}", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ **{team_name}** disbanded. (No transactions channel set.){note}", ephemeral=True)
        await interaction.channel.send(embed=embed)


@bot.tree.command(name="roster", description="View a team's current roster")
@app_commands.describe(team_role="The team's Discord role")
async def roster_cmd(interaction: discord.Interaction, team_role: discord.Role):
    data = await load_data()
    team = get_team_by_role(data, team_role.id)
    if not team:
        await interaction.response.send_message(f"❌ {team_role.mention} isn't registered.", ephemeral=True); return

    roster = team.get("roster", [])
    color  = team.get("color", UFF_COLOR)
    embed  = discord.Embed(title=f"{team['name'].lower()} roster",
                           description=f"> roster: **{len(roster)}/{MAX_ROSTER}**", color=color)

    hc_id = team.get("head_coach_id")
    if hc_id:
        rbx = f"`{team.get('head_coach_roblox', '')}`" if team.get("head_coach_roblox") else ""
        embed.add_field(name="head coach:",
            value=f"> <@{hc_id}> (@{team.get('head_coach_name', '')}) ✓ {rbx}".strip(), inline=False)

    ahc_lines, pl_lines = [], []
    for r in roster:
        rbx  = f"`{r['roblox']}`" if r.get("roblox") else ""
        line = f"> <@{r['id']}> (@{r['name']}) ✓ {rbx}".strip()
        role = r.get("role", "Player")
        if role == "Assistant Head Coach": ahc_lines.append(line)
        elif role != "Head Coach":         pl_lines.append(line)

    if ahc_lines: embed.add_field(name="assistant head coach:", value="\n".join(ahc_lines), inline=False)
    if pl_lines:  embed.add_field(name="players:", value="\n".join(pl_lines), inline=False)
    elif not roster: embed.add_field(name="players:", value="> *No players yet.*", inline=False)

    logo = team.get("logo_url", "") or _best_thumbnail(interaction.guild)
    if logo: embed.set_thumbnail(url=logo)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
                rbx_str = f"`{hc_rbx}`" if hc_rbx else ""
                hc_str  = f"<@{hc_id}> (@{hc_name}) ✓ {rbx_str}".strip()
            else:
                hc_str = "*vacant*"
            tr          = get_team_role(interaction.guild, rid)
            role_mention = tr.mention if tr else team["name"]
            lines.append(f"{role_mention} — {hc_str}")

        # Single vertical list chunked to stay under Discord's 1024-char field limit
        chunks, cur = [], ""
        for line in lines:
            candidate = f"{cur}\n{line}" if cur else line
            if len(candidate) > 1000:
                chunks.append(cur); cur = line
            else:
                cur = candidate
        if cur: chunks.append(cur)
        for chunk in (chunks or ["*None*"]):
            embed.add_field(name="\u200b", value=chunk, inline=False)

    # Always use the league logo (Zevora) for the /coaches embed
    thumb = ZEVORA_LOGO_URL or UFF_THUMBNAIL or (_best_thumbnail(interaction.guild))
    if thumb: embed.set_thumbnail(url=thumb)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═════════════════════════════════════════════════════════════════════
# PICKUP COMMANDS
# ═════════════════════════════════════════════════════════════════════

@bot.tree.command(name="pickup_ranked", description="Challenge another player to a ranked UFF pickup")
@app_commands.describe(opponent="The player you want to challenge", game_link="Roblox game link",
                        your_team="Your team name", opponent_team="Opponent's team name")
async def pickup_ranked(interaction: discord.Interaction, opponent: discord.Member,
                        game_link: str, your_team: str, opponent_team: str):
    if not ({r.id for r in interaction.user.roles} & PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.response.send_message("❌ You don't have the required role.", ephemeral=True); return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ Can't challenge yourself!", ephemeral=True); return
    if opponent.bot:
        await interaction.response.send_message("❌ Can't challenge a bot!", ephemeral=True); return

    data = await load_data()
    cd, remaining = on_cooldown(data, interaction.user.id)
    if cd:
        e = discord.Embed(title="⏳ cooldown active",
            description=f"Challenge again in **{remaining}**. Cooldown: `{COOLDOWN_MINUTES} min`.", color=0xE84040)
        e.set_footer(text=UFF_FOOTER)
        await interaction.response.send_message(embed=e, ephemeral=True); return

    p1 = get_player(data, interaction.user.id); p1["username"] = interaction.user.display_name
    p2 = get_player(data, opponent.id);         p2["username"] = opponent.display_name

    match_id = f"{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("pending", {})[match_id] = {
        "challenger_id": str(interaction.user.id), "opponent_id": str(opponent.id),
        "challenger_name": interaction.user.display_name, "opponent_name": opponent.display_name,
        "challenger_team": your_team, "opponent_team": opponent_team, "game_link": game_link,
        "timestamp": datetime.utcnow().isoformat(), "match_id": match_id, "guild_id": interaction.guild.id
    }
    await save_data(data)

    r1, e1, _ = get_rank(p1["elo"]); r2, e2, _ = get_rank(p2["elo"])

    dm = discord.Embed(title="🏈 You've Been Challenged to a Ranked Pickup!",
        description=f"**{interaction.user.display_name}** wants a ranked pickup. Expires in **30 minutes**.",
        color=UFF_COLOR)
    dm.add_field(name=f"🟡 {interaction.user.display_name}",
        value=f"`{interaction.user.name}`\n**{your_team}**\nRank: `{e1} {r1}`", inline=True)
    dm.add_field(name="\u200b", value="**— VS —**", inline=True)
    dm.add_field(name=f"🔵 {opponent.display_name}",
        value=f"`{opponent.name}`\n**{opponent_team}**\nRank: `{e2} {r2}`", inline=True)
    dm.add_field(name="🔗 Game Link", value=f"[Click here to join →]({game_link})", inline=False)
    if UFF_BANNER: dm.set_image(url=UFF_BANNER)
    thumb = _best_thumbnail(interaction.guild)
    if thumb: dm.set_thumbnail(url=thumb)
    dm.set_footer(text=f"Challenge by {interaction.user.display_name} | {UFF_FOOTER}")
    dm.timestamp = datetime.utcnow()

    view = RankedPickupView(match_id=match_id, challenger_id=interaction.user.id, opponent_id=opponent.id,
        challenger_name=interaction.user.display_name, opponent_name=opponent.display_name,
        challenger_team=your_team, opponent_team=opponent_team, game_link=game_link, guild_id=interaction.guild.id)
    try:
        await opponent.send(embed=dm, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Could not DM **{opponent.display_name}**.", ephemeral=True)
        data.get("pending", {}).pop(match_id, None); await save_data(data); return

    ack = discord.Embed(title="📨 Challenge Sent!",
        description=f"Challenge sent to **{opponent.display_name}** via DM. Posted publicly only if they accept.",
        color=0x57F287)
    ack.set_footer(text=f"{UFF_FOOTER} • 30-minute window")
    await interaction.response.send_message(embed=ack, ephemeral=True)


@bot.tree.command(name="pickup_casual", description="Challenge to a casual pickup — no ELO changes")
@app_commands.describe(opponent="Player to challenge", game_link="Roblox game link",
                        your_team="Your team", opponent_team="Opponent's team")
async def pickup_casual(interaction, opponent: discord.Member, game_link: str, your_team: str, opponent_team: str):
    await _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team)


@bot.tree.command(name="casual_pickup", description="Challenge to a casual pickup — no ELO changes")
@app_commands.describe(opponent="Player to challenge", game_link="Roblox game link",
                        your_team="Your team", opponent_team="Opponent's team")
async def casual_pickup(interaction, opponent: discord.Member, game_link: str, your_team: str, opponent_team: str):
    await _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team)


@bot.tree.command(name="pickup_results", description="Submit ranked pickup results + screenshot")
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
        await interaction.response.send_message("❌ No pending ranked pickup. Use `/pickup_ranked` first.", ephemeral=True); return

    c_id = int(match["challenger_id"]); o_id = int(match["opponent_id"])
    if winner.id not in [c_id, o_id]:
        await interaction.response.send_message("❌ Winner must be one of the two players.", ephemeral=True); return

    loser_id   = o_id if winner.id == c_id else c_id
    loser_name = match["opponent_name"] if winner.id == c_id else match["challenger_name"]

    wp = get_player(data, winner.id); lp = get_player(data, loser_id)
    wp["username"] = winner.display_name
    old_w, old_l   = wp["elo"], lp["elo"]
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
    data.get("pending", {}).pop(match_key, None)
    await save_data(data)

    wr, we, wcolor = get_rank(wp["elo"]); lr, le, _ = get_rank(lp["elo"])
    embed = discord.Embed(title="🏆 pickup results", color=wcolor)
    embed.add_field(name="🏆 Winner",
        value=f"<@{winner.id}> **{winner.display_name}**\n> Score: **{winner_score}**\n> ELO: `{old_w}` → `{wp['elo']}` **(+{WIN_ELO})**\n> Rank: `{we} {wr}`",
        inline=True)
    embed.add_field(name="❌ Loser",
        value=f"<@{loser_id}> **{loser_name}**\n> Score: **{loser_score}**\n> ELO: `{old_l}` → `{lp['elo']}` **(-{LOSS_ELO})**\n> Rank: `{le} {lr}`",
        inline=True)
    embed.add_field(name="📊 Final Score",
        value=f"**{winner.display_name}** `{winner_score} — {loser_score}` **{loser_name}**", inline=False)
    embed.set_image(url=screenshot.url)
    thumb = _best_thumbnail(interaction.guild)
    if thumb: embed.set_thumbnail(url=thumb)
    embed.set_footer(text=f"{UFF_FOOTER} • Submitted by {interaction.user.display_name}")
    embed.timestamp = datetime.utcnow()

    ch = await get_pickup_channel(interaction.guild)
    target_ch = ch if (ch and ch.id != interaction.channel_id) else interaction.channel
    await interaction.response.send_message("✅ Results posted!", ephemeral=True)
    await target_ch.send(embed=embed)


@bot.tree.command(name="pickup_profile", description="View UFF pickup rank and stats")
@app_commands.describe(player="Player to look up (blank = yourself)")
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
        await interaction.response.send_message("No players yet.", ephemeral=True); return
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
    embed.set_footer(text=UFF_FOOTER); embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="match_history", description="View recent UFF pickup results")
async def match_history(interaction: discord.Interaction):
    data    = await load_data()
    matches = list(reversed(data.get("matches", [])))[:10]
    if not matches:
        await interaction.response.send_message("No matches yet.", ephemeral=True); return
    lines = [f"🏆 **{m['winner_name']}** `{m.get('winner_score','?')}–{m.get('loser_score','?')}` {m['loser_name']}" for m in matches]
    embed = discord.Embed(title="📋 UFF Pickup — Recent Results", description="\n".join(lines), color=0x4090E8)
    embed.set_footer(text=f"{UFF_FOOTER} • Last 10 matches"); embed.timestamp = datetime.utcnow()
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
    data["players"][str(player.id)] = {"elo": STARTING_ELO, "wins": 0, "losses": 0, "last_game": None, "username": player.display_name}
    await save_data(data)
    await interaction.response.send_message(f"✅ Reset **{player.display_name}** ELO to `{STARTING_ELO}`.", ephemeral=True)


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
    await interaction.response.send_message(f"✅ **{player.display_name}** ELO: `{old}` → `{p['elo']}` ({sign}{amount})", ephemeral=True)


@bot.tree.command(name="clear_cooldown", description="[Admin] Clear a player's cooldown")
@app_commands.describe(player="Player to clear")
@app_commands.default_permissions(administrator=True)
async def clear_cooldown(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True); return
    data = await load_data(); get_player(data, player.id)["last_game"] = None; await save_data(data)
    await interaction.response.send_message(f"✅ Cleared cooldown for **{player.display_name}**.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# SUSPENSION COMMANDS
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="suspension", description="[Staff] Issue a suspension to a player")
@app_commands.describe(player="The player to suspend")
async def suspension(interaction: discord.Interaction, player: discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message("❌ No permission to issue suspensions.", ephemeral=True); return
    view = SuspensionView(target=player, issuer_id=interaction.user.id)
    await interaction.response.send_message(
        content=f"**Issuing suspension for {player.display_name}**\nSelect up to {MAX_SUSPENSION_REASONS} reasons.",
        view=view, ephemeral=True)


@bot.tree.command(name="unsuspend", description="[Staff] Clear a player's suspension")
@app_commands.describe(player="The player to unsuspend")
async def unsuspend(interaction: discord.Interaction, player: discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message("❌ No permission to clear suspensions.", ephemeral=True); return
    data = await load_data(); cleared_any = False
    for s in data.get("suspensions", []):
        if s.get("player_id") == str(player.id) and not s.get("cleared", False):
            s.update(cleared=True, cleared_by=str(interaction.user.id),
                     cleared_by_name=interaction.user.display_name,
                     cleared_date=datetime.utcnow().isoformat())
            cleared_any = True
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
        note = "" if cleared_any else "\n*(No open records found, but notice posted anyway.)*"
        await interaction.response.send_message(f"✅ {player.display_name} unsuspended. Posted to {ch.mention}.{note}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Suspension channel not found.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /help_uff
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="help_uff", description="UFF bot command guide")
async def help_uff(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 United Flag Football — Commands", color=UFF_COLOR)
    embed.add_field(name="📋 Transactions", value=(
        "`/set_team` — [Staff] Register a team role\n"
        "`/set_team_image` — [Staff] Set team logo URL\n"
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
    ), inline=False)
    embed.add_field(name="⚔️ Ranked Pickup", value=(
        "`/pickup_ranked` — Start a ranked pickup\n"
        "`/pickup_results` — Submit results + screenshot\n"
        "`/pickup_profile` — ELO, rank & stats\n"
        "`/pickup_leaderboard` — Top 15 ELO rankings\n"
        "`/match_history` — Last 10 results"
    ), inline=False)
    embed.add_field(name="🎮 Casual", value="`/pickup_casual` or `/casual_pickup`", inline=False)
    embed.add_field(name="🏟️ League", value="`/teams` — All 20 UFF teams", inline=False)
    embed.add_field(name="🛡️ Admin", value="`/reset_player` · `/adjust_elo` · `/clear_cooldown`", inline=False)
    embed.add_field(name="🚫 Suspensions", value="`/suspension` · `/unsuspend`", inline=False)
    embed.add_field(name="📊 Ranks", value=(
        "**Start:** 900 ELO | **Win:** +100 | **Loss:** −100\n"
        "⚙️ Iron I/II/III → 0/700/900\n"
        "🥇 Gold I/II/III → 1100/1300/1500\n"
        "💎 Amethyst I/II/III → 1700/1900/2100"
    ), inline=False)
    apply_branding(embed)
    embed.set_footer(text=f"{UFF_FOOTER} • {COOLDOWN_MINUTES}-min ranked cooldown")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
