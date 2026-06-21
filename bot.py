"""
UFF Discord Bot — Python
United Flag Football League — Pickup Ranked System

FLOW:
  1. /pickup_ranked → challenger selects opponent + teams + game link
  2. Bot DMs the opponent with Accept / Decline buttons (NO public post yet)
  3. Opponent clicks:
       Decline → opponent DM updated, challenger gets a DM saying declined. Done.
       Accept  → public matchup embed posts to pickup channel with a ping
  4. /pickup_results → submit screenshot + winner → ELO updated, results embed posted

  /pickup_casual or /casual_pickup → same flow as ranked but no ELO, just a matchup post

  /suspension → staff-only. Pick a player + up to 5 reasons (stackable — same
  reason can be picked multiple times via x1/x2/x3 variants). Games add up
  automatically. A suspension notice posts to the suspension channel.

  /unsuspend → staff-only. Pick a player, post a clearance notice to the
  suspension channel and mark any open suspensions for that player as cleared.
"""

import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter
import json
import os
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────────
# ENVIRONMENT VARIABLES  (set these in Railway)
# ─────────────────────────────────────────────────────────────────────
TOKEN          = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID       = int(os.getenv("OWNER_DISCORD_ID", "0"))
SECRET_KEY     = os.getenv("SECRET_KEY", "")
QBB_CHANNEL_ID = int(os.getenv("QBB_CHANNEL_ID", "0"))

# Banner / logo — set in Railway env vars, no code changes needed to swap art
#   UFF_BANNER_URL    -> large image in matchup embed
#   UFF_THUMBNAIL_URL -> small icon top-right of embeds (logo)
UFF_THUMBNAIL  = os.getenv("UFF_THUMBNAIL_URL", "")
UFF_BANNER     = os.getenv("UFF_BANNER_URL",    "")

DATA_FILE = "uff_data.json"

UFF_FOOTER   = "United Flag Football League"
UFF_COLOR    = 0xF0C040
CASUAL_COLOR = 0x5865F2

GUILD_ID = 1262200419564785755  # UFF server — instant command sync

# ─────────────────────────────────────────────────────────────────────
# ELO CONFIG
# ─────────────────────────────────────────────────────────────────────
STARTING_ELO     = 900
WIN_ELO          = 100
LOSS_ELO         = 100
COOLDOWN_MINUTES = 30

