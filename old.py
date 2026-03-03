import discord
from discord.ext import commands, tasks
import logging
from dotenv import load_dotenv
import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError(
        "DISCORD_TOKEN not found. Make sure you have a .env file in the same "
        "directory as bot.py with the line:\n  DISCORD_TOKEN=your_token_here"
    )

TRACKED_PATH = Path(__file__).parent / "tracked_messages.json"
ROLES_PATH   = Path(__file__).parent / "allowed_roles.json"

handle = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
handle.setFormatter(logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s"))
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handle)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_tracked():
    if not TRACKED_PATH.exists():
        return []
    try:
        with open(TRACKED_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []

def save_tracked(data):
    with open(TRACKED_PATH, "w") as f:
        json.dump(data, f, indent=2)

def load_roles() -> dict:
    """Returns {guild_id_str: role_id_str}"""
    if not ROLES_PATH.exists():
        return {}
    try:
        with open(ROLES_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_roles(data: dict):
    with open(ROLES_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def is_guild_owner():
    async def predicate(ctx):
        if ctx.author.id == ctx.guild.owner_id:
            return True
        await ctx.message.add_reaction("🚫")
        return False
    return commands.check(predicate)

def has_allowed_role():
    """Guild owner always passes. Others need the role configured via !annoy_setup."""
    async def predicate(ctx):
        if ctx.author.id == ctx.guild.owner_id:
            return True
        roles = load_roles()
        role_id = roles.get(str(ctx.guild.id))
        if not role_id:
            await ctx.send("⚠️ No allowed role has been set. The server owner must run `!annoy_setup @Role` first.")
            return False
        if any(str(r.id) == role_id for r in ctx.author.roles):
            return True
        await ctx.message.add_reaction("🚫")
        return False
    return commands.check(predicate)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    if not deadline_check.is_running():
        deadline_check.start()

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    tracked = load_tracked()
    changed = False
    for t in tracked:
        if t["message_id"] == str(reaction.message.id) and t["trigger_type"] == "reaction":
            if str(user.id) not in t["responded_user_ids"]:
                t["responded_user_ids"].append(str(user.id))
                changed = True
            break
    if changed:
        save_tracked(tracked)

@bot.event
async def on_message(message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    # Only count Discord-native replies (reply arrow UI) to a tracked message
    if message.reference and message.reference.message_id:
        tracked = load_tracked()
        changed = False
        for t in tracked:
            if t["message_id"] == str(message.reference.message_id) and t["trigger_type"] == "reply":
                if str(message.author.id) not in t["responded_user_ids"]:
                    t["responded_user_ids"].append(str(message.author.id))
                    changed = True
                break
        if changed:
            save_tracked(tracked)
    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Deadline loop
# ---------------------------------------------------------------------------

@tasks.loop(seconds=30)
async def deadline_check():
    now = datetime.now(timezone.utc)
    tracked = load_tracked()
    to_remove = []

    for t in tracked:
        try:
            deadline = datetime.fromisoformat(t["deadline_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            to_remove.append(t)
            continue

        if now < deadline:
            continue

        to_remove.append(t)

        guild   = bot.get_guild(int(t["guild_id"]))
        channel = bot.get_channel(int(t["ping_channel_id"]))
        if not guild:
            continue

        responded = set(t["responded_user_ids"])
        members   = [m for m in guild.members if not m.bot and str(m.id) not in responded]

        if not members:
            logger.info(f"All members responded for message {t['message_id']}.")
            continue

        verb     = "reply to" if t["trigger_type"] == "reply" else "react to"
        jump_url = f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}/{t['message_id']}"

        for member in members:
            try:
                if t.get("do_dm"):
                    await member.send(
                        f"Hey! You haven't {verb} a message in **{guild.name}**.\n"
                        f"Jump to it here: {jump_url}"
                    )
                if t.get("do_ping", True) and channel:
                    await channel.send(
                        f"{member.mention} — reminder to {verb} the message: {jump_url}"
                    )
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning("Could not notify %s: %s", member.id, e)

    for t in to_remove:
        if t in tracked:
            tracked.remove(t)
    if to_remove:
        save_tracked(tracked)


# ---------------------------------------------------------------------------
# !annoy_setup  (guild owner only)
# ---------------------------------------------------------------------------

@bot.command(name="annoy_setup")
@is_guild_owner()
async def annoy_setup(ctx, *, arg: str = None):
    """
    Grant a role access to !annoy commands. Only the server owner can run this.

    `!annoy_setup @Role`   — give a role access
    `!annoy_setup clear`   — remove the role (owner-only access after)
    """
    if arg is None:
        await ctx.send("Usage: `!annoy_setup @Role` or `!annoy_setup clear`")
        return

    roles = load_roles()

    if arg.strip().lower() == "clear":
        roles.pop(str(ctx.guild.id), None)
        save_roles(roles)
        await ctx.send("✅ Allowed role cleared. Only the server owner can use `!annoy` now.")
        return

    # Try to resolve a role mention or name
    role = None
    if ctx.message.role_mentions:
        role = ctx.message.role_mentions[0]
    else:
        role = discord.utils.find(lambda r: r.name.lower() == arg.lower(), ctx.guild.roles)

    if not role:
        await ctx.send(f"Role `{arg}` not found. Mention it with @Role or use the exact name.")
        return

    roles[str(ctx.guild.id)] = str(role.id)
    save_roles(roles)
    await ctx.send(f"✅ Members with **{role.name}** can now use `!annoy` commands.")


# ---------------------------------------------------------------------------
# !annoy  (guild owner or allowed role)
# ---------------------------------------------------------------------------

@bot.command(name="annoy")
@has_allowed_role()
async def annoy(ctx, action: str = None, trigger_type: str = None, minutes: str = None, channel_mention: str = None, *flags):
    """
    Reply to a message, then run:
    `!annoy track <reply|reaction> <minutes> <#channel> [--noping] [--dm]`

      reply | reaction  — watch for Discord replies or reactions
      minutes           — deadline from now (60 = 1hr, 1440 = 1 day, 10080 = 1 week)
      #channel          — where to send ping reminders
      --noping          — disable channel pings (default: on)
      --dm              — also DM non-responders (default: off)

    `!annoy list`              — show active trackers in this server
    `!annoy cancel <msg_id>`   — stop tracking a message
    """
    if action is None or action.lower() == "help":
        await ctx.send(
            "**!annoy commands**\n"
            "Reply to a message first, then:\n"
            "`!annoy track <reply|reaction> <minutes> <#channel> [--noping] [--dm]`\n"
            "  • `reply|reaction` — watch for replies or reactions\n"
            "  • `minutes` — deadline (60=1hr, 1440=1day, 10080=1wk)\n"
            "  • `#channel` — where to ping\n"
            "  • `--noping` — disable channel pings\n"
            "  • `--dm` — also DM non-responders\n\n"
            "`!annoy list` — show active trackers\n"
            "`!annoy cancel <message_id>` — stop tracking\n\n"
            "**Setup (server owner only):**\n"
            "`!annoy_setup @Role` — grant a role access to !annoy\n"
            "`!annoy_setup clear` — remove role (owner-only after)"
        )
        return

    # --- LIST ---
    if action.lower() == "list":
        tracked = load_tracked()
        guild_tracked = [t for t in tracked if str(t["guild_id"]) == str(ctx.guild.id)]
        if not guild_tracked:
            await ctx.send("No messages are being tracked in this server.")
            return
        lines = []
        for t in guild_tracked:
            try:
                deadline = datetime.fromisoformat(t["deadline_at"].replace("Z", "+00:00"))
                jump_url = f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}/{t['message_id']}"
                lines.append(
                    f"• <{jump_url}> — **{t['trigger_type']}** — "
                    f"deadline {deadline.strftime('%Y-%m-%d %H:%M')} UTC — "
                    f"ping: {t.get('do_ping', True)}, dm: {t.get('do_dm', False)} — "
                    f"{len(t['responded_user_ids'])} responded"
                )
            except (KeyError, ValueError):
                lines.append(f"• `{t.get('message_id', '?')}` — invalid data")
        await ctx.send("\n".join(lines))
        return

    # --- CANCEL ---
    if action.lower() == "cancel":
        msg_id = trigger_type  # positional reuse
        if not msg_id:
            await ctx.send("Usage: `!annoy cancel <message_id>`")
            return
        tracked = load_tracked()
        before  = len(tracked)
        tracked = [t for t in tracked if not (t["message_id"] == msg_id and str(t["guild_id"]) == str(ctx.guild.id))]
        if len(tracked) < before:
            save_tracked(tracked)
            await ctx.send(f"✅ Stopped tracking message `{msg_id}`.")
        else:
            await ctx.send(f"No tracker found for message `{msg_id}` in this server.")
        return

    # --- TRACK ---
    if action.lower() != "track":
        await ctx.send("Unknown action. Run `!annoy help` for usage.")
        return

    if not ctx.message.reference:
        await ctx.send("You need to **reply** to the message you want to track, then run the command.")
        return

    if trigger_type not in ("reply", "reaction"):
        await ctx.send("Trigger type must be `reply` or `reaction`.")
        return

    try:
        mins = int(minutes)
        if mins < 1:
            raise ValueError
    except (TypeError, ValueError):
        await ctx.send("Please provide a valid number of minutes (e.g. `60`).")
        return

    if not channel_mention or not (channel_mention.startswith("<#") and channel_mention.endswith(">")):
        await ctx.send("Please mention the ping channel, e.g. `#general`")
        return

    try:
        ping_channel_id = int(channel_mention.strip("<#>"))
    except ValueError:
        await ctx.send("Invalid channel. Use a channel mention like `#general`.")
        return

    ping_channel = bot.get_channel(ping_channel_id)
    if not ping_channel or ping_channel.guild.id != ctx.guild.id:
        await ctx.send("That channel isn't in this server or doesn't exist.")
        return

    do_ping = "--noping" not in [f.lower() for f in flags]
    do_dm   = "--dm"     in  [f.lower() for f in flags]

    if not do_ping and not do_dm:
        await ctx.send("You disabled both pings and DMs — nothing would happen. Remove `--noping` or add `--dm`.")
        return

    try:
        tracked_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
    except discord.NotFound:
        await ctx.send("Couldn't find the message you replied to.")
        return

    tracked = load_tracked()
    if any(t["message_id"] == str(tracked_message.id) for t in tracked):
        await ctx.send("This message is already being tracked.")
        return

    # Seed users who already responded before tracking started
    responded_ids = []
    if trigger_type == "reaction":
        for r in tracked_message.reactions:
            async for u in r.users():
                if not u.bot and str(u.id) not in responded_ids:
                    responded_ids.append(str(u.id))
    else:
        try:
            async for msg in tracked_message.channel.history(limit=500):
                if (
                    msg.reference is not None
                    and msg.reference.message_id == tracked_message.id
                    and not msg.author.bot
                ):
                    if str(msg.author.id) not in responded_ids:
                        responded_ids.append(str(msg.author.id))
        except (discord.HTTPException, AttributeError):
            pass

    deadline = datetime.now(timezone.utc) + timedelta(minutes=mins)

    tracked.append({
        "message_id":         str(tracked_message.id),
        "channel_id":         str(tracked_message.channel.id),
        "guild_id":           str(ctx.guild.id),
        "trigger_type":       trigger_type,
        "deadline_at":        deadline.isoformat(),
        "ping_channel_id":    str(ping_channel_id),
        "do_ping":            do_ping,
        "do_dm":              do_dm,
        "responded_user_ids": responded_ids,
    })
    save_tracked(tracked)

    await ctx.send(
        f"✅ Tracking this message for **{trigger_type}**.\n"
        f"⏰ Deadline: **{mins} minutes** from now ({deadline.strftime('%H:%M UTC')})\n"
        f"📣 Will {'ping in ' + ping_channel.mention if do_ping else 'not ping'} "
        f"and {'DM' if do_dm else 'not DM'} non-responders."
    )


def main():
    bot.run(token)

if __name__ == "__main__":
    main()