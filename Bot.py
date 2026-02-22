"""
Lite Discord Bot
- Ticket system (create/close/claim tickets)
- Changelog command for posting updates with images/files
- Role management helpers
"""

import discord
from discord.ext import commands
from discord import app_commands
import os, asyncio, datetime, json

# ── Config ────────────────────────────────────────────────────
TOKEN         = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID      = int(os.environ.get("DISCORD_GUILD_ID", "1475014802194567238"))

# Channel IDs — set these to your actual channel IDs
TICKET_CATEGORY_ID  = int(os.environ.get("TICKET_CATEGORY_ID",  "0"))
TICKET_LOG_ID       = int(os.environ.get("TICKET_LOG_ID",       "0"))
CHANGELOG_CHANNEL_ID= int(os.environ.get("CHANGELOG_CHANNEL_ID","0"))

# Role IDs
ROLE_LITE = 1475015141882855424
ROLE_UFF  = 1475022240754962452
ROLE_FFL  = 1475022200095510621
SUPPORT_ROLE_ID = int(os.environ.get("SUPPORT_ROLE_ID", str(ROLE_LITE)))

# ── Bot setup ─────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Ticket Views ──────────────────────────────────────────────

class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a Ticket", style=discord.ButtonStyle.green,
                       emoji="🎫", custom_id="ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user  = interaction.user

        existing = discord.utils.get(guild.text_channels, name=f"ticket-{user.name.lower()[:20]}")
        if existing:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}", ephemeral=True)
            return

        category = None
        if TICKET_CATEGORY_ID:
            category = guild.get_channel(TICKET_CATEGORY_ID)
        if not category:
            category = discord.utils.get(guild.categories, name="Tickets")
            if not category:
                category = await guild.create_category("Tickets")

        support_role = guild.get_role(SUPPORT_ROLE_ID)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_channels=True)

        ch_name = f"ticket-{user.name.lower()[:20]}"
        channel = await guild.create_text_channel(
            ch_name, category=category, overwrites=overwrites,
            topic=f"Ticket opened by {user} ({user.id})")

        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=(
                f"Welcome {user.mention}!\n\n"
                "Please describe your issue and a staff member will assist you shortly.\n\n"
                "Click **Close Ticket** when your issue is resolved."
            ),
            color=0x00f5a0,
            timestamp=datetime.datetime.utcnow()
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_footer(text=f"Ticket ID: {channel.id}")

        view = TicketControlView()
        msg  = await channel.send(content=f"{user.mention}", embed=embed, view=view)
        await msg.pin()

        await interaction.response.send_message(
            f"✅ Ticket created: {channel.mention}", ephemeral=True)

        if TICKET_LOG_ID:
            log_ch = guild.get_channel(TICKET_LOG_ID)
            if log_ch:
                log_embed = discord.Embed(
                    title="Ticket Opened",
                    description=f"**User:** {user.mention} (`{user.id}`)\n**Channel:** {channel.mention}",
                    color=0x00f5a0, timestamp=datetime.datetime.utcnow())
                await log_ch.send(embed=log_embed)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red,
                       emoji="🔒", custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
        is_staff = support_role and support_role in interaction.user.roles
        is_admin = interaction.user.guild_permissions.administrator
        if not (is_staff or is_admin):
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔒 Closing Ticket",
            description=f"Closed by {interaction.user.mention}. Deleting in 5 seconds.",
            color=0xff4d6d, timestamp=datetime.datetime.utcnow())
        await interaction.response.send_message(embed=embed)

        if TICKET_LOG_ID:
            log_ch = interaction.guild.get_channel(TICKET_LOG_ID)
            if log_ch:
                log_embed = discord.Embed(
                    title="Ticket Closed",
                    description=f"**Channel:** #{interaction.channel.name}\n**Closed by:** {interaction.user.mention}",
                    color=0xff4d6d, timestamp=datetime.datetime.utcnow())
                await log_ch.send(embed=log_embed)

        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.blurple,
                       emoji="✋", custom_id="ticket_claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
        if not (support_role in interaction.user.roles or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return
        embed = discord.Embed(
            description=f"✋ {interaction.user.mention} has claimed this ticket.",
            color=0x4d9fff)
        await interaction.response.send_message(embed=embed)


# ── Slash Commands ────────────────────────────────────────────

@tree.command(name="ticket-panel", description="Post the ticket creation panel (owner only)",
              guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_role(ROLE_LITE)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 Lite Support",
        description=(
            "Need help? Click below to open a private support ticket.\n\n"
            "**What to include:**\n"
            "• Your issue in detail\n"
            "• Screenshots if relevant\n"
            "• Your Discord and Roblox username\n\n"
            "*Tickets are private — only you and staff can see them.*"
        ),
        color=0x00f5a0
    )
    embed.set_footer(text="Lite Forensic Platform")
    view = TicketCreateView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Ticket panel posted.", ephemeral=True)


@tree.command(name="changelog", description="Post a changelog update (owner only)",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    version="Version tag (e.g. v3.1)",
    title="Short update title",
    description="Full changelog — use \\n for new lines",
    attachment="Optional image or file to attach"
)
@app_commands.checks.has_role(ROLE_LITE)
async def changelog(
    interaction: discord.Interaction,
    version: str,
    title: str,
    description: str,
    attachment: discord.Attachment = None
):
    ch = interaction.guild.get_channel(CHANGELOG_CHANNEL_ID)
    if not ch:
        await interaction.response.send_message(
            "❌ Changelog channel not found. Set `CHANGELOG_CHANNEL_ID` env var.", ephemeral=True)
        return

    description = description.replace("\\n", "\n")

    embed = discord.Embed(
        title=f"📋 {title}",
        description=description,
        color=0x00f5a0,
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_author(
        name=f"Lite Scanner  ·  {version}",
        icon_url=interaction.guild.icon.url if interaction.guild.icon else None
    )
    embed.set_footer(
        text=f"Posted by {interaction.user}",
        icon_url=interaction.user.display_avatar.url
    )

    file = None
    if attachment:
        import io, aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as resp:
                data = await resp.read()
        file = discord.File(io.BytesIO(data), filename=attachment.filename)
        if attachment.content_type and attachment.content_type.startswith("image/"):
            embed.set_image(url=f"attachment://{attachment.filename}")

    await ch.send(embed=embed, file=file)
    await interaction.response.send_message(f"✅ Changelog posted to {ch.mention}!", ephemeral=True)


@tree.command(name="announce", description="Post an announcement to any channel (owner only)",
              guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    channel="Channel to post in",
    message="Message text — use \\n for new lines",
    ping="Optional role to ping",
    attachment="Optional image or file"
)
@app_commands.checks.has_role(ROLE_LITE)
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
    ping: discord.Role = None,
    attachment: discord.Attachment = None
):
    message = message.replace("\\n", "\n")
    embed = discord.Embed(
        description=message,
        color=0x00f5a0,
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url
    )

    file    = None
    content = ping.mention if ping else None

    if attachment:
        import io, aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as resp:
                data = await resp.read()
        file = discord.File(io.BytesIO(data), filename=attachment.filename)
        if attachment.content_type and attachment.content_type.startswith("image/"):
            embed.set_image(url=f"attachment://{attachment.filename}")

    await channel.send(content=content, embed=embed, file=file)
    await interaction.response.send_message(
        f"✅ Posted to {channel.mention}!", ephemeral=True)


@tree.command(name="close", description="Close the current ticket",
              guild=discord.Object(id=GUILD_ID))
async def close_cmd(interaction: discord.Interaction):
    if not interaction.channel.name.startswith("ticket-"):
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    support_role = interaction.guild.get_role(SUPPORT_ROLE_ID)
    is_staff = support_role and support_role in interaction.user.roles
    if not (is_staff or interaction.user.guild_permissions.administrator):
        await interaction.response.send_message("Only staff can use this.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🔒 Closing Ticket",
        description=f"Closed by {interaction.user.mention}. Deleting in 5 seconds.",
        color=0xff4d6d)
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(5)
    await interaction.channel.delete()


# ── Events ────────────────────────────────────────────────────

@bot.event
async def on_ready():
    bot.add_view(TicketCreateView())
    bot.add_view(TicketControlView())
    try:
        synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✓ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"✗ Command sync error: {e}")
    print(f"✓ Lite Bot online as {bot.user} ({bot.user.id})")
    print(f"  Guild:      {GUILD_ID}")
    print(f"  Changelog:  #{CHANGELOG_CHANNEL_ID}")
    print(f"  Ticket cat: #{TICKET_CATEGORY_ID}")


@tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingRole):
        await interaction.response.send_message(
            "❌ You need the **Lite** role to use this command.", ephemeral=True)
    else:
        print(f"Command error: {error}")
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN env var not set")
        exit(1)
    bot.run(TOKEN)