# ─────────────────────────────────────────────────────────────────────
# ROLES ALLOWED TO START A PICKUP
# ─────────────────────────────────────────────────────────────────────
PICKUP_ALLOWED_ROLE_IDS = {
    1269693904815521994,  # QBB Captain
    1404271074623099040,  # Moderators
    1404271002241728617,  # League Boards
    1429344923865448550,  # Operations Director
    1262200419686285342,  # Commissioner
    1401450124424642561,  # Founder
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

# Each key maps to (base display label, games per instance).
# x1 / x2 / x3 variants let the same reason be stacked — Discord requires
# unique option values so we suffix them. The display strips the suffix.
SUSPENSION_REASONS = {
    # Exploiting (no stacking needed)
    "exploiting_x1":                ("Exploiting",                  24),

    # Dodging Screenshare (no stacking needed)
    "dodging_screenshare_x1":       ("Dodging Screenshare",          6),

    # Illegally Playing — stackable x4
    "illegally_playing_x1":         ("Illegally Playing",            2),
    "illegally_playing_x2":         ("Illegally Playing",            2),
    "illegally_playing_x3":         ("Illegally Playing",            2),
    "illegally_playing_x4":         ("Illegally Playing",            2),

    # Possession of Exploits — stackable x4
    "possession_of_exploits_x1":    ("Possession of Exploits",      12),
    "possession_of_exploits_x2":    ("Possession of Exploits",      12),
    "possession_of_exploits_x3":    ("Possession of Exploits",      12),
    "possession_of_exploits_x4":    ("Possession of Exploits",      12),

    # Gameplay Manipulation (no stacking needed)
    "gameplay_manipulation_x1":     ("Gameplay Manipulation",        8),

    # Alting — stackable x4
    "alting_x1":                    ("Alting",                      12),
    "alting_x2":                    ("Alting",                      12),
    "alting_x3":                    ("Alting",                      12),
    "alting_x4":                    ("Alting",                      12),

    # Disbanding (no stacking needed)
    "disbanding_x1":                ("Disbanding",                   4),

    # Distributing Exploits (no stacking needed)
    "distributing_exploits_x1":     ("Distributing Exploits",       40),

    # Distributing Alt Accounts (no stacking needed)
    "distributing_alts_x1":         ("Distributing Alt Accounts",   25),

    # Framing (no stacking needed)
    "framing_x1":                   ("Framing",                     12),

    # Obstruction of Justice (no stacking needed)
    "obstruction_of_justice_x1":    ("Obstruction of Justice",       8),
}

# Special reasons: no games added — applies a status label only
SPECIAL_SUSPENSION_REASONS = {
    "ineligible_until_ss": "Ineligible Until Screenshare",
}


def _base_label(key: str) -> str:
    """Strip the _x1/_x2/_x3 suffix to get the clean display label."""
    return SUSPENSION_REASONS[key][0]


def _build_suspension_summary(selected_reasons):
    """
    Returns (total_games, reason_lines_str, status_lines_str).
    Groups stacked selections of the same reason into one line with a count.
    """
    normal_keys  = [r for r in selected_reasons if r in SUSPENSION_REASONS]
    special_keys = [r for r in selected_reasons if r in SPECIAL_SUSPENSION_REASONS]

    total_games = sum(SUSPENSION_REASONS[r][1] for r in normal_keys)

    # Group by base label preserving insertion order
    label_games: dict[str, list[int]] = {}
    for r in normal_keys:
        label = _base_label(r)
        label_games.setdefault(label, []).append(SUSPENSION_REASONS[r][1])

    lines = []
    for label, game_list in label_games.items():
        count      = len(game_list)
        per_game   = game_list[0]
        subtotal   = per_game * count
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
# DATA HELPERS
# ─────────────────────────────────────────────────────────────────────
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"players": {}, "matches": [], "pending": {}, "casual_pending": {}, "suspensions": []}
    with open(DATA_FILE, "r") as f:
        data = json.load(f)
    data.setdefault("casual_pending", {})
    data.setdefault("suspensions", [])
    return data

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def get_player(data, user_id: int):
    uid = str(user_id)
    if uid not in data["players"]:
        data["players"][uid] = {
            "elo": STARTING_ELO,
            "wins": 0,
            "losses": 0,
            "last_game": None,
            "username": ""
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

def can_issue_suspension(interaction: discord.Interaction) -> bool:
    if interaction.user.id in SUSPENSION_ALLOWED_USER_IDS:
        return True
    user_role_ids = {role.id for role in interaction.user.roles}
    if user_role_ids & SUSPENSION_ALLOWED_ROLE_IDS:
        return True
    return is_admin(interaction)

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


# ─────────────────────────────────────────────────────────────────────
# EMBED BUILDERS
# ─────────────────────────────────────────────────────────────────────
def build_ranked_pickup_embed(
    challenger, opponent, your_team, opponent_team,
    game_link, match_id, guild, data, accepted=False
):
    p1 = get_player(data, challenger.id)
    p2 = get_player(data, opponent.id)
    e1, e2 = p1["elo"], p2["elo"]
    r1, emoji1, _ = get_rank(e1)
    r2, emoji2, _ = get_rank(e2)

    embed = discord.Embed(title="Ranked Pickup Matchup", color=UFF_COLOR)
    embed.add_field(
        name=f"🟡 {challenger.display_name}",
        value=f"<@{challenger.id}>\n**{your_team}**\nRank: `{emoji1} {r1}`",
        inline=True
    )
    embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    embed.add_field(
        name=f"🔵 {opponent.display_name}",
        value=f"<@{opponent.id}>\n**{opponent_team}**\nRank: `{emoji2} {r2}`",
        inline=True
    )
    embed.add_field(name="🔗 Game Link", value=f"[**Click here to join →**]({game_link})", inline=False)
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


def build_casual_pickup_embed(
    challenger, opponent, your_team, opponent_team,
    game_link, guild, accepted=False
):
    embed = discord.Embed(title="Casual Pickup Matchup", color=CASUAL_COLOR)
    embed.add_field(
        name=f"🟡 {challenger.display_name}",
        value=f"<@{challenger.id}>\n**{your_team}**",
        inline=True
    )
    embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    embed.add_field(
        name=f"🔵 {opponent.display_name}",
        value=f"<@{opponent.id}>\n**{opponent_team}**",
        inline=True
    )
    embed.add_field(name="🔗 Game Link", value=f"[**Click here to join →**]({game_link})", inline=False)
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


# ─────────────────────────────────────────────────────────────────────
# SHARED CASUAL LOGIC
# ─────────────────────────────────────────────────────────────────────
async def _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team):
    user_role_ids = {role.id for role in interaction.user.roles}
    if not (user_role_ids & PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.response.send_message(
            "❌ You don't have the required role to start a casual pickup.", ephemeral=True
        )
        return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't challenge yourself!", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("❌ Can't challenge a bot!", ephemeral=True)
        return

    data = load_data()
    match_id = f"casual_{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("casual_pending", {})[match_id] = {
        "challenger_id":   str(interaction.user.id),
        "opponent_id":     str(opponent.id),
        "challenger_name": interaction.user.display_name,
        "opponent_name":   opponent.display_name,
        "challenger_team": your_team,
        "opponent_team":   opponent_team,
        "game_link":       game_link,
        "timestamp":       datetime.utcnow().isoformat(),
        "match_id":        match_id,
        "guild_id":        interaction.guild.id
    }
    save_data(data)

    dm_embed = discord.Embed(
        title="🏈 You've Been Challenged to a Casual Pickup!",
        description=(
            f"**{interaction.user.display_name}** wants to play a casual (unranked) pickup against you.\n"
            f"Accept or decline below. This request expires in **30 minutes**."
        ),
        color=CASUAL_COLOR
    )
    dm_embed.add_field(
        name=f"🟡 {interaction.user.display_name}",
        value=f"`{interaction.user.name}`\n**{your_team}**",
        inline=True
    )
    dm_embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    dm_embed.add_field(
        name=f"🔵 {opponent.display_name}",
        value=f"`{opponent.name}`\n**{opponent_team}**",
        inline=True
    )
    dm_embed.add_field(name="🔗 Game Link", value=f"[**Click here to join →**]({game_link})", inline=False)
    dm_embed.add_field(name="\u200b", value="⚠️ This is **NOT** a ranked matchup — no ELO will be affected.", inline=False)
    if UFF_BANNER:
        dm_embed.set_image(url=UFF_BANNER)
    if UFF_THUMBNAIL:
        dm_embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild and interaction.guild.icon:
        dm_embed.set_thumbnail(url=interaction.guild.icon.url)
    dm_embed.set_footer(text=f"Casual pickup challenge issued by {interaction.user.display_name} | {UFF_FOOTER}")
    dm_embed.timestamp = datetime.utcnow()

    view = CasualPickupView(
        match_id=match_id,
        challenger_id=interaction.user.id,
        opponent_id=opponent.id,
        challenger_name=interaction.user.display_name,
        opponent_name=opponent.display_name,
        challenger_team=your_team,
        opponent_team=opponent_team,
        game_link=game_link,
        guild_id=interaction.guild.id,
    )

    try:
        await opponent.send(embed=dm_embed, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Could not DM **{opponent.display_name}** — they have DMs disabled.", ephemeral=True
        )
        if match_id in data.get("casual_pending", {}):
            del data["casual_pending"][match_id]
            save_data(data)
        return

    confirm_embed = discord.Embed(
        title="📨 Casual Pickup Sent!",
        description=(
            f"Your casual pickup challenge has been sent to **{opponent.display_name}** via DM.\n\n"
            f"The matchup will be posted publicly only **if they accept**.\n"
            f"No ELO changes will occur."
        ),
        color=0x57F287
    )
    confirm_embed.set_footer(text=f"{UFF_FOOTER} • 30-minute response window")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# RANKED PICKUP — ACCEPT / DECLINE VIEW
