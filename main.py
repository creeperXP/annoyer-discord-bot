import discord
from discord.ext import commands, tasks
import logging
from dotenv import load_dotenv
import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from keep_alive import keep_alive

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError(
        "DISCORD_TOKEN not found. Make sure you have a .env file in the same "
        "directory as bot.py with the line:\n  DISCORD_TOKEN=your_token_here"
    )

keep_alive()

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
    """Returns {guild_id_str: role_id_str} for bot-operator roles."""
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
# Helpers
# ---------------------------------------------------------------------------

def get_target_members(guild: discord.Guild, target_role_id: Optional[str], responded: set) -> list:
    """
    Returns non-bot members who haven't responded yet.
    If target_role_id is set, only members with that role are included.
    """
    if target_role_id:
        role = guild.get_role(int(target_role_id))
        if not role:
            return []
        return [m for m in role.members if not m.bot and str(m.id) not in responded]
    return [m for m in guild.members if not m.bot and str(m.id) not in responded]

def parse_flags(flags: tuple) -> tuple:
    """
    Parse *flags into (do_ping, do_dm, role_mention_str).
    --noping  → disable channel pings
    --dm      → enable DMs
    --role <@&id> → only track/ping members of this role
    """
    flag_list = list(flags)
    do_ping = "--noping" not in [f.lower() for f in flag_list]
    do_dm   = "--dm"     in  [f.lower() for f in flag_list]
    role_str = None
    for i, f in enumerate(flag_list):
        if f.lower() == "--role" and i + 1 < len(flag_list):
            role_str = flag_list[i + 1]
            break
    return do_ping, do_dm, role_str

def resolve_role_from_str(guild: discord.Guild, role_str: str) -> Optional[discord.Role]:
    """Resolve a role mention (<@&id>) or plain name to a Role object."""
    if not role_str:
        return None
    # Role mention format: <@&123456>
    if role_str.startswith("<@&") and role_str.endswith(">"):
        try:
            role_id = int(role_str[3:-1])
            return guild.get_role(role_id)
        except ValueError:
            return None
    # Fall back to name match
    return discord.utils.find(lambda r: r.name.lower() == role_str.lower(), guild.roles)


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
            await ctx.send("⚠️ No allowed role set. The server owner must run `!annoy_setup @Role` first.")
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
            # If role-scoped, only count users who have that role
            target_role_id = t.get("target_role_id")
            if target_role_id:
                member = reaction.message.guild.get_member(user.id)
                if not member or not any(str(r.id) == target_role_id for r in member.roles):
                    continue
            if str(user.id) not in t["responded_user_ids"]:
                t["responded_user_ids"].append(str(user.id))
                changed = True
            break
    if changed:
        save_tracked(tracked)

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return
    tracked = load_tracked()
    changed = False
    for t in tracked:
        if t["message_id"] == str(reaction.message.id) and t["trigger_type"] == "reaction":
            if str(user.id) in t["responded_user_ids"]:
                t["responded_user_ids"].remove(str(user.id))
                changed = True
            break
    if changed:
        save_tracked(tracked)

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    # If a reply to a tracked message is deleted, unmark that user as responded
    if message.reference and message.reference.message_id:
        tracked = load_tracked()
        changed = False
        for t in tracked:
            if t["message_id"] == str(message.reference.message_id) and t["trigger_type"] == "reply":
                # Only unmark if they have no OTHER replies to this message still up
                # We can't check remaining messages from an event, so we re-scan channel history
                still_replied = False
                try:
                    async for msg in message.channel.history(limit=500):
                        if (
                            msg.reference is not None
                            and msg.reference.message_id == message.reference.message_id
                            and msg.author.id == message.author.id
                            and msg.id != message.id
                            and not msg.author.bot
                        ):
                            still_replied = True
                            break
                except (discord.HTTPException, AttributeError):
                    pass
                if not still_replied and str(message.author.id) in t["responded_user_ids"]:
                    t["responded_user_ids"].remove(str(message.author.id))
                    changed = True
                break
        if changed:
            save_tracked(tracked)
    if message.author.bot:
        await bot.process_commands(message)
        return
    if message.reference and message.reference.message_id:
        tracked = load_tracked()
        changed = False
        for t in tracked:
            if t["message_id"] == str(message.reference.message_id) and t["trigger_type"] == "reply":
                # If role-scoped, only count users who have that role
                target_role_id = t.get("target_role_id")
                if target_role_id:
                    if not any(str(r.id) == target_role_id for r in message.author.roles):
                        continue
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

        responded      = set(t["responded_user_ids"])
        target_role_id = t.get("target_role_id")
        members        = get_target_members(guild, target_role_id, responded)

        if not members:
            scope = f"role-scoped ({t.get('target_role_name', target_role_id)})" if target_role_id else "global"
            logger.info(f"All {scope} members responded for message {t['message_id']}.")
            continue

        verb     = "reply to" if t["trigger_type"] == "reply" else "react to"
        jump_url = f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}/{t['message_id']}"
        role_note = f" (as a member of **{t.get('target_role_name', 'the required role')}**)" if target_role_id else ""

        for member in members:
            try:
                if t.get("do_dm"):
                    await member.send(
                        f"Hey! You haven't {verb} a message in **{guild.name}**{role_note}.\n"
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
    if arg is None:
        await ctx.send("Usage: `!annoy_setup @Role` or `!annoy_setup clear`")
        return

    roles = load_roles()

    if arg.strip().lower() == "clear":
        roles.pop(str(ctx.guild.id), None)
        save_roles(roles)
        await ctx.send("✅ Allowed role cleared. Only the server owner can use `!annoy` now.")
        return

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
    if action is None or action.lower() == "help":
        await ctx.send(
            "**!annoy commands**\n"
            "Reply to a message first, then:\n"
            "`!annoy track <reply|reaction> <minutes> <#channel> [flags]`\n"
            "  • `reply|reaction` — watch for replies or reactions\n"
            "  • `minutes` — deadline (60=1hr, 1440=1day, 10080=1wk)\n"
            "  • `#channel` — where to ping\n"
            "  • `--role @Role` — only track members of this role (default: everyone)\n"
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
                deadline  = datetime.fromisoformat(t["deadline_at"].replace("Z", "+00:00"))
                jump_url  = f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}/{t['message_id']}"
                scope     = f"@{t['target_role_name']}" if t.get("target_role_id") else "everyone"
                lines.append(
                    f"• <{jump_url}> — **{t['trigger_type']}** — scope: **{scope}** — "
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
        msg_id = trigger_type
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

    # Parse flags — also grab role mentions from the message itself
    do_ping, do_dm, role_str = parse_flags(flags)

    # --role @Mention will show up in ctx.message.role_mentions too
    target_role = None
    if ctx.message.role_mentions:
        # The first role mention that isn't the bot-operator role
        op_roles   = load_roles()
        op_role_id = op_roles.get(str(ctx.guild.id))
        for r in ctx.message.role_mentions:
            if str(r.id) != op_role_id:
                target_role = r
                break
    elif role_str:
        target_role = resolve_role_from_str(ctx.guild, role_str)
        if not target_role:
            await ctx.send(f"Role `{role_str}` not found.")
            return

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

    # Seed users who already responded, filtered to role if specified
    target_role_id = str(target_role.id) if target_role else None

    def in_scope(user_id_str, member=None):
        if not target_role_id:
            return True
        if member:
            return any(str(r.id) == target_role_id for r in member.roles)
        m = ctx.guild.get_member(int(user_id_str))
        return m and any(str(r.id) == target_role_id for r in m.roles)

    responded_ids = []
    if trigger_type == "reaction":
        for r in tracked_message.reactions:
            async for u in r.users():
                if not u.bot and str(u.id) not in responded_ids:
                    member = ctx.guild.get_member(u.id)
                    if in_scope(str(u.id), member):
                        responded_ids.append(str(u.id))
    else:
        try:
            async for msg in tracked_message.channel.history(limit=500):
                if (
                    msg.reference is not None
                    and msg.reference.message_id == tracked_message.id
                    and not msg.author.bot
                    and str(msg.author.id) not in responded_ids
                ):
                    member = ctx.guild.get_member(msg.author.id)
                    if in_scope(str(msg.author.id), member):
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
        "target_role_id":     target_role_id,
        "target_role_name":   target_role.name if target_role else None,
        "responded_user_ids": responded_ids,
    })
    save_tracked(tracked)

    scope_note = f"members with role **{target_role.name}**" if target_role else "**everyone** in the server"
    await ctx.send(
        f"✅ Tracking this message for **{trigger_type}**.\n"
        f"👥 Scope: {scope_note}\n"
        f"⏰ Deadline: **{mins} minutes** from now ({deadline.strftime('%H:%M UTC')})\n"
        f"📣 Will {'ping in ' + ping_channel.mention if do_ping else 'not ping'} "
        f"and {'DM' if do_dm else 'not DM'} non-responders."
    )


def main():
    bot.run(token)

if __name__ == "__main__":
    main()