# ─────────────────────────────────────────────────────────────────────
class RankedPickupView(discord.ui.View):
    def __init__(
        self, match_id, challenger_id, opponent_id,
        challenger_name, opponent_name, challenger_team,
        opponent_team, game_link, guild_id
    ):
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
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True)
            return
        self.responded = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        data = load_data()
        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Could not find the server.", ephemeral=True)
            return
        try:
            challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent   = guild.get_member(self.opponent_id)   or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not find one of the players.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        embed = build_ranked_pickup_embed(
            challenger=challenger, opponent=opponent,
            your_team=self.challenger_team, opponent_team=self.opponent_team,
            game_link=self.game_link, match_id=self.match_id,
            guild=guild, data=data, accepted=True
        )
        header = (
            f"@everyone  **Ranked Pickup**\n"
            f"**{challenger.display_name}** vs **{opponent.display_name}** is hosting a ranked pickup!"
        )
        ch = await get_pickup_channel(guild)
        if ch:
            await ch.send(content=header, embed=embed)
        else:
            try:
                await challenger.send(content="⚠️ Pickup channel not set. Here's the matchup:", embed=embed)
            except discord.Forbidden:
                pass

        accepted_embed = discord.Embed(
            title="✅ Challenge Accepted!",
            description=(
                f"You accepted **{self.challenger_name}**'s challenge!\n\n"
                f"The matchup has been posted to the pickup channel.\n"
                f"🔗 [Join the game]({self.game_link})"
            ),
            color=0x57F287
        )
        accepted_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=accepted_embed, view=self)

        try:
            notify = discord.Embed(
                title="✅ Challenge Accepted!",
                description=(
                    f"**{self.opponent_name}** accepted your pickup challenge!\n\n"
                    f"The matchup has been posted to the pickup channel.\n"
                    f"🔗 [Join the game]({self.game_link})\n\n"
                    f"Use `/pickup_results` when the game is over."
                ),
                color=0x57F287
            )
            notify.set_footer(text=UFF_FOOTER)
            await challenger.send(embed=notify)
        except discord.Forbidden:
            pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True)
            return
        self.responded = True
        self.stop()
        for item in self.children:
            item.disabled = True

        data = load_data()
        if self.match_id in data.get("pending", {}):
            del data["pending"][self.match_id]
            save_data(data)

        declined_embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s pickup challenge.",
            color=0xED4245
        )
        declined_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=declined_embed, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild:
            try:
                challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                notify = discord.Embed(
                    title="❌ Challenge Declined",
                    description=(
                        f"**{self.opponent_name}** declined your pickup challenge.\n"
                        f"No match was recorded. You're free to challenge someone else!"
                    ),
                    color=0xED4245
                )
                notify.set_footer(text=UFF_FOOTER)
                await challenger.send(embed=notify)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                pass


# ─────────────────────────────────────────────────────────────────────
# CASUAL PICKUP — ACCEPT / DECLINE VIEW
# ─────────────────────────────────────────────────────────────────────
class CasualPickupView(discord.ui.View):
    def __init__(
        self, match_id, challenger_id, opponent_id,
        challenger_name, opponent_name, challenger_team,
        opponent_team, game_link, guild_id
    ):
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
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True)
            return
        self.responded = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Could not find the server.", ephemeral=True)
            return
        try:
            challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
            opponent   = guild.get_member(self.opponent_id)   or await guild.fetch_member(self.opponent_id)
        except discord.NotFound:
            await interaction.response.send_message("❌ Could not find one of the players.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        data = load_data()
        if self.match_id in data.get("casual_pending", {}):
            del data["casual_pending"][self.match_id]
            save_data(data)

        embed = build_casual_pickup_embed(
            challenger=challenger, opponent=opponent,
            your_team=self.challenger_team, opponent_team=self.opponent_team,
            game_link=self.game_link, guild=guild, accepted=True
        )
        header = (
            f"@here  **Casual Pickup**\n"
            f"**{challenger.display_name}** vs **{opponent.display_name}** is hosting a casual pickup!"
        )
        ch = await get_pickup_channel(guild)
        if ch:
            await ch.send(content=header, embed=embed)
        else:
            try:
                await challenger.send(content="⚠️ Pickup channel not set. Here's the matchup:", embed=embed)
            except discord.Forbidden:
                pass

        accepted_embed = discord.Embed(
            title="✅ Challenge Accepted!",
            description=(
                f"You accepted **{self.challenger_name}**'s casual pickup!\n\n"
                f"The matchup has been posted to the pickup channel.\n"
                f"🔗 [Join the game]({self.game_link})"
            ),
            color=0x57F287
        )
        accepted_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=accepted_embed, view=self)

        try:
            notify = discord.Embed(
                title="✅ Challenge Accepted!",
                description=(
                    f"**{self.opponent_name}** accepted your casual pickup!\n\n"
                    f"The matchup has been posted to the pickup channel.\n"
                    f"🔗 [Join the game]({self.game_link})"
                ),
                color=0x57F287
            )
            notify.set_footer(text=UFF_FOOTER)
            await challenger.send(embed=notify)
        except discord.Forbidden:
            pass

    @discord.ui.button(label="❌  Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Only the challenged player can respond.", ephemeral=True)
            return
        if self.responded:
            await interaction.response.send_message("This challenge has already been answered.", ephemeral=True)
            return
        self.responded = True
        self.stop()
        for item in self.children:
            item.disabled = True

        data = load_data()
        if self.match_id in data.get("casual_pending", {}):
            del data["casual_pending"][self.match_id]
            save_data(data)

        declined_embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"You declined **{self.challenger_name}**'s casual pickup.",
            color=0xED4245
        )
        declined_embed.set_footer(text=UFF_FOOTER)
        await interaction.response.edit_message(embed=declined_embed, view=self)

        guild = bot.get_guild(self.guild_id)
        if guild:
            try:
                challenger = guild.get_member(self.challenger_id) or await guild.fetch_member(self.challenger_id)
                notify = discord.Embed(
                    title="❌ Challenge Declined",
                    description=(
                        f"**{self.opponent_name}** declined your casual pickup.\n"
                        f"You're free to challenge someone else!"
                    ),
                    color=0xED4245
                )
                notify.set_footer(text=UFF_FOOTER)
                await challenger.send(embed=notify)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                pass


# ─────────────────────────────────────────────────────────────────────
# SUSPENSION UI
# ─────────────────────────────────────────────────────────────────────
def _make_suspension_options():
    """
    Build the list of SelectOptions for the suspension dropdown.
    Normal reasons are grouped and their x1/x2/x3 variants shown with
    a suffix so staff can pick the same reason multiple times.
    Discord hard-limits selects to 25 options.
    """
    options = []
    seen_bases: dict[str, int] = {}  # base label -> count of variants added

    for key, (label, games) in SUSPENSION_REASONS.items():
        seen_bases[label] = seen_bases.get(label, 0) + 1
        instance = seen_bases[label]
        if instance == 1:
            display = f"{label} — {games} games"
            desc    = f"Adds {games} games"
        else:
            display = f"{label} (×{instance}) — +{games} games"
            desc    = f"Stack: adds another {games} games"
        options.append(
            discord.SelectOption(label=display, value=key, description=desc)
        )

    for key, label in SPECIAL_SUSPENSION_REASONS.items():
        options.append(
            discord.SelectOption(
                label=label,
                value=key,
                description="Status only — no games added"
            )
        )

    return options[:25]  # Discord hard limit


class SuspensionReasonSelect(discord.ui.Select):
    def __init__(self):
        options = _make_suspension_options()
        super().__init__(
            placeholder=f"Select up to {MAX_SUSPENSION_REASONS} reasons...",
            min_values=1,
            max_values=min(MAX_SUSPENSION_REASONS, len(options)),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        view.selected_reasons = self.values

        total_games, reason_lines, status_lines = _build_suspension_summary(view.selected_reasons)

        preview  = f"**Suspension preview — {view.target.display_name}**\n\n"
        if reason_lines:
            preview += reason_lines + "\n\n"
        if status_lines:
            preview += status_lines + "\n\n"
        preview += f"**Total: {total_games} games**\n\n"
        preview += "Click **Confirm & Post** to publish, or change your selection above."

        await interaction.response.edit_message(content=preview, view=view)


class SuspensionConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Confirm & Post", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view

        if not view.selected_reasons:
            await interaction.response.send_message(
                "❌ Select at least one reason before confirming.", ephemeral=True
            )
            return

        total_games, reason_lines, status_lines = _build_suspension_summary(view.selected_reasons)

        embed = discord.Embed(title="🚫 Player Suspension", color=0xED4245)
        embed.add_field(name="Player", value=f"<@{view.target.id}> ({view.target.display_name})", inline=False)
        if reason_lines:
            embed.add_field(name="Reason(s)", value=reason_lines, inline=False)
        if status_lines:
            embed.add_field(name="Additional Status", value=status_lines, inline=False)
        embed.add_field(name="Total Games Suspended", value=f"**{total_games} games**", inline=False)
        if view.target.avatar:
            embed.set_thumbnail(url=view.target.avatar.url)
        embed.set_footer(text=f"Issued by {interaction.user.display_name} | {UFF_FOOTER}")
        embed.timestamp = datetime.utcnow()

        ch = await get_suspension_channel(interaction.guild)
        for item in view.children:
            item.disabled = True

        if ch:
            await ch.send(embed=embed)
            await interaction.response.edit_message(
                content=f"✅ Suspension posted to {ch.mention}.", view=view
            )
            normal_keys  = [r for r in view.selected_reasons if r in SUSPENSION_REASONS]
            special_keys = [r for r in view.selected_reasons if r in SPECIAL_SUSPENSION_REASONS]
            data = load_data()
            data.setdefault("suspensions", []).append({
                "player_id":      str(view.target.id),
                "player_name":    view.target.display_name,
                "reason_keys":    normal_keys,
                "reasons":        [_base_label(r) for r in normal_keys],
                "status_flags":   [SPECIAL_SUSPENSION_REASONS[r] for r in special_keys],
                "total_games":    total_games,
                "issued_by":      str(interaction.user.id),
                "issued_by_name": interaction.user.display_name,
                "date":           datetime.utcnow().isoformat(),
                "cleared":        False,
            })
            save_data(data)
        else:
            await interaction.response.edit_message(
                content="❌ Could not find the suspension channel. Check `SUSPENSION_CHANNEL_ID`.",
                view=view
            )


class SuspensionCancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: SuspensionView = self.view
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ Suspension cancelled — nothing was posted.", view=view
        )
        view.stop()


class SuspensionView(discord.ui.View):
    def __init__(self, target: discord.Member, issuer_id: int):
        super().__init__(timeout=300)
        self.target           = target
        self.issuer_id        = issuer_id
        self.selected_reasons = []
        self.add_item(SuspensionReasonSelect())
        self.add_item(SuspensionConfirmButton())
        self.add_item(SuspensionCancelButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.issuer_id:
            await interaction.response.send_message(
                "❌ Only the staff member who started this can use these controls.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ─────────────────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    await bot.tree.sync(guild=guild_obj)
    await bot.tree.sync()
    print(f"✅ UFF Bot online — {bot.user}")
    print(f"   Guild             : {GUILD_ID}")
    print(f"   Pickup Channel    : {QBB_CHANNEL_ID or 'not set'}")
    print(f"   Suspension Channel: {SUSPENSION_CHANNEL_ID or 'not set'}")
    print(f"   Owner ID          : {OWNER_ID}")
    print(f"   Thumbnail         : {'set' if UFF_THUMBNAIL else 'NOT SET'}")
    print(f"   Banner            : {'set' if UFF_BANNER else 'NOT SET'}")


# ─────────────────────────────────────────────────────────────────────
# /pickup_ranked
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="pickup_ranked", description="Challenge another player to a ranked UFF pickup")
@app_commands.describe(
    opponent="The player you want to challenge",
    game_link="Roblox game link for this match",
    your_team="Your team name",
    opponent_team="Opponent's team name"
)
async def pickup_ranked(
    interaction: discord.Interaction,
    opponent: discord.Member,
    game_link: str,
    your_team: str,
    opponent_team: str
):
    user_role_ids = {role.id for role in interaction.user.roles}
    if not (user_role_ids & PICKUP_ALLOWED_ROLE_IDS) and not is_admin(interaction):
        await interaction.response.send_message(
            "❌ You don't have the required role to start a ranked pickup.", ephemeral=True
        )
        return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't challenge yourself!", ephemeral=True)
        return
    if opponent.bot:
        await interaction.response.send_message("❌ Can't challenge a bot!", ephemeral=True)
        return

    data = load_data()
    cd, remaining = on_cooldown(data, interaction.user.id)
    if cd:
        e = discord.Embed(
            title="⏳ Cooldown Active",
            description=f"You can challenge again in **{remaining}**.\nCooldown: `{COOLDOWN_MINUTES} minutes`.",
            color=0xE84040
        )
        e.set_footer(text=UFF_FOOTER)
        await interaction.response.send_message(embed=e, ephemeral=True)
        return

    p1 = get_player(data, interaction.user.id)
    p1["username"] = interaction.user.display_name
    p2 = get_player(data, opponent.id)
    p2["username"] = opponent.display_name

    match_id = f"{interaction.user.id}_{opponent.id}_{int(datetime.utcnow().timestamp())}"
    data.setdefault("pending", {})[match_id] = {
        "challenger_id":   str(interaction.user.id),
        "opponent_id":     str(opponent.id),
        "challenger_name": interaction.user.display_name,
        "opponent_name":   opponent.display_name,
        "challenger_team": your_team,
        "opponent_team":   opponent_team,
        "game_link":       game_link,
        "timestamp":       datetime.utcnow().isoformat(),
        "match_id":        match_id,
        "guild_id":        interaction.guild.id
    }
    save_data(data)

    e1, e2 = p1["elo"], p2["elo"]
    r1, emoji1, _ = get_rank(e1)
    r2, emoji2, _ = get_rank(e2)

    dm_embed = discord.Embed(
        title="🏈 You've Been Challenged to a Ranked Pickup!",
        description=(
            f"**{interaction.user.display_name}** wants to play a ranked pickup against you.\n"
            f"Accept or decline below. This request expires in **30 minutes**."
        ),
        color=UFF_COLOR
    )
    dm_embed.add_field(
        name=f"🟡 {interaction.user.display_name}",
        value=f"`{interaction.user.name}`\n**{your_team}**\nRank: `{emoji1} {r1}`",
        inline=True
    )
    dm_embed.add_field(name="\u200b", value="**— VS —**", inline=True)
    dm_embed.add_field(
        name=f"🔵 {opponent.display_name}",
        value=f"`{opponent.name}`\n**{opponent_team}**\nRank: `{emoji2} {r2}`",
        inline=True
    )
    dm_embed.add_field(name="🔗 Game Link", value=f"[**Click here to join →**]({game_link})", inline=False)
    if UFF_BANNER:
        dm_embed.set_image(url=UFF_BANNER)
    if UFF_THUMBNAIL:
        dm_embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild and interaction.guild.icon:
        dm_embed.set_thumbnail(url=interaction.guild.icon.url)
    dm_embed.set_footer(text=f"Challenge issued by {interaction.user.display_name} | {UFF_FOOTER}")
    dm_embed.timestamp = datetime.utcnow()

    view = RankedPickupView(
        match_id=match_id,
        challenger_id=interaction.user.id,
        opponent_id=opponent.id,
        challenger_name=interaction.user.display_name,
        opponent_name=opponent.display_name,
        challenger_team=your_team,
        opponent_team=opponent_team,
        game_link=game_link,
        guild_id=interaction.guild.id,
    )

    try:
        await opponent.send(embed=dm_embed, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Could not DM **{opponent.display_name}** — they have DMs disabled.\n"
            "Ask them to open their DMs and try again.",
            ephemeral=True
        )
        if match_id in data.get("pending", {}):
            del data["pending"][match_id]
            save_data(data)
        return

    confirm_embed = discord.Embed(
        title="📨 Challenge Sent!",
        description=(
            f"Your ranked pickup challenge has been sent to **{opponent.display_name}** via DM.\n\n"
            f"The match will be posted publicly only **if they accept**.\n"
            f"You'll get a DM either way."
        ),
        color=0x57F287
    )
    confirm_embed.set_footer(text=f"{UFF_FOOTER} • 30-minute response window")
    await interaction.response.send_message(embed=confirm_embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# /pickup_casual
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="pickup_casual", description="Challenge another player to a casual (unranked) pickup — no ELO changes")
@app_commands.describe(
    opponent="The player you want to challenge",
    game_link="Roblox game link for this match",
    your_team="Your team name",
    opponent_team="Opponent's team name"
)
async def pickup_casual(
    interaction: discord.Interaction,
    opponent: discord.Member,
    game_link: str,
    your_team: str,
    opponent_team: str
):
    await _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team)


# ─────────────────────────────────────────────────────────────────────
# /casual_pickup  — alternate name
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="casual_pickup", description="Challenge another player to a casual (unranked) pickup — no ELO changes")
@app_commands.describe(
    opponent="The player you want to challenge",
    game_link="Roblox game link for this match",
    your_team="Your team name",
    opponent_team="Opponent's team name"
)
async def casual_pickup(
    interaction: discord.Interaction,
    opponent: discord.Member,
    game_link: str,
    your_team: str,
    opponent_team: str
):
    await _run_casual_pickup(interaction, opponent, game_link, your_team, opponent_team)


# ─────────────────────────────────────────────────────────────────────
# /pickup_results
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="pickup_results", description="Submit ranked pickup match results and scoreboard screenshot")
@app_commands.describe(
    winner="Who won?",
    winner_score="Winner's score",
    loser_score="Loser's score",
    screenshot="Scoreboard screenshot"
)
async def pickup_results(
    interaction: discord.Interaction,
    winner: discord.Member,
    winner_score: int,
    loser_score: int,
    screenshot: discord.Attachment
):
    data = load_data()
    uid = str(interaction.user.id)

    pending = data.get("pending", {})
    match, match_key = None, None
    for key in sorted(pending, key=lambda k: pending[k].get("timestamp", ""), reverse=True):
        m = pending[key]
        if m["challenger_id"] == uid or m["opponent_id"] == uid:
            match, match_key = m, key
            break

    if not match:
        await interaction.response.send_message(
            "❌ No pending ranked pickup found. Use `/pickup_ranked` to start one first.",
            ephemeral=True
        )
        return

    c_id = int(match["challenger_id"])
    o_id = int(match["opponent_id"])

    if winner.id not in [c_id, o_id]:
        await interaction.response.send_message(
            "❌ Winner must be one of the two players in this match.", ephemeral=True
        )
        return

    loser_id   = o_id if winner.id == c_id else c_id
    loser_name = match["opponent_name"] if winner.id == c_id else match["challenger_name"]

    wp = get_player(data, winner.id)
    lp = get_player(data, loser_id)
    wp["username"] = winner.display_name

    old_w, old_l = wp["elo"], lp["elo"]
    wp["elo"] += WIN_ELO
    lp["elo"] = max(0, lp["elo"] - LOSS_ELO)
    wp["wins"]   += 1
    lp["losses"] += 1

    now = datetime.utcnow().isoformat()
    wp["last_game"] = now
    lp["last_game"] = now

    data.setdefault("matches", []).append({
        "winner_id":       str(winner.id),
        "winner_name":     winner.display_name,
        "loser_id":        str(loser_id),
        "loser_name":      loser_name,
        "winner_score":    winner_score,
        "loser_score":     loser_score,
        "challenger_team": match["challenger_team"],
        "opponent_team":   match["opponent_team"],
        "date":            now
    })

    if match_key and match_key in data["pending"]:
        del data["pending"][match_key]
    save_data(data)

    w_elo, l_elo   = wp["elo"], lp["elo"]
    wr, we, wcolor = get_rank(w_elo)
    lr, le, _      = get_rank(l_elo)

    embed = discord.Embed(title="🏆 Pickup Results", color=wcolor)
    embed.add_field(
        name="🏆 Winner",
        value=(
            f"<@{winner.id}> **{winner.display_name}**\n"
            f"> Score: **{winner_score}**\n"
            f"> ELO: `{old_w}` → `{w_elo}` **(+{WIN_ELO})**\n"
            f"> Rank: `{we} {wr}`"
        ),
        inline=True
    )
    embed.add_field(
        name="❌ Loser",
        value=(
            f"<@{loser_id}> **{loser_name}**\n"
            f"> Score: **{loser_score}**\n"
            f"> ELO: `{old_l}` → `{l_elo}` **(-{LOSS_ELO})**\n"
            f"> Rank: `{le} {lr}`"
        ),
        inline=True
    )
    embed.add_field(
        name="📊 Final Score",
        value=f"**{winner.display_name}** `{winner_score} — {loser_score}` **{loser_name}**",
        inline=False
    )
    embed.set_image(url=screenshot.url)
    if UFF_THUMBNAIL:
        embed.set_thumbnail(url=UFF_THUMBNAIL)
    elif interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.set_footer(text=f"{UFF_FOOTER} • Submitted by {interaction.user.display_name}")
    embed.timestamp = datetime.utcnow()

    ch = await get_pickup_channel(interaction.guild)
    if ch and ch.id != interaction.channel_id:
        await interaction.response.send_message("✅ Results posted!", ephemeral=True)
        await ch.send(embed=embed)
    else:
        await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# /pickup_profile
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="pickup_profile", description="View UFF pickup rank and stats")
@app_commands.describe(player="Player to look up (leave blank for yourself)")
async def pickup_profile(interaction: discord.Interaction, player: discord.Member = None):
    target = player or interaction.user
    data = load_data()
    p = get_player(data, target.id)
    elo = p["elo"]
    rank, emoji, color = get_rank(elo)
    gp = p["wins"] + p["losses"]
    wr = f"{p['wins'] / gp * 100:.1f}%" if gp else "N/A"

    embed = discord.Embed(title=f"{emoji} {target.display_name}", color=color)
    embed.add_field(name="Rank",     value=f"`{emoji} {rank}`", inline=True)
    embed.add_field(name="ELO",      value=f"`{elo}`",          inline=True)
    embed.add_field(name="Wins",     value=f"`{p['wins']}`",    inline=True)
    embed.add_field(name="Losses",   value=f"`{p['losses']}`",  inline=True)
    embed.add_field(name="Win Rate", value=f"`{wr}`",           inline=True)
    if target.avatar:
        embed.set_thumbnail(url=target.avatar.url)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# /pickup_leaderboard
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="pickup_leaderboard", description="UFF pickup ELO leaderboard")
async def pickup_leaderboard(interaction: discord.Interaction):
    data = load_data()
    players = data.get("players", {})
    if not players:
        await interaction.response.send_message("No players yet — play some pickups!", ephemeral=True)
        return

    top = sorted(players.items(), key=lambda x: x[1]["elo"], reverse=True)[:15]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, p) in enumerate(top):
        elo   = p["elo"]
        rank, emoji, _ = get_rank(elo)
        medal = medals[i] if i < 3 else f"`{i + 1}.`"
        name  = p.get("username") or f"<@{uid}>"
        lines.append(f"{medal} **{name}** — {emoji} `{rank}` | ELO `{elo}` | {p['wins']}W {p['losses']}L")

    embed = discord.Embed(title="UFF Pickup — ELO Leaderboard", description="\n".join(lines), color=UFF_COLOR)
    apply_branding(embed)
    embed.set_footer(text=UFF_FOOTER)
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# /match_history
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="match_history", description="View recent UFF pickup results")
async def match_history(interaction: discord.Interaction):
    data = load_data()
    matches = list(reversed(data.get("matches", [])))[:10]
    if not matches:
        await interaction.response.send_message("No matches recorded yet.", ephemeral=True)
        return

    lines = [
        f"🏆 **{m['winner_name']}** `{m.get('winner_score', '?')}–{m.get('loser_score', '?')}` {m['loser_name']}"
        for m in matches
    ]
    embed = discord.Embed(title="📋 UFF Pickup — Recent Results", description="\n".join(lines), color=0x4090E8)
    embed.set_footer(text=f"{UFF_FOOTER} • Last 10 matches")
    embed.timestamp = datetime.utcnow()
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# /teams
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="teams", description="View all 20 UFF league teams")
async def teams_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="United Flag Football — All Teams", color=UFF_COLOR)
    embed.add_field(
        name="Teams 1–10",
        value="\n".join(f"`{t[1]}` **{t[0]}**" for t in TEAMS[:10]),
        inline=True
    )
    embed.add_field(
        name="Teams 11–20",
        value="\n".join(f"`{t[1]}` **{t[0]}**" for t in TEAMS[10:]),
        inline=True
    )
    apply_branding(embed)
    embed.set_footer(text=f"{UFF_FOOTER} • 20 teams")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# /reset_player  — Admin only
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="reset_player", description="[Admin] Reset a player's ELO to 900")
@app_commands.describe(player="Player to reset")
@app_commands.default_permissions(administrator=True)
async def reset_player(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    data = load_data()
    data["players"][str(player.id)] = {
        "elo": STARTING_ELO, "wins": 0, "losses": 0,
        "last_game": None, "username": player.display_name
    }
    save_data(data)
    await interaction.response.send_message(
        f"✅ Reset **{player.display_name}**'s ELO to `{STARTING_ELO}`.", ephemeral=True
    )


# ─────────────────────────────────────────────────────────────────────
# /adjust_elo  — Admin only
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="adjust_elo", description="[Admin] Manually adjust a player's ELO")
@app_commands.describe(player="Target player", amount="ELO to add (negative to subtract)")
@app_commands.default_permissions(administrator=True)
async def adjust_elo(interaction: discord.Interaction, player: discord.Member, amount: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    data = load_data()
    p = get_player(data, player.id)
    old = p["elo"]
    p["elo"] = max(0, p["elo"] + amount)
    p["username"] = player.display_name
    save_data(data)
    sign = "+" if amount >= 0 else ""
    await interaction.response.send_message(
        f"✅ **{player.display_name}** ELO: `{old}` → `{p['elo']}` ({sign}{amount})", ephemeral=True
    )


# ─────────────────────────────────────────────────────────────────────
# /clear_cooldown  — Admin only
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="clear_cooldown", description="[Admin] Clear a player's cooldown")
@app_commands.describe(player="Player to clear")
@app_commands.default_permissions(administrator=True)
async def clear_cooldown(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    data = load_data()
    get_player(data, player.id)["last_game"] = None
    save_data(data)
    await interaction.response.send_message(
        f"✅ Cleared cooldown for **{player.display_name}**.", ephemeral=True
    )


# ─────────────────────────────────────────────────────────────────────
# /suspension  — Staff only
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="suspension", description="[Staff] Issue a suspension to a player")
@app_commands.describe(player="The player to suspend")
async def suspension(interaction: discord.Interaction, player: discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to issue suspensions.", ephemeral=True
        )
        return
    view = SuspensionView(target=player, issuer_id=interaction.user.id)
    await interaction.response.send_message(
        content=(
            f"**Issuing suspension for {player.display_name}**\n"
            f"Select up to {MAX_SUSPENSION_REASONS} reasons below. "
            f"Pick the same reason multiple times (×2, ×3) to stack games."
        ),
        view=view,
        ephemeral=True
    )


# ─────────────────────────────────────────────────────────────────────
# /unsuspend  — Staff only
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="unsuspend", description="[Staff] Clear a player's suspension")
@app_commands.describe(player="The player to unsuspend")
async def unsuspend(interaction: discord.Interaction, player: discord.Member):
    if not can_issue_suspension(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to clear suspensions.", ephemeral=True
        )
        return

    data = load_data()
    cleared_any = False
    for s in data.get("suspensions", []):
        if s.get("player_id") == str(player.id) and not s.get("cleared", False):
            s["cleared"]          = True
            s["cleared_by"]       = str(interaction.user.id)
            s["cleared_by_name"]  = interaction.user.display_name
            s["cleared_date"]     = datetime.utcnow().isoformat()
            cleared_any = True
    save_data(data)

    embed = discord.Embed(title="✅ Player Unsuspended", color=0x57F287)
    embed.add_field(name="Player", value=f"<@{player.id}> ({player.display_name})", inline=False)
    embed.add_field(name="Status", value="**Cleared** — eligible to play", inline=False)
    if player.avatar:
        embed.set_thumbnail(url=player.avatar.url)
    embed.set_footer(text=f"Cleared by {interaction.user.display_name} | {UFF_FOOTER}")
    embed.timestamp = datetime.utcnow()

    ch = await get_suspension_channel(interaction.guild)
    if ch:
        await ch.send(embed=embed)
        note = "" if cleared_any else "\n*(No open suspension records found, but the notice was posted anyway.)*"
        await interaction.response.send_message(
            f"✅ {player.display_name} has been unsuspended. Notice posted to {ch.mention}.{note}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Could not find the suspension channel. Check `SUSPENSION_CHANNEL_ID`.",
            ephemeral=True
        )


# ─────────────────────────────────────────────────────────────────────
# /help_uff
# ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="help_uff", description="UFF bot command guide")
async def help_uff(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 United Flag Football — Commands", color=UFF_COLOR)
    embed.add_field(name="⚔️ Ranked Pickup", value=(
        "`/pickup_ranked` — Challenge to a ranked pickup (ELO affected)\n"
        "`/pickup_results` — Submit results + screenshot after the game\n"
        "`/pickup_profile` — Your ELO, rank & stats\n"
        "`/pickup_leaderboard` — Top 15 ELO rankings\n"
        "`/match_history` — Last 10 results"
    ), inline=False)
    embed.add_field(name="🎮 Casual Pickup", value=(
        "`/pickup_casual` or `/casual_pickup` — Casual pickup, no ELO changes"
    ), inline=False)
    embed.add_field(name="🏟️ League", value="`/teams` — View all 20 UFF teams", inline=False)
    embed.add_field(name="🛡️ Admin", value=(
        "`/reset_player` — Reset ELO to 900\n"
        "`/adjust_elo` — Manually change ELO\n"
        "`/clear_cooldown` — Remove cooldown"
    ), inline=False)
    embed.add_field(name="🚫 Suspensions", value=(
        f"`/suspension` — [Staff] Pick a player + up to {MAX_SUSPENSION_REASONS} reasons (stack same reason via ×2/×3)\n"
        "`/unsuspend` — [Staff] Clear a player's suspension and post a clearance notice"
    ), inline=False)
    embed.add_field(name="📊 Ranks", value=(
        "**Start:** 900 ELO | **Win:** +100 | **Loss:** −100\n"
        "⚙️ Iron I / II / III → 0 / 700 / 900 ELO\n"
        "🥇 Gold I / II / III → 1,100 / 1,300 / 1,500 ELO\n"
        "💎 Amethyst I / II / III → 1,700 / 1,900 / 2,100 ELO"
    ), inline=False)
    embed.add_field(name="ℹ️ How It Works", value=(
        "1️⃣ Use `/pickup_ranked` or `/pickup_casual` to challenge someone\n"
        "2️⃣ Opponent gets a **DM** with Accept / Decline buttons\n"
        "3️⃣ If they accept → matchup posts publicly with a ping\n"
        "4️⃣ If they decline → challenger is notified, no public post\n"
        "5️⃣ After a **ranked** game, use `/pickup_results` to log the winner"
    ), inline=False)
    apply_branding(embed)
    embed.set_footer(text=f"{UFF_FOOTER} • {COOLDOWN_MINUTES}-min ranked cooldown")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
