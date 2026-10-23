"""
City Of Boombox -- audio verification bot (KILLA'S BOSS).

Checks whether a Roblox audio asset is actually usable from the game and reports
a plain reason when it isn't, so you can catch bad ids before you wire them into
the audio player.

Free tool commands -- runnable IN THE SERVER by members holding the "Trusted"
role (see TRUSTED_ROLES / TRUSTED_ROLE_IDS). Server-only for that tier: they do
NOT work in DMs or other servers by role alone -- except /copyr8, whose
membership is re-checked against the primary server from outside it.
/whitelist-ed users may run all of them (and the base commands) from ANYWHERE:
DMs, other servers, any channel. The owner can always run everything:
  /verify <asset id or url>   -- validate a Roblox AUDIO asset.
                                 Accepts a plain number, a rbxassetid:// url, or
                                 a roblox.com asset link. Replies with a green
                                 embed on success, a red one with a concrete
                                 reason on failure.
  /monitor <asset id>         -- watch a Roblox asset's moderation status.
  /analyze <file>             -- loudness (LUFS/peak) + waveform PNG + playable file.
  /copyr8 <file> [preset]     -- CopyR8 song checking tool for testing against a filter.
                                Auto-Fit = fast fixed-ladder song check.
                                Auto-Fit V2 = closest-sounding search, mildest first, with tremolo test.
                                Auto-Fit V3 = bundled macro chain + mixed search that
                                              reuses both playbooks until it's unreadable.
                                Auto-Fit V4 = clean stereo spectral cuts + V3's mixed search.
                                Subtle / Balanced / Stealth / Aggressive = fixed key/speed shifts.
  /lookup <file>              -- identify a song with Shazam: title, artist, album,
                                 release, cover art, and a link to the Shazam page.
                                 Full-track recognition, results cached.
  /download <url> [kind]      -- download a YouTube/web video (yt-dlp): audio mp3 or <=720p mp4.
  /groups                     -- list the Roblox groups this bot is verified for.

Staff commands (Killa feature commands, forked from typicaalusername/scope,
GPL-3.0), gated to the server Owner, Head Mods, Mods, or whitelisted users:
  /roblox <file>              -- two-pass ogg compression simulation.
  /verifygroup add|remove|unlock|delete [<group id>] -- manage which groups'
    audio is grantable (admin: owner / Head Mod / Mod). `remove` marks a group
    LOCKED (stays listed, can't grant).
  /ping                       -- host CPU/RAM/GPU/OS health check (admin).
  /reload                     -- re-sync the command tree (owner only).
  /whitelist add/remove/list  -- manage the extra staff allow-list (owner only).

Run it:

  double-click run_audio_bot.bat      (uses this folder's .venv)
  or  .venv/Scripts/python.exe audio_bot.py   (in the folder with a filled-in .env)
  One-time setup: py -3.10 -m venv .venv
                  .venv/Scripts/python.exe -m pip install -r requirements.txt

Env (same file as the main bot, or its own):
  DISCORD_TOKEN   -- token of the bot app you make for THIS bot
  GUILD_ID        -- server id for instant slash-command registration
"""

import asyncio
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import discord
from discord import app_commands
from dotenv import load_dotenv

# This is a SEPARATE Discord application ("KILLA'S BOSS") with its OWN token in THIS
# folder's .env. Load it first (overriding) so the KILLA'S BOSS token is authoritative,
# then let the parent video bot .env (VidBot) fill in any shared vars without overriding ours.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so `import verify_audio` works no matter how we're run

load_dotenv(os.path.join(HERE, ".env"), override=True)
load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))

import verify_audio  # noqa: E402
import grant_audio  # noqa: E402
import killa_tools  # noqa: E402
import shazam_backend  # noqa: E402
import songrec_backend  # noqa: E402

# The single recognition engine (/lookup). The /copyr8 auto-fit presets run
# their own ShazamIO + AcoustID + SongRec checks in killa_tools (find_undetectable /
# find_mildest); recognition results never drive their processing.
RECOGNITION = shazam_backend.AudioRecognition()

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "")
# The single server whose Head Mod / Mod roles are trusted for the ADMIN commands.
# A member with a matching role in a DIFFERENT server doesn't count. The owner and
# whitelisted staff (/whitelist) CAN use the base commands from anywhere (other
# guilds, DMs); only the owner can run the owner-only commands.
PRIMARY_GUILD_ID = int(GUILD_ID) if GUILD_ID.strip().isdigit() else None
# /verify may only be used in this channel (name or id). Default: bot-commands.
COMMAND_CHANNEL = os.getenv("COMMANDS_CHANNEL", "bot-commands").strip().lower()
# Roles allowed to run /verifygroup (substring match on role name). Default covers
# Owner, Head Mods, Mods.
VERIFYGROUP_ROLES = {
    r.strip().lower() for r in os.getenv("VERIFYGROUP_ROLES", "owner,head mod,mod").split(",")
    if r.strip()
}
# Exact Discord role IDs treated as staff (Mods / Head Mods), matched by role.id.
# More reliable than the name-substring above when role names don't contain "mod".
STAFF_ROLE_IDS = {
    int(x) for x in os.getenv("STAFF_ROLE_IDS", "").split(",") if x.strip().isdigit()
}
# Roles that may run the "free tool" commands (/verify, /monitor, /analyze, /copyr8,
# /lookup, /download, /groups) in the server. Substring match on role name;
# default is the "Trusted" role, so you hand these commands out with a role
# instead of /whitelist-ing each person. These commands are SERVER-ONLY.
TRUSTED_ROLES = {
    r.strip().lower() for r in os.getenv("TRUSTED_ROLES", "trusted").split(",")
    if r.strip()
}
# Exact Discord role ids treated as Trusted (matched by role.id), for when the
# role name doesn't contain "trusted".
TRUSTED_ROLE_IDS = {
    int(x) for x in os.getenv("TRUSTED_ROLE_IDS", "").split(",") if x.strip().isdigit()
}
# Discord user ids treated as the owner for the owner-only Killa commands
# (/reload, /whitelist). The server owner always counts; this list is extra.
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()
}
# Persisted allow-list of extra user ids granted staff access (managed by
# /whitelist). It is OR'd into is_authorized, on top of the role check, so a
# whitelisted user can use the base commands without holding a mod role.
STAFF_WHITELIST_FILE = os.path.join(HERE, "staff_whitelist.json")
# The Discord id of the application owner (detected at connect via
# bot.application.owner). Lets the owner run staff commands from a DM, where
# there are no guild roles to check.
APP_OWNER_ID = None

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Fill in .env first.")

GREEN = 0x4CAF50
RED = 0xE53935
PURPLE = 0x4E2A6E   # KILLA'S BOSS brand accent (deep violet, slightly brightened from #3A194C)

# Where command usage is appended (next to the scripts) so the console log is
# also a persistent file. Line format: "[YYYY-MM-DD HH:MM:SS] user Name (id) used /cmd ...".
USAGE_LOG = os.path.join(HERE, "usage.log")

# Cooldown (seconds) between /copyr8 auto-fit runs, per user. Auto-fit contacts
# Shazam repeatedly (rate-limits by device id) and takes a while, so it is
# throttled to stop people hammering it. 150s is the ceiling -- the env var
# can lower it but never raise it past that.
COPYR8_COOLDOWN_SECONDS = min(
    150, int(os.getenv("COPYR8_COOLDOWN_SECONDS", "150") or 150))

# Only /copyr8's Auto-Fit V3 uses this chain. An unset/blank override selects
# the bundled macro, installed into Audacity's Macros folder on the first run.
# COPYR8_V3_MACRO may still name a different installed macro (or a .txt path).
AUTOFIT_V3_MACRO = os.getenv("COPYR8_V3_MACRO", "").strip() or os.path.join(
    HERE, "macros", "CopyR8 AutoFit V3.txt")

# Same pattern for /copyr8's Auto-Fit V4 (static stereo EQ, no generated tones
# or sweeping modulation; the search layer chooses any pitch/tempo shifts).
AUTOFIT_V4_MACRO = os.getenv("COPYR8_V4_MACRO", "").strip() or os.path.join(
    HERE, "macros", "CopyR8 AutoFit V4.txt")

# /monitor polling. Polls each watched asset every MONITOR_INTERVAL_SECONDS, and
# gives up after MONITOR_TIMEOUT_SECONDS (a Roblox moderation review rarely takes
# longer; the interaction token also decays, so a short window is the honest cap).
MONITOR_INTERVAL_SECONDS = float(os.getenv("MONITOR_INTERVAL_SECONDS", "6") or 6)
MONITOR_TIMEOUT_SECONDS = float(os.getenv("MONITOR_TIMEOUT_SECONDS", "900") or 900)


def _log(line):
    """Write a log line to the command-prompt console and append it to the usage log.

    The console line is what you see when you run the bot (``python audio_bot.py``);
    the same line is appended to usage.log so it survives the session too.
    """
    full = time.strftime("[%Y-%m-%d %H:%M:%S] ") + line
    print(full, flush=True)
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(full + "\n")
    except OSError:
        pass


# Session bookkeeping. Everything writes to the ONE file (usage.log) -- no per-session
# files -- and each command is logged exactly once (in on_interaction). _cmd_count
# feeds the end-of-session summary so the SESSION ENDED line is never duplicated.
_session_start = time.time()
_cmd_count = 0
_session_end_logged = False


def _log_session_end():
    """Append a single end-of-session summary line to usage.log (idempotent).

    Guarded so a shutdown that calls this more than once (e.g. an error path plus
    a clean exit) can't write two 'SESSION ENDED' lines for one run.
    """
    global _session_end_logged
    if _session_end_logged:
        return
    _session_end_logged = True
    dur = int(time.time() - _session_start)
    _log(f"SESSION ENDED - {_cmd_count} command(s) over {dur}s")


class AudioBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Mirror the owner's presence: the bot shows online only while the owner is
        # Online or on Do Not Disturb, and appears offline (invisible) when the owner
        # is Idle or Offline. Watching another user's status needs the two PRIVILEGED
        # intents below, which must ALSO be enabled in the Developer Portal (Bot ->
        # Privileged Gateway Intents, "Presence Intent" + "Server Members Intent").
        # Off by default so the bot always starts and stays up; set MIRROR_PRESENCE=1
        # AFTER enabling those intents. Without the intents, Discord refuses the login
        # and the process would exit (which closes the run window) -- so we default off.
        self._mirror_presence = os.getenv(
            "MIRROR_PRESENCE", "0").strip().lower() in ("1", "true", "yes", "on")
        self._presence_poll_s = max(15, int(os.getenv("MIRROR_PRESENCE_POLL", "30") or 30))
        if self._mirror_presence:
            intents.members = True
            intents.presences = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._presence_task = None
        self._applied_status = None

    # -- presence mirroring ------------------------------------------------- #
    def _owner_ids(self):
        ids = set(OWNER_IDS)
        if APP_OWNER_ID:
            ids.add(APP_OWNER_ID)
        return ids

    def _owner_member(self):
        """A guild member object for the owner (app owner or OWNER_IDS), or None."""
        for uid in self._owner_ids():
            for guild in self.guilds:
                member = guild.get_member(uid)
                if member:
                    return member
        return None

    @staticmethod
    def _target_status(owner_status):
        # Online and DND count as active -> the bot is online. Idle/Offline turn the
        # bot invisible (it looks offline, and off-limits while the owner is away).
        if owner_status in (discord.Status.online, discord.Status.dnd):
            return discord.Status.online
        return discord.Status.invisible

    async def _apply_owner_presence(self):
        """One pass: read the owner's status and flip this bot's own status to match.

        Fail-open: if the owner can't be resolved (no shared guild, or the owner id
        isn't a member anywhere), we leave the bot as-is rather than accidentally
        hiding it. Logs what it's doing so the behaviour is visible in the console.
        """
        if not self._mirror_presence:
            return
        try:
            member = self._owner_member()
            if member is None:
                self._log_once_mirror(
                    f"presence: can't see owner ids {sorted(self._owner_ids()) or 'unknown'} "
                    "(no shared guild) -- leaving bot visible")
                return
            target = self._target_status(member.status)
            if target != self._applied_status:
                _log(f"presence: owner {member.id} is {member.status.name} -> bot "
                     f"{'online' if target == discord.Status.online else 'offline'}")
                await self.change_presence(status=target)
                self._applied_status = target
        except Exception as e:  # noqa: BLE001
            _log(f"mirror presence error: {e}")

    async def _mirror_owner_presence(self):
        """Poll the owner's status and flip this bot's own presence to match.

        The on_presence_update handler reacts instantly; this loop is a safety net
        that catches a missed update within _presence_poll_s. Runs until the client
        closes. Started in setup_hook so it can't be skipped by a failed sync.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            await self._apply_owner_presence()
            await asyncio.sleep(self._presence_poll_s)

    async def on_presence_update(self, before, after):
        """React instantly to a member's status change (fires for the owner too)."""
        if self._mirror_presence and after.id in self._owner_ids():
            await self._apply_owner_presence()

    def _log_once_mirror(self, msg):
        if not getattr(self, "_mirror_warned", False):
            self._mirror_warned = True
            _log(msg)

    async def setup_hook(self):
        # Make every command usable in a guild, in the bot's DMs, and in private
        # channels. It is registered GLOBALLY only -- no guild-scoped copy. A
        # global command shows exactly once in every server the bot is in, so
        # also registering a guild-scoped copy of the same name would make it
        # appear TWICE in a server (that was the "doubled commands" bug).
        from discord.app_commands import AppCommandContext, AppInstallationType

        for cmd in self.tree.get_commands():
            cmd.allowed_contexts = AppCommandContext(
                guild=True, dm_channel=True, private_channel=True)
            # Both guild and user install. The commands are registered globally
            # only (no guild-scoped copy), so user install does NOT create a
            # second entry -- it just lets the same global command be used as a
            # user app (DMs / private) as well as in a server. See the "doubled
            # commands" note below: that was global + guild registrations, not
            # the install type.
            cmd.allowed_installs = AppInstallationType(guild=True, user=True)

        # Start the presence watcher HERE (setup_hook runs right after login, before
        # on_ready / command sync), so a slow or failing command sync can never leave
        # the bot running without presence mirroring.
        if self._mirror_presence and self._presence_task is None:
            self._presence_task = asyncio.create_task(self._mirror_owner_presence())

        # The single global sync is done after 'ready' (see _sync_all_guilds),
        # and any stale guild-scoped commands left over from an older config are
        # deleted there (_remove_guild_command_duplicates) so the server shows
        # one clean set.


bot = AudioBot()


def _brand_footer(embed, extra=None):
    """Apply the branded KILLA'S BOSS footer (bot name + avatar icon)."""
    icon = None
    try:
        icon = bot.user.display_avatar.url if bot.user else None
    except Exception:
        icon = None
    text = "KILLA'S BOSS" + (f" • {extra}" if extra else "")
    embed.set_footer(text=text, icon_url=icon)
    return embed


# Promotional links shown on the branded cards (same as /ping advertises).
_ADVERT_DISCORD = "https://discord.gg/AWFAFSCbXQ"
_ADVERT_GROUP = ("https://www.roblox.com/communities/101224973/"
                 "KILLAS-HUB-3-0#!/about")


def _advertise_fields(embed):
    """Add the KILLA'S BOSS promotion fields (Discord invite + Roblox group)."""
    embed.add_field(name="Discord",
                    value=f"[Join the server]({_ADVERT_DISCORD})", inline=False)
    embed.add_field(name="Roblox Group",
                    value=f"[KILLA'S HUB 3.0]({_ADVERT_GROUP})", inline=False)
    return embed


def _whitelist_dm_embed():
    """Private message sent to a user who has been whitelisted."""
    embed = discord.Embed(
        title="You've been verified",
        description=(
            "You can now use **KILLA'S BOSS** as an app on your Discord account.\n\n"
            "Add it from the bot profile or under **Discord -> User Settings -> Apps**, "
            "then run slash commands in a DM with the bot or in your own server."
        ),
        color=GREEN,
    )
    embed.add_field(
        name="Roblox Group",
        value=f"[KILLA'S HUB 3.0]({_ADVERT_GROUP})",
        inline=False,
    )
    _brand_footer(embed, "staff whitelist")
    return embed


def _build_groups_embed():
    """Build the /groups card from the currently supported (verified) groups.

    Names come from the runtime JSON store where available; any group id with no
    stored name is resolved via a public group lookup (best effort). Runs off the
    event loop because a lookup can hit the network. A group shows a LOCKED badge
    when it's marked locked (via /verifygroup remove) or Roblox reports it locked.
    """
    ids = grant_audio.all_supported_ids()
    named = {g["id"]: (g.get("name") or "?")
             for g in grant_audio._load_dynamic_groups()}
    locks = grant_audio.effective_locks()
    entries = []
    for gid in sorted(ids):
        name = named.get(gid)
        if not name:
            try:
                name = grant_audio.get_group_info(gid).get("name")
            except Exception:
                name = None
        entries.append((gid, name, bool(locks.get(gid))))

    if entries:
        lines = []
        for gid, name, locked in entries:
            line = f"**{name}** — `{gid}`" if name else f"`{gid}`"
            if locked:
                line += " — **LOCKED**"
            lines.append(line)
        embed = discord.Embed(
            title="Verified Groups",
            description=(
                "These Roblox groups are supported, so this bot can grant their "
                "audio to your experience. Groups marked **LOCKED** can no longer "
                "be granted (a locked group can't have new audio)."
            ),
            color=PURPLE,
        )
        embed.add_field(name="Groups", value="\n".join(lines), inline=False)
    else:
        embed = discord.Embed(
            title="Verified Groups",
            description=(
                "No groups are supported yet. Run `/verifygroup add <group id>` to "
                "verify a Roblox group so the bot can grant its audio."
            ),
            color=PURPLE,
        )
    embed.add_field(
        name="How to add",
        value=("Ask an admin to run `/verifygroup add <group id>` to support a new "
               "group."),
        inline=False,
    )
    _advertise_fields(embed)
    _brand_footer(embed, "verified groups")
    return embed


def _in_primary_guild(interaction: discord.Interaction) -> bool:
    """True when the command ran in the single trusted server (GUILD_ID)."""
    return interaction.guild is not None and interaction.guild.id == PRIMARY_GUILD_ID


def is_owner(interaction: discord.Interaction) -> bool:
    """The bot owner: the app owner, any id in OWNER_IDS, or the owner of the
    primary guild. The owner is ALWAYS allowed -- any server, DMs, any channel,
    even when the bot is showing offline (owner away)."""
    if APP_OWNER_ID and interaction.user.id == APP_OWNER_ID:
        return True
    if interaction.user.id in OWNER_IDS:
        return True
    if _in_primary_guild(interaction) and interaction.guild.owner_id == interaction.user.id:
        return True
    return False


def _has_mod_role(member: discord.Member) -> bool:
    """True when a guild member holds a Head Mod / Mod role.

    Matched by role id (STAFF_ROLE_IDS) or by role name against VERIFYGROUP_ROLES
    (default 'owner','head mod','mod').
    """
    for role in getattr(member, "roles", []):
        if role.id in STAFF_ROLE_IDS:
            return True
        low = role.name.lower()
        for token in VERIFYGROUP_ROLES:
            if token in low:
                return True
    return False


def is_mod(interaction: discord.Interaction) -> bool:
    """A Head Mod / Mod of the PRIMARY guild. Roles in a different server don't count."""
    return _in_primary_guild(interaction) and _has_mod_role(interaction.user)


def bot_is_offline() -> bool:
    """True when presence mirroring turned the bot invisible (the owner is away).

    While the bot is showing offline, commands lock down to the owner only. If
    presence mirroring is disabled this always returns False (no lock).
    """
    return bool(bot._mirror_presence and bot._applied_status == discord.Status.invisible)


def is_whitelisted(interaction: discord.Interaction) -> bool:
    """True when the user is on the extra staff allow-list (/whitelist add).

    Grants the BASE (non-admin, non-owner) commands AND the free-tool commands
    from anywhere -- the primary guild, another server, or the bot's DMs -- so
    a whitelisted person can use the bot even outside the server. It does NOT
    grant admin commands (/verifygroup, /ping) or owner-only commands
    (/reload, /whitelist).
    """
    return interaction.user.id in _read_staff_whitelist()


def is_authorized(interaction: discord.Interaction) -> bool:
    """Master gate for every base (non-admin) command.

    Allowed: the owner (always -- any server/DM/channel, even while the bot is
    offline), a whitelisted staff member (any server/DM/channel, even while the
    bot is showing offline -- /whitelist is a direct grant), or a Head Mod / Mod
    of the primary guild (any channel in that server, while the bot is online).
    Everyone else is denied. Admin commands check is_admin_authorized instead,
    so a whitelisted user can't use those.
    """
    if is_owner(interaction):
        return True
    if is_whitelisted(interaction):
        return True
    if bot_is_offline():
        return False
    return is_mod(interaction)


def is_admin_authorized(interaction: discord.Interaction) -> bool:
    """Gate for the admin commands (/verifygroup, /ping).

    Stricter than is_authorized: the owner always, plus a Head Mod / Mod of the
    primary guild while the bot is online. A whitelisted (non-mod) user is NOT
    admin-authorized -- the whitelist only unlocks the base commands.
    """
    if is_owner(interaction):
        return True
    if bot_is_offline():
        return False
    return is_mod(interaction)


# The "free tool" commands: runnable in the PRIMARY server by members holding a
# Trusted role (see TRUSTED_ROLES), instead of requiring /whitelist or a staff
# role. They are SERVER-ONLY -- not in DMs or other servers. Everything not listed
# here stays gated behind is_authorized (owner / whitelist / Head Mod / Mod). Keep
# this in sync with the command list in the module docstring.
FREE_COMMANDS = {
    "verify", "monitor", "analyze", "copyr8", "lookup", "download", "groups",
}

# The free commands the TRUSTED-ROLE tier may use outside the primary server
# (in DMs and other servers). /whitelist-ed users don't need to be listed
# here: their id-based grant already covers every free command anywhere. A DM
# / other-server interaction carries a User, which has no role list, so the
# Trusted / Mod check is re-resolved against the primary server's member list
# instead (see _outside_primary_trusted).
ANYWHERE_FREE_COMMANDS = {"copyr8"}


def is_free_command(interaction: discord.Interaction) -> bool:
    """True when the command that was run is in the free-tool tier."""
    cmd = getattr(interaction, "command", None)
    if cmd is None:
        return False
    root = getattr(cmd, "root_parent", None)
    name = getattr(root, "name", None) or getattr(cmd, "name", None)
    return name in FREE_COMMANDS


def _has_trusted_role(member: discord.Member) -> bool:
    """True when a guild member holds a Trusted role.

    Matched by role id (TRUSTED_ROLE_IDS) or by role name against TRUSTED_ROLES
    (default 'trusted'). This is the membership-based grant for the free-tool
    commands, so they can be handed out with a role rather than /whitelist-ing
    each person.
    """
    for role in getattr(member, "roles", []):
        if role.id in TRUSTED_ROLE_IDS:
            return True
        low = role.name.lower()
        for token in TRUSTED_ROLES:
            if token in low:
                return True
    return False


def _is_anywhere_free_command(interaction: discord.Interaction) -> bool:
    """True when the command is one of ANYWHERE_FREE_COMMANDS -- the free-tool
    commands the trusted tier may also run outside the primary server."""
    cmd = getattr(interaction, "command", None)
    if cmd is None:
        return False
    root = getattr(cmd, "root_parent", None)
    name = getattr(root, "name", None) or getattr(cmd, "name", None)
    return name in ANYWHERE_FREE_COMMANDS


async def _outside_primary_trusted(user_id: int) -> bool:
    """Re-check a user's Head Mod / Mod / Trusted roles against the PRIMARY
    server while they are outside it (DMs / another server).

    A DM or other-server interaction carries a User, which has no role list, so
    membership is looked up in the primary server instead: member cache first,
    then a single API fetch. Fail-closed -- when the server or the member can't
    be resolved, access is denied.
    """
    guild = bot.get_guild(PRIMARY_GUILD_ID)
    if guild is None:
        return False
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
    return _has_mod_role(member) or _has_trusted_role(member)


async def _deny_trusted(interaction):
    if not _in_primary_guild(interaction):
        if _is_anywhere_free_command(interaction):
            await interaction.response.send_message(
                "To use that outside the server you need the **Trusted** role "
                "(or a whitelisted / staff role) in the main server.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "These commands only work inside the server -- not in DMs or other "
                "servers.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "You need the **Trusted** role in this server to use that command.",
            ephemeral=True)


async def _require_trusted(interaction):
    """Gate for the free-tool commands (the FREE_COMMANDS set).

    These run in the PRIMARY server for members holding a Trusted role -- an
    easier way to hand them out than /whitelist-ing each person. /whitelist-ed
    users may run them (and every other base command) from ANYWHERE -- DMs and
    other servers included; the whitelist is a direct id-based grant. Commands
    in ANYWHERE_FREE_COMMANDS (just /copyr8) additionally work anywhere for
    Trusted-role members, with membership re-checked against the primary
    server from outside it. The owner is always allowed (any
    server/DM/channel). The away-lockdown applies to trusted-role members and
    mods; /whitelist-ed users bypass it (that's what the whitelist is for).
    If a non-listed command calls this by mistake it falls back to the normal
    staff gate, so the tier can't accidentally leak access.
    """
    if not is_free_command(interaction):
        return await _require(interaction)  # safety net; not expected
    if is_owner(interaction):
        return True
    if bot_is_offline() and not is_whitelisted(interaction):
        await _deny_staff(interaction)
        return False
    if not _in_primary_guild(interaction):
        # Whitelisted users are granted the base AND free commands from
        # anywhere by their user id -- that is what the whitelist is for.
        # Trusted-role members can only be verified inside the primary server
        # (a DM user carries no roles), so outside it just the commands in
        # ANYWHERE_FREE_COMMANDS work for them, with membership re-checked
        # against the primary server (see _outside_primary_trusted).
        if is_whitelisted(interaction) or (
                _is_anywhere_free_command(interaction)
                and await _outside_primary_trusted(interaction.user.id)):
            return True
        await _deny_trusted(interaction)
        return False
    if is_mod(interaction) or is_whitelisted(interaction) or _has_trusted_role(interaction.user):
        return True
    await _deny_trusted(interaction)
    return False


def in_commands_channel(interaction: discord.Interaction) -> bool:
    """True when the command ran in the configured command channel.

    A DM (interaction.guild is None) has no guild channel, so it always passes --
    the owner gate still applies on top."""
    if interaction.guild is None:
        return True
    channel = interaction.channel
    if channel is None or getattr(channel, "name", None) is None:
        return False
    if COMMAND_CHANNEL.isdigit():
        return str(channel.id) == COMMAND_CHANNEL
    return channel.name.lower() == COMMAND_CHANNEL


def _read_staff_whitelist_doc() -> dict:
    try:
        with open(STAFF_WHITELIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def _read_staff_whitelist() -> set:
    data = _read_staff_whitelist_doc()
    try:
        return {int(x) for x in (data.get("user_ids", []) or [])}
    except (ValueError, TypeError):
        return set()


def _read_pending_whitelist_dms() -> set:
    data = _read_staff_whitelist_doc()
    try:
        return {int(x) for x in (data.get("pending_dm_ids", []) or [])}
    except (ValueError, TypeError):
        return set()


def _write_staff_whitelist(user_ids: set, pending_dm_ids: set = None):
    if pending_dm_ids is None:
        pending_dm_ids = _read_pending_whitelist_dms() & set(user_ids)
    data = {"user_ids": sorted(user_ids)}
    if pending_dm_ids:
        data["pending_dm_ids"] = sorted(pending_dm_ids)
    with open(STAFF_WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def _resolve_whitelist_target(interaction, user, user_id):
    """Return (uid, discord_user_or_None, error_or_None) for /whitelist.

    A resolved `user` (mention) wins; otherwise `user_id` (a raw id string or
    pasted `<@id>` mention) is parsed and looked up via guild/client cache and
    fetch_user. A failed lookup is NOT an error here -- callers that need the
    user object (add + DM) check for None, so `remove` can still work by id for
    accounts the bot can't fetch.
    """
    if user is not None:
        return user.id, user, None
    raw = (user_id or "").strip().strip("`").strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].lstrip("!&").strip()
    elif raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return None, None, None
    if not raw.isdigit():
        return None, None, (
            "`user_id` must be a number, e.g. "
            "`/whitelist add user_id: 123456789012345678`.")
    uid = int(raw)
    guild = getattr(interaction, "guild", None)
    if guild is not None:
        get_member = getattr(guild, "get_member", None)
        if callable(get_member):
            cached_m = get_member(uid)
            if cached_m is not None:
                return uid, cached_m, None
    get_user = getattr(interaction.client, "get_user", None)
    if callable(get_user):
        cached_u = get_user(uid)
        if cached_u is not None:
            return uid, cached_u, None
    try:
        return uid, await interaction.client.fetch_user(uid), None
    except Exception:
        return uid, None, None


# ---- compatibility shims so existing call sites stay simple ------------------- #

def is_allowed(interaction: discord.Interaction) -> bool:
    return is_authorized(interaction)


def is_verifygroup_authorized(interaction: discord.Interaction) -> bool:
    return is_authorized(interaction)


def is_staff(interaction: discord.Interaction) -> bool:
    return is_authorized(interaction)


def is_admin_command_authorized(interaction: discord.Interaction) -> bool:
    # Admin commands: the owner always, plus a primary-guild Head Mod / Mod while
    # the bot is online. Truly owner-only subcommands (/reload, /whitelist) check
    # is_owner again inside their handler. Whitelisted non-mods are NOT admin-
    # authorized (that's is_authorized).
    return is_admin_authorized(interaction)


async def _deny_staff(interaction):
    if bot_is_offline():
        await interaction.response.send_message(
            "The bot is currently offline (the owner is away) -- only the owner can "
            "use it right now.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Only the bot owner, a whitelisted staff member, or a Head Mod / Mod of "
            "this server can use this.", ephemeral=True)


async def _deny_admin(interaction):
    await interaction.response.send_message(
        "Only the owner can use this in DMs; inside this server only the Owner, "
        "Head Mod, or Mod roles can.", ephemeral=True)


async def _deny_channel(interaction):
    await interaction.response.send_message(
        f"This command can only be used in **#{COMMAND_CHANNEL}**.", ephemeral=True
    )


def channel_ok(interaction: discord.Interaction) -> bool:
    """Authorized users (owner, or a primary-guild Head Mod / Mod) may run in any
    channel; everyone else is denied outright by the gate before this is reached."""
    return is_authorized(interaction)


async def _require(interaction, staff=True):
    """Return True when the caller may run a Killa feature, else send a denial."""
    if not is_authorized(interaction):
        await _deny_staff(interaction)
        return False
    return True


async def _require_admin(interaction):
    """Gate for the admin commands: owner always, primary-guild Head Mod / Mod online.

    Whitelisted users (non-mods) pass is_authorized but NOT this gate, so they can
    use the base commands but can't run /verifygroup or /ping.
    """
    if not is_admin_authorized(interaction):
        await _deny_admin(interaction)
        return False
    return True


async def _require_owner(interaction):
    """Strict owner-only gate (used by /reload and /whitelist)."""
    if not is_owner(interaction):
        await interaction.response.send_message(
            "This command is owner-only.", ephemeral=True)
        return False
    return True


async def _save_attachment(workdir, attachment):
    """Download a Discord attachment to disk, return the local path."""
    data = await attachment.read()
    ext = os.path.splitext(attachment.filename or "a.mp3")[1] or ".mp3"
    path = os.path.join(workdir, f"input_{int(time.time() * 1000)}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


async def _safe_send(fn, *args, **kwargs):
    """Await a send call but never let a stale interaction crash a bg task."""
    try:
        await fn(*args, **kwargs)
    except Exception:
        pass


@bot.tree.command(
    name="verify",
    description="Check whether a Roblox audio asset can be used in-game (and why not).",
)
@app_commands.describe(
    asset="The audio asset id, rbxassetid:// url, or roblox.com asset link to check.",
)
async def verify_slash(interaction: discord.Interaction, asset: str):
    if not await _require_trusted(interaction):
        return

    await interaction.response.defer(thinking=True)
    try:
        asset_id = await asyncio.to_thread(verify_audio.extract_asset_id, asset)
    except verify_audio.AssetError as e:
        await interaction.followup.send(
            embed=discord.Embed(title="Not a valid id", color=RED,
                                description=str(e)),
            ephemeral=True,
        )
        return

    # Roblox calls are synchronous + rate-limited; run off the event loop.
    try:
        result = await asyncio.to_thread(grant_audio.verify, asset_id)
    except grant_audio.GrantError as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Audio rejected ({asset_id})",
                color=RED,
                description=f"**Reason:** {e}",
            ),
            ephemeral=True,
        )
        return
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Verification error ({asset_id})",
                color=discord.Color.orange(),
                description=f"Something went wrong talking to Roblox: `{e}`",
            ),
            ephemeral=True,
        )
        return

    title = f"Verified & granted ({asset_id})" if result.get("granted") else f"Audio OK ({asset_id})"
    embed = discord.Embed(
        title=title, color=GREEN,
        description=result["message"] +
        "\n\nThis audio is now **cached as verified** across every server, so any "
        "player gets instant playback (no re-check).")

    embed.set_footer(text=f"Verified by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="verifygroup",
    description="Verify (add), remove, or manage the Roblox groups this bot grants audio for.",
)
@app_commands.describe(
    action="add, remove, unlock, or delete.",
    group="The Roblox group id to add/remove. Use /groups to view the list.",
)
async def verifygroup_slash(interaction: discord.Interaction,
                            action: str, group: str = None):
    if not await _require_admin(interaction):
        return

    await interaction.response.defer(thinking=True, ephemeral=False)
    action = (action or "").strip().lower()
    # Back-compat: /verifygroup <id> (no action) is treated as add.
    if action not in ("add", "remove", "unlock", "delete") and group is None:
        if action.replace("-", "").isdigit():
            group = action
            action = "add"

    if action in ("add", "remove", "unlock", "delete"):
        if not group:
            embed = discord.Embed(
                title="Pick a group",
                description=(
                    "Pass the group's numeric id to `add`, `remove`, `unlock`, or "
                    "`delete`, e.g. `/verifygroup remove 15611107`."
                ),
                color=PURPLE,
            )
            _brand_footer(embed, "verifygroup")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        try:
            gid = int(group.strip())
        except (ValueError, TypeError):
            embed = discord.Embed(
                title="Not a valid group id", color=RED,
                description="Pass the group's numeric id, e.g. `15611107`.")
            _brand_footer(embed, "verifygroup")
            await interaction.followup.send(embed=embed, ephemeral=False)
            return

        if action == "remove":
            await _verifygroup_remove(interaction, gid)
            return
        if action == "unlock":
            await _verifygroup_unlock(interaction, gid)
            return
        if action == "delete":
            await _verifygroup_delete(interaction, gid)
            return

        # ---- add (was the whole of the old /verifygroup) ----
        try:
            result = await asyncio.to_thread(grant_audio.verify_group, gid)
        except grant_audio.GrantError as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"Could not verify group ({gid})",
                    color=RED,
                    description=f"**Reason:** {e}",
                ),
                ephemeral=False,
            )
            return
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"Group verify error ({gid})",
                    color=discord.Color.orange(),
                    description=f"Something went wrong: `{e}`",
                ),
                ephemeral=False,
            )
            return

        if result.get("captcha"):
            embed = discord.Embed(
                title=f"CAPTCHA needed to join group ({result['id']})",
                color=0xFB8C00,
                description=(
                    f"**{result['name']}** (id **{result['id']}**) was added to the "
                    f"supported groups list, so the bot can grant its audio.\n\n"
                    f"{result['message']}\n\n"
                    f"Complete the CAPTCHA manually (log into Roblox as the bot account "
                    f"and join the group), then run `/verifygroup add {result['id']}` again."
                ),
            )
        elif result.get("joined") or result.get("already_member"):
            embed = discord.Embed(
                title=f"Group verified & joined ({result['id']})",
                color=GREEN,
                description=(
                    f"**{result['name']}** (id **{result['id']}**) was added to the "
                    f"supported groups.\n\n{result.get('message') or ''}\n\n"
                    f"The bot can now grant this group's audio to your game "
                    f"(universe **{grant_audio.GRANT_UNIVERSE_ID}**)."
                ),
            )
        else:
            embed = discord.Embed(
                title=f"Group added, but not joined ({result['id']})",
                color=0xFB8C00,
                description=(
                    f"**{result['name']}** (id **{result['id']}**) was added to the "
                    f"supported groups list, but the account could **not** join the group.\n\n"
                    f"{result['message']}\n\n"
                    f"Grants on this group's audio may be refused until the account is a "
                    f"member. If the .env ROBLOX_COOKIE is stale, refresh it and re-run."
                ),
            )
        embed.set_footer(text=f"Verified by {interaction.user.display_name}")
        # posted in-channel (ephemeral=False) so the person asking can see it
        await interaction.followup.send(embed=embed, ephemeral=False)
        return

    # Unknown action.
    embed = discord.Embed(
        title="Add, remove, unlock, or delete",
        description=(
            "Pass `action` as `add`, `remove`, `unlock`, or `delete` "
            "(e.g. `/verifygroup add 15611107`). Use `/groups` to view the list."
        ),
        color=PURPLE,
    )
    _brand_footer(embed, "verifygroup")
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _verifygroup_remove(interaction: discord.Interaction, gid: int):
    """Handle `/verifygroup remove <id>` -- marks the group LOCKED (stays on the
    list, but can no longer be granted) instead of deleting it."""
    try:
        result = await asyncio.to_thread(grant_audio.mark_group_locked, gid)
    except grant_audio.GrantError as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Could not lock group ({gid})",
                color=RED,
                description=f"**Reason:** {e}",
            ), ephemeral=False)
        return
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Group lock error ({gid})",
                color=discord.Color.orange(),
                description=f"Something went wrong: `{e}`",
            ), ephemeral=False)
        return

    if result and result.get("name"):
        embed = discord.Embed(
            title=f"Group locked ({result['id']})",
            color=0xFB8C00,
            description=(
                f"**{result['name']}** (id **{result['id']}**) is now **LOCKED**.\n\n"
                f"It stays on the verified list but can no longer be granted audio, "
                f"and it can't have new audio made in it.\n\n"
                f"The in-game Groups tab has been updated live. Use "
                f"`/verifygroup unlock {result['id']}` to grant it again."
            ),
        )
    else:
        embed = discord.Embed(
            title=f"Group not in supported list ({gid})",
            color=0xFB8C00,
            description=f"Group `{gid}` is not a supported group, so nothing was changed.",
        )
    embed.set_footer(text=f"Locked by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=False)


async def _verifygroup_unlock(interaction: discord.Interaction, gid: int):
    """Handle `/verifygroup unlock <id>` -- clears the LOCKED flag."""
    try:
        result = await asyncio.to_thread(grant_audio.unlock_supported_group, gid)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Group unlock error ({gid})",
                color=discord.Color.orange(),
                description=f"Something went wrong: `{e}`",
            ), ephemeral=False)
        return

    if result and result.get("name"):
        embed = discord.Embed(
            title=f"Group unlocked ({result['id']})",
            color=GREEN,
            description=(
                f"**{result['name']}** (id **{result['id']}**) is no longer locked.\n\n"
                f"The bot can grant its audio again."
            ),
        )
    else:
        embed = discord.Embed(
            title=f"Group not in supported list ({gid})",
            color=0xFB8C00,
            description=f"Group `{gid}` is not a supported group, so nothing was changed.",
        )
    embed.set_footer(text=f"Unlocked by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=False)


async def _verifygroup_delete(interaction: discord.Interaction, gid: int):
    """Handle `/verifygroup delete <id>` -- removes the group from the list entirely."""
    try:
        result = await asyncio.to_thread(grant_audio.delete_supported_group, gid)
    except grant_audio.GrantError as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Could not delete group ({gid})",
                color=RED,
                description=f"**Reason:** {e}",
            ), ephemeral=False)
        return
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Group delete error ({gid})",
                color=discord.Color.orange(),
                description=f"Something went wrong: `{e}`",
            ), ephemeral=False)
        return

    if result and result.get("name"):
        embed = discord.Embed(
            title=f"Group deleted ({result['id']})",
            color=RED,
            description=(
                f"**{result['name']}** (id **{result['id']}**) was removed from the "
                f"supported groups list.\n\nThe in-game Groups tab has been updated live."
            ),
        )
    else:
        embed = discord.Embed(
            title=f"Group not in supported list ({gid})",
            color=0xFB8C00,
            description=f"Group `{gid}` is not a supported group, so nothing was changed.",
        )
    embed.set_footer(text=f"Deleted by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=False)


# --------------------------------------------------------------------------- #
# Killa feature commands (forked from typicaalusername/scope, GPL-3.0, gated to
# Owner / Head Mods / Mods / owner-whitelisted users)
# --------------------------------------------------------------------------- #
active_monitors = {}  # asset id -> asyncio.Task
_watcher_display = {}  # asset id -> display name (for the 'list' view)

# /copyr8 auto-fit throttle. Only one auto-fit may run at a time (it hits
# Shazam, which rate-limits by device id), and each user is cooled down
# between runs.
_copyr8_autofit_running = False
_copyr8_cooldown_until = {}  # user id -> epoch after which they may auto-fit again


COPYR8_PROGRESS_REFRESH_SECONDS = 1.5

# Discord answers these when an edit/send can never succeed: 401 = the
# interaction webhook token expired (15 minutes after the command), 403 = the
# bot lacks permission in that channel, 404 = the message/webhook is gone.
# Retrying them only produces a cascade of identical errors in the log.
_DISCORD_FATAL_STATUSES = (401, 403, 404)


def _format_wait(seconds):
    """'42s' / '2m 05s' for a live countdown."""
    seconds = max(0, int(round(seconds)))
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def _copyr8_pacing_line(status):
    """One status line for the API pace-keeper, or '' when nothing is pending."""
    if not status:
        return ""
    rl = status.get("rate_limit_remaining") or 0.0
    if rl > 0.0:
        streak = status.get("consecutive_limits") or 0
        return (f"\u23f3 Shazam rate limit (API Cooldown) -- cool-down resets in "
                f"**{_format_wait(rl)}**"
                + (f" (throttle #{streak} in a row)" if streak > 1 else ""))
    breath = status.get("breath_remaining") or 0.0
    if breath >= 0.5:
        return (f"\u23f1 API pacing (API Cooldown) -- next probe in **{breath:.1f}s** "
                f"(spacing {status.get('breath_seconds', 0.0):.1f}s)")
    return ""


def _copyr8_progress_content(label, lines, status):
    content = f"{label}: " + (lines[-1] if lines else "starting...")
    pacing = _copyr8_pacing_line(status)
    if pacing:
        content += "\n" + pacing
    return content[:2000]


async def _update_copyr8_progress(msg, lines, label="Auto-fit",
                                  status_fn=None,
                                  interval=COPYR8_PROGRESS_REFRESH_SECONDS):
    """Keep the deferred message updated with the latest auto-fit step.

    Runs alongside the worker thread; cancels when the fit is done. Refreshes
    every `interval` seconds (1.5s) so the live API pacing / rate-limit reset
    countdown from `status_fn()` (killa_tools.pacing_status) ticks down in
    Discord, and only edits when the rendered text actually changed.

    A 401 (expired interaction token), 403 (no permission) or 404 (message
    gone) can never succeed on retry, so the updater stops quietly instead of
    cascading errors. Transient failures (429 / 5xx / network) are skipped and
    the next tick tries again.
    """
    if status_fn is None:
        status_fn = getattr(killa_tools, "pacing_status", None)
    last = None
    while True:
        try:
            status = status_fn() if status_fn else None
        except Exception:  # noqa: BLE001 -- a status read must never kill the updater
            status = None
        content = _copyr8_progress_content(label, lines, status)
        if content != last:
            try:
                await msg.edit(content=content)
                last = content
            except discord.HTTPException as e:
                if getattr(e, "status", None) in _DISCORD_FATAL_STATUSES:
                    _log(f"copyr8 progress updates stopped (HTTP {e.status}): "
                         "the interaction token expired or the bot lost access")
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- network hiccup; try next tick
                pass
        await asyncio.sleep(interval)


async def _safe_followup_send(interaction, *args, **kwargs):
    """interaction.followup.send that never raises. Returns True on success."""
    try:
        await interaction.followup.send(*args, **kwargs)
        return True
    except discord.HTTPException as e:
        _log(f"followup.send failed (HTTP {getattr(e, 'status', '?')}): {e}")
    except Exception as e:  # noqa: BLE001
        _log(f"followup.send failed: {e}")
    return False


async def _safe_channel_send(channel, *args, **kwargs):
    """channel.send that never raises (403 no-permission, missing channel...)."""
    if channel is None:
        return False
    try:
        await channel.send(*args, **kwargs)
        return True
    except discord.HTTPException as e:
        _log(f"channel.send fallback failed (HTTP {getattr(e, 'status', '?')}): {e}")
    except Exception as e:  # noqa: BLE001
        _log(f"channel.send fallback failed: {e}")
    return False


# Discord kills an interaction's webhook token 15 minutes after the
# interaction was created; stop trying the webhook a little before that.
_FOLLOWUP_MAX_AGE_SECONDS = 14 * 60


def _interaction_token_expired(interaction) -> bool:
    """True when the interaction's webhook token is (almost) certainly spent.

    A long auto-fit can outlive the 15-minute token, making the followup a
    guaranteed 401 (50027 Invalid Webhook Token) -- deliver via the channel /
    DM routes straight away instead. When the age can't be determined (no
    created_at, e.g. a stubbed interaction) assume the token is still fresh.
    """
    created = getattr(interaction, "created_at", None)
    if created is None:
        return False
    try:
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - created
    except (TypeError, AttributeError, OSError):
        return False
    return age.total_seconds() >= _FOLLOWUP_MAX_AGE_SECONDS


async def _deliver_result(interaction, make_kwargs, mention=True):
    """Deliver a finished result: followup -> channel -> the user's DMs.

    `make_kwargs()` must build FRESH send kwargs on every call -- a
    discord.File is consumed by the first attempt, so a retry cannot reuse it.
    A long auto-fit can outlast the 15-minute interaction token; once it is
    nearly spent the followup is skipped entirely instead of firing a
    guaranteed 401. The channel fallback can still hit 403 when the bot may
    not post there (a user-install run in a DM / server the bot isn't in),
    and the DM fallback can fail when the user doesn't share a server with
    the bot; each failure is logged once and the next route is tried instead
    of raising. If every route fails, the files stay in the run's temp folder
    for 24 hours and the fact is logged so the owner can grab them.
    """
    if _interaction_token_expired(interaction):
        _log("interaction token is past its 15-minute lifetime; delivering "
             "via channel/DM instead of the followup webhook")
    elif await _safe_followup_send(interaction, **make_kwargs()):
        return True
    user = getattr(interaction, "user", None)
    kwargs = make_kwargs()
    if mention and user is not None:
        kwargs.setdefault("content", f"{user.mention} your result is ready:")
    if await _safe_channel_send(getattr(interaction, "channel", None), **kwargs):
        _log("result delivered via channel fallback")
        return True
    if user is not None:
        kwargs = make_kwargs()
        try:
            await user.send(**kwargs)
            _log(f"result delivered via DM to user id "
                 f"{getattr(user, 'id', '?')}")
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"DM fallback failed: {e}")
    _log("result could not be delivered by followup, channel or DM; the "
         "files remain in the run's temp folder (kb_*) for 24 hours -- "
         "retrieve them from %TEMP% if needed")
    return False


# A state is "final" once the moderation decision is locked in. In the review
# vocabulary an asset may sit at NotReviewed / Reviewing / Pending for a while;
# only these are treated as a done deal so the watcher keeps polling through
# any intermediate state and only stops + reacts on an actual decision.
_MODERATION_FINAL = {
    "approved", "declined", "revoked", "moderated", "denied", "edited",
    "unavailable", "removed", "rejected", "reject", "failed", "failure",
    "closed", "flagged",
}


def _is_final_state(state):
    return (state or "").strip().lower() in _MODERATION_FINAL


def _codec_ext(codec):
    """Pick a sensible audio extension from a codec name for the attached file."""
    c = (codec or "").lower()
    if "vorbis" in c or "opus" in c:
        return "ogg"
    if "mp3" in c:
        return "mp3"
    if "flac" in c:
        return "flac"
    if "wav" in c or "pcm" in c:
        return "wav"
    return "ogg"


def _fresh_files(files):
    """Rebuild attachment File objects so each target gets its own open handle.

    A discord.File is read from its current fp position on send; reusing the same
    object across a second send yields an empty attachment (fp is at EOF). Files
    backed by a real path are reopened here; others are passed through as-is.
    """
    out = []
    for f in files or []:
        src = getattr(f, "fp", None)
        path = getattr(src, "name", None)
        if path:
            out.append(discord.File(str(path), filename=f.filename))
        else:
            out.append(f)
    return out


def _monitor_embed(asset_id, display, prev, cur, description=None):
    """Fancy embed for a moderation state change."""
    cur_l = (cur or "").strip().lower()
    if cur_l == "approved":
        title, color = "Audio Accepted", PURPLE
    elif cur_l in ("declined", "denied", "revoked", "moderated"):
        title, color = "Audio Declined", RED
    else:
        title, color = f"Status: {cur}", PURPLE
    e = discord.Embed(title=title, color=color)
    e.add_field(name="Asset", value=f"`{asset_id}`", inline=True)
    e.add_field(name="Name", value=display or "—", inline=True)
    if description:
        e.add_field(name="Description", value=(description[:120] or "—"), inline=False)
    e.add_field(name="Changed", value=f"{prev} → **{cur}**", inline=False)
    e.add_field(name="Link", value=f"https://roblox.com/library/{asset_id}", inline=False)
    _brand_footer(e, "moderation monitor")
    return e


def _monitor_accepted_embed(asset_id, display, description, info, image="waveform.png"):
    """Fancy embed for an accepted asset, mirroring /analyze: the specs fields +
    the inline waveform image (set via the attached ``image`` file)."""
    e = discord.Embed(title="Audio Accepted", color=PURPLE)
    e.add_field(name="Asset", value=f"`{asset_id}`", inline=True)
    e.add_field(name="Name", value=display or "—", inline=True)
    if description:
        e.add_field(name="Description", value=(description[:120] or "—"), inline=False)
    e.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
    e.add_field(name="Codec", value=info.get("codec", "—"), inline=True)
    channels = info.get("channels")
    e.add_field(name="Channels", value=str(channels) if channels else "—", inline=True)
    sample = info.get("sample_rate")
    e.add_field(name="Sample Rate", value=f"{sample} Hz" if sample else "—", inline=True)
    br = info.get("bitrate_kbps")
    e.add_field(name="Bitrate", value=f"{br} kbps" if br else "—", inline=True)
    lufs = info.get("lufs")
    e.add_field(name="Integrated Loudness", value=f"{lufs:.1f} LUFS" if lufs is not None else "—", inline=True)
    peak = info.get("peak")
    e.add_field(name="Peak", value=f"{peak:.1f} dBFS" if peak is not None else "—", inline=True)
    e.add_field(name="Link", value=f"https://roblox.com/library/{asset_id}", inline=False)
    if image:
        e.set_image(url=f"attachment://{image}")
    _brand_footer(e, "moderation monitor")
    return e


def _monitor_status_embed(title, color, fields, tag="moderation monitor"):
    """A clean, single-purpose informational embed for monitor status notices."""
    e = discord.Embed(title=title, color=color)
    for name, value in fields:
        e.add_field(name=name, value=value, inline=False)
    _brand_footer(e, tag)
    return e


class _MonitorSession:
    """Tracks every id watched by a single ``/monitor`` invocation.

    The channel the command ran in gets ONE beautified summary board that is
    EDIT-ed in place as those audios change -- it only shows COUNTS (pending /
    accepted / deleted), so a long watch never spams the chat. The owner's DMs
    get the FULL detail for each audio (its own card, edited in place, including
    the accepted download + analysis). When the command was run in a DM there is
    no channel board -- the detailed DM card is the only message.
    """

    def __init__(self, user, channel):
        self.user = user
        self.channel = channel
        self.in_dm = channel is not None and getattr(channel, "type", None) == discord.ChannelType.private
        self.board_msg = None      # channel summary Message (None when in a DM / send failed)
        self.states = {}           # asset id -> latest moderation state
        self.displays = {}         # asset id -> display name
        self.descriptions = {}     # asset id -> description
        self.pending = {}          # asset id -> True while still being watched
        self.dm_msgs = {}          # asset id -> the detailed DM Message (edited in place)

    # -- channel summary board (counts only) -------------------------------
    def _counts(self):
        accepted = deleted = pending = 0
        for aid, state in self.states.items():
            s = (state or "").strip().lower()
            if s == "approved":
                accepted += 1
            elif _is_final_state(s):
                deleted += 1
            elif self.pending.get(aid):
                pending += 1
        return pending, accepted, deleted

    def _board_embed(self):
        pending, accepted, deleted = self._counts()
        e = discord.Embed(title="Audio Monitor", color=PURPLE)
        e.add_field(name="Audio (pending)", value=str(pending), inline=True)
        e.add_field(name="Accepted", value=str(accepted), inline=True)
        e.add_field(name="Deleted", value=str(deleted), inline=True)
        watched = [aid for aid in self.pending if self.pending.get(aid)]
        if watched:
            e.add_field(name="Monitoring", value=", ".join(f"`{a}`" for a in watched), inline=False)
        _brand_footer(e, "moderation monitor")
        return e

    async def _update_board(self):
        if self.in_dm:
            return
        embed = self._board_embed()
        if self.board_msg is None:
            try:
                self.board_msg = await self.channel.send(embed=embed)
            except Exception:  # noqa: BLE001
                self.board_msg = None
            return
        try:
            await self.board_msg.edit(embed=embed)
        except Exception:  # noqa: BLE001
            try:
                self.board_msg = await self.channel.send(embed=embed)
            except Exception:  # noqa: BLE001
                self.board_msg = None

    # -- per-audio DM detail (edited in place) ------------------------------
    async def _dm_set(self, asset_id, embed, files=None):
        msg = self.dm_msgs.get(asset_id)
        if msg is not None:
            try:
                if files is not None:
                    await msg.edit(embed=embed, attachments=_fresh_files(files))
                else:
                    await msg.edit(embed=embed)
                return
            except Exception:  # noqa: BLE001
                self.dm_msgs[asset_id] = None
        if self.user is None:
            return
        try:
            self.dm_msgs[asset_id] = await self.user.send(embed=embed, files=_fresh_files(files))
        except Exception:  # noqa: BLE001
            self.dm_msgs[asset_id] = None

    async def _set_state(self, asset_id, state, pending=True):
        self.states[asset_id] = state
        self.pending[asset_id] = pending
        await self._update_board()

    # -- public API ---------------------------------------------------------
    async def start(self, asset_id, initial_state, display, description):
        """Register one id: resolve it now (show a card in DMs), update the board,
        and spawn a watcher if it is still pending."""
        self.states[asset_id] = initial_state
        self.displays[asset_id] = display
        self.descriptions[asset_id] = description or ""
        if _is_final_state(initial_state):
            self.pending[asset_id] = False
            if (initial_state or "").strip().lower() == "approved":
                await self._dm_set(asset_id, _monitor_status_embed(
                    "Already accepted", PURPLE,
                    [("Asset", f"`{asset_id}` ({display})"),
                     ("Status", "showing its analysis as native Roblox audio...")]))
                await self._accept(asset_id)
            else:
                await self._dm_set(asset_id, _monitor_embed(
                    asset_id, display, "—", initial_state, description))
            await self._update_board()
            return
        self.pending[asset_id] = True
        await self._dm_set(asset_id, _monitor_status_embed(
            "Monitoring started", PURPLE,
            [("Asset", f"`{asset_id}` ({display})"),
             ("Status", f"currently **{initial_state}**"),
             ("Note", "full detail for this audio is kept here in your DMs.")]))
        await self._update_board()
        active_monitors[asset_id] = asyncio.create_task(self.watch(asset_id, initial_state))

    async def watch(self, asset_id, initial_state):
        task = asyncio.current_task()
        display = self.displays.get(asset_id, asset_id)
        last = initial_state
        try:
            deadline = time.monotonic() + MONITOR_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
                try:
                    st = await asyncio.to_thread(killa_tools.get_asset_moderation, asset_id)
                except Exception as e:  # noqa: BLE001
                    _log(f"monitor {asset_id}: read error -> {e}")
                    await self._dm_set(asset_id, _monitor_status_embed(
                        "Monitoring stopped", RED,
                        [("Asset", f"`{asset_id}` ({display})"),
                         ("Reason", f"{e}")]))
                    await self._set_state(asset_id, "stopped", pending=False)
                    return
                cur = st["state"]
                _log(f"monitor {asset_id}: {last} -> {cur}")
                if cur and cur != last:
                    if _is_final_state(cur):
                        await self._final(asset_id, st, last)
                        return
                    await self._dm_set(asset_id, _monitor_embed(
                        asset_id, st.get("display_name"), last, cur, st.get("description")))
                    await self._set_state(asset_id, cur, pending=True)
                    last = cur
            await self._dm_set(asset_id, _monitor_status_embed(
                "Monitoring timed out", PURPLE,
                [("Asset", f"`{asset_id}` ({display})"),
                 ("Detail", f"No final decision after {int(MONITOR_TIMEOUT_SECONDS)}s.")]))
            await self._set_state(asset_id, "stopped", pending=False)
        finally:
            if active_monitors.get(asset_id) is task:
                active_monitors.pop(asset_id, None)
                _watcher_display.pop(asset_id, None)
                _log(f"monitor {asset_id}: ended (slot freed)")

    async def _final(self, asset_id, st, prev):
        cur = st["state"]
        display = st.get("display_name")
        _log(f"monitor {asset_id}: FINAL state {cur}")
        if (cur or "").strip().lower() == "approved":
            await self._dm_set(asset_id, _monitor_status_embed(
                "Accepted", PURPLE,
                [("Asset", f"`{asset_id}` ({display})"),
                 ("Status", "downloading the audio as native Roblox audio...")]))
            await self._accept(asset_id, st)
        else:
            await self._dm_set(asset_id, _monitor_embed(
                asset_id, display, prev, cur, st.get("description")))
        await self._set_state(asset_id, cur, pending=False)

    async def _accept(self, asset_id, st=None):
        """Download the accepted audio, analyze it, and post the full specs card
        (with the waveform) to the DMs -- the DM card is edited in place."""
        display = self.displays.get(asset_id) or (st or {}).get("display_name") or asset_id
        description = self.descriptions.get(asset_id) or (st or {}).get("description") or ""
        workdir = tempfile.mkdtemp(prefix="kb_monitor_")
        try:
            raw = await asyncio.to_thread(killa_tools.download_asset_audio, asset_id)
            input_path = os.path.join(workdir, "input_audio")  # ffmpeg sniffs content
            with open(input_path, "wb") as f:
                f.write(raw)
            pass1 = os.path.join(workdir, "pass1.ogg")
            ogg_path = os.path.join(workdir, "audio.ogg")
            wf = os.path.join(workdir, "waveform.png")
            res = await asyncio.to_thread(
                killa_tools.roblox_simulate, input_path, pass1, ogg_path, wf)
            # roblox_simulate returns (info_dict, waveform_path).
            info_dict = res[0] if isinstance(res, tuple) else res
            waveform_ok = os.path.exists(wf)
            title = killa_tools._safe_name(display or f"audio_{asset_id}", "ogg")
            files = [discord.File(ogg_path, filename=title)]
            if waveform_ok:
                files.append(discord.File(wf, filename="waveform.png"))
            embed = _monitor_accepted_embed(
                asset_id, display, description, info_dict,
                image="waveform.png" if waveform_ok else None)
            await self._dm_set(asset_id, embed, files=files)
        except Exception as e:  # noqa: BLE001
            await self._dm_set(asset_id, _monitor_status_embed(
                "Audio accepted, but download failed", RED,
                [("Asset", f"`{asset_id}` ({display})"),
                 ("Error", f"{e}")]))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _split_ids(text):
    """Split a mixed id string on commas, semicolons and whitespace.

    Backticks are stripped so copy-pasted ids like ``123, 456`` work too.
    """
    return [t.strip("`") for t in text.replace(",", " ").replace(";", " ").split()]


@bot.tree.command(
    name="monitor",
    description="Monitor Roblox asset moderation states; changes are posted here.",
)
@app_commands.describe(
    asset="One or more audio asset ids (space/comma separated), 'list' to show "
          "watchers, or 'stop <id...>' to stop one or more.",
)
async def monitor_slash(interaction: discord.Interaction, asset: str):
    if not await _require_trusted(interaction):
        return
    if not killa_tools.ROBLOX_COOKIE:
        embed = discord.Embed(
            title="Roblox Session Required",
            description="No `ROBLOX_COOKIE` is configured in `.env`. Add a valid `.ROBLOSECURITY` session cookie to monitor moderation states.",
            color=RED,
        )
        _brand_footer(embed, "moderation monitor")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    token = (asset or "").strip().lower()

    # /monitor list -- active watchers overview card
    if token in ("list", "list_"):
        if not active_monitors:
            embed = discord.Embed(
                title="Active Moderation Watchers",
                description="No Roblox audio assets are currently being monitored.\n\nStart watching one or more IDs with `/monitor <id...>`.",
                color=PURPLE,
            )
            _brand_footer(embed, "moderation monitor")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        lines = [
            f"• `{aid}` — **{_watcher_display.get(aid, 'Audio Asset')}** "
            f"([Library](https://www.roblox.com/library/{aid}))"
            for aid in sorted(active_monitors)
        ]
        embed = discord.Embed(
            title="Active Moderation Watchers",
            description="\n".join(lines)[:4000],
            color=PURPLE,
        )
        embed.add_field(name="Active Count", value=str(len(active_monitors)), inline=True)
        embed.add_field(
            name="Poll Interval",
            value=f"Every {int(MONITOR_INTERVAL_SECONDS)}s",
            inline=True,
        )
        _brand_footer(embed, "moderation monitor")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # /monitor stop <id...|all>
    if token == "stop" or token.startswith("stop "):
        raw_rest = (asset or "").strip()[4:].strip()
        if raw_rest.lower() == "all":
            targets = list(sorted(active_monitors))
        else:
            targets = []
            for item in _split_ids(raw_rest):
                try:
                    targets.append(str(verify_audio.extract_asset_id(item)))
                except verify_audio.AssetError:
                    targets.append(item)
        if not targets:
            embed = discord.Embed(
                title="Specify Watchers to Stop",
                description=(
                    "Pass one or more asset IDs (or `all`) after `stop`, for example:\n"
                    "`/monitor stop 123456789 987654321` or `/monitor stop all`."
                ),
                color=RED,
            )
            _brand_footer(embed, "moderation monitor")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        stopped, missing = [], []
        for t in targets:
            task = active_monitors.get(t)
            if task:
                task.cancel()
                active_monitors.pop(t, None)
                disp = _watcher_display.pop(t, None)
                stopped.append(f"`{t}`" + (f" ({disp})" if disp else ""))
            else:
                missing.append(f"`{t}`")
        embed = discord.Embed(
            title="Moderation Monitor Updated",
            description=(
                f"Stopped **{len(stopped)}** watcher(s)."
                if stopped else "None of the specified asset IDs had an active watcher."
            ),
            color=PURPLE if stopped else RED,
        )
        if stopped:
            embed.add_field(name="Stopped", value=", ".join(stopped)[:1024], inline=False)
        if missing:
            embed.add_field(name="Not Active", value=", ".join(missing)[:1024], inline=False)
        _brand_footer(embed, "moderation monitor")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Subscribe: comma/space/semicolon separated ids in one call
    ids, bad = [], []
    for t in _split_ids(asset or ""):
        try:
            ids.append(str(verify_audio.extract_asset_id(t)))
        except verify_audio.AssetError:
            bad.append(f"`{t}`")
    if not ids:
        if bad:
            embed = discord.Embed(
                title="Invalid Asset ID",
                description="Could not extract a valid Roblox asset ID from: " + ", ".join(bad),
                color=RED,
            )
        else:
            embed = discord.Embed(
                title="Moderation Monitor Usage",
                description=(
                    "Pass one or more Roblox audio asset IDs or URLs:\n"
                    "• `/monitor 123456789 987654321`\n"
                    "• `/monitor list`\n"
                    "• `/monitor stop 123456789`"
                ),
                color=PURPLE,
            )
        _brand_footer(embed, "moderation monitor")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    channel = getattr(interaction, "channel", None)
    user = interaction.user
    session = _MonitorSession(user, channel)

    started, skipped, decided, failed = [], [], [], list(bad)
    for asset_id in ids:
        if asset_id in active_monitors:
            skipped.append(f"`{asset_id}`")
            continue
        try:
            st = await asyncio.to_thread(killa_tools.get_asset_moderation, asset_id)
        except Exception as e:  # noqa: BLE001
            failed.append(f"`{asset_id}` ({e})")
            continue
        initial_state = st["state"]
        display = st.get("display_name")
        description = st.get("description")
        if _is_final_state(initial_state):
            await session.start(asset_id, initial_state, display, description)
            decided.append(f"`{asset_id}` — **{display}** (`{initial_state}`)")
            continue
        _watcher_display[asset_id] = st["display_name"]
        await session.start(asset_id, initial_state, st["display_name"], st.get("description") or "")
        started.append(f"`{asset_id}` — **{st['display_name']}** (`{initial_state}`)")

    any_ok = bool(started or decided)
    summary_embed = discord.Embed(
        title="Moderation Monitor",
        description=(
            f"Tracking **{len(started)}** pending asset(s)."
            if started else
            f"Checked **{len(decided)}** already-decided asset(s)."
            if decided else
            "No new watchers were started."
        ),
        color=PURPLE if any_ok or skipped else RED,
    )
    if started:
        summary_embed.add_field(name="Watching", value="\n".join(started)[:1024], inline=False)
    if decided:
        summary_embed.add_field(name="Already Decided", value="\n".join(decided)[:1024], inline=False)
    if skipped:
        summary_embed.add_field(name="Already Active", value=", ".join(skipped)[:1024], inline=False)
    if failed:
        summary_embed.add_field(name="Could Not Start", value="\n".join(failed)[:1024], inline=False)
    if any_ok:
        summary_embed.add_field(
            name="Updates",
            value="Full status cards and audio previews are sent to your DMs as states update.",
            inline=False,
        )
    _brand_footer(summary_embed, "moderation monitor")
    await interaction.followup.send(embed=summary_embed, ephemeral=True)


@bot.tree.command(
    name="ping",
    description="Check that KILLA'S BOSS is online and ready.",
)
async def ping_slash(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound:
        # The interaction already expired before we could respond (e.g. the
        # gateway churned while two bot processes raced). Nothing is left to
        # reply to, so just return instead of dumping a traceback.
        return
    embed = discord.Embed(
        title="KILLA'S BOSS is online",
        description="Killa's audio verification bot is awake and ready to check your sounds.",
        color=PURPLE,
    )
    embed.add_field(name="Status", value="Active", inline=True)
    embed.add_field(name="Operated by", value="Killa", inline=True)
    embed.add_field(name="Service", value="Roblox audio verification", inline=True)
    embed.add_field(
        name="Discord",
        value="[Join the server](https://discord.gg/AWFAFSCbXQ)",
        inline=False,
    )
    embed.add_field(
        name="Roblox Group",
        value="[KILLA'S HUB 3.0](https://www.roblox.com/communities/101224973/KILLAS-HUB-3-0#!/about)",
        inline=False,
    )
    try:
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
    except Exception:
        pass
    _brand_footer(embed, "City Of Boombox audio tool")
    await interaction.followup.send(embed=embed)


def _audio_embed(title, filename, info, note=None, image="waveform.png", tag="audio analysis"):
    """Build a clean embed for a processed audio file (spec fields + image).

    `image` is the filename of the attached image to show inline (e.g. the
    waveform or the spectrogram); pass None for no image.  `tag` sets the
    branded footer label.
    """
    sr = info.get("sample_rate")
    chn = info.get("channels")
    codec = info.get("codec")
    br = info.get("bitrate_kbps")
    lufs = info.get("lufs")
    peak = info.get("peak")
    embed = discord.Embed(title=title, description=filename, color=PURPLE)
    if note:
        embed.add_field(name="Notes", value=note, inline=False)
    embed.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
    embed.add_field(name="Codec", value=(codec or "—"), inline=True)
    embed.add_field(name="Channels", value=(str(chn) if chn else "—"), inline=True)
    embed.add_field(name="Sample Rate", value=f"{sr} Hz" if sr else "—", inline=True)
    embed.add_field(name="Bitrate", value=f"{br} kbps" if br else "—", inline=True)
    embed.add_field(name="Integrated Loudness", value=f"{lufs:.1f} LUFS" if lufs is not None else "—", inline=True)
    embed.add_field(name="Peak", value=f"{peak:.1f} dBFS" if peak is not None else "—", inline=True)
    if image:
        embed.set_image(url=f"attachment://{image}")
    _brand_footer(embed, tag)
    return embed


def _analyze_roblox_warning_embed(asset_id, meta=None, reason=None):
    """Basic warning card shown when a Roblox audio asset cannot be downloaded."""
    embed = discord.Embed(
        title="Warning: Cannot Download Audio",
        description=f"Warning: Roblox audio asset `{asset_id}` could not be downloaded.",
        color=RED,
    )
    if meta:
        if meta.get("name"):
            embed.add_field(name="Asset Name", value=str(meta["name"])[:256], inline=True)
        if meta.get("creator"):
            embed.add_field(name="Creator", value=str(meta["creator"])[:256], inline=True)
        if meta.get("asset_type"):
            embed.add_field(name="Type", value=str(meta["asset_type"])[:128], inline=True)
    if reason:
        embed.add_field(name="Details", value=str(reason)[:512], inline=False)
    embed.add_field(
        name="Link",
        value=f"https://www.roblox.com/library/{asset_id}",
        inline=False,
    )
    _brand_footer(embed, "audio analysis")
    return embed


analyze_group = app_commands.Group(
    name="analyze",
    description="Analyze an uploaded audio file or a Roblox audio asset.",
)


@analyze_group.command(
    name="file",
    description="Analyze an uploaded audio file: loudness, waveform, format info.",
)
@app_commands.describe(file="The audio file to analyze.")
async def analyze_file_slash(interaction: discord.Interaction, file: discord.Attachment):
    if not await _require_trusted(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_analyze_")
    try:
        input_path = await _save_attachment(workdir, file)
        if os.path.getsize(input_path) < 1024:
            raise RuntimeError(
                "the file came through empty or truncated -- try sending it again.")
        info = await asyncio.to_thread(killa_tools.analyze_audio, input_path)
        wf = os.path.join(workdir, "waveform.png")
        await asyncio.to_thread(killa_tools.render_waveform, input_path, wf)
        ext = (os.path.splitext(file.filename or "")[1].lstrip(".")
               or _codec_ext(info.get("codec")))
        clean_name = killa_tools._safe_name(file.filename or "audio", ext)
        try:
            await interaction.followup.send(
                embed=_audio_embed("Audio Analysis", clean_name, info),
                files=[discord.File(wf, filename="waveform.png"),
                       discord.File(input_path, filename=clean_name)])
        except discord.HTTPException:
            await interaction.followup.send(
                embed=_audio_embed("Audio Analysis", clean_name, info),
                files=[discord.File(wf, filename="waveform.png")])
            await interaction.followup.send(
                "the original file was too large to attach, so only the analysis is shown.",
                ephemeral=True)
    except Exception as e:  # noqa: BLE001
        _log(f"analyze file failed for {interaction.user}: {e}\n"
             + traceback.format_exc())
        await interaction.followup.send(f"analyze failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@analyze_group.command(
    name="roblox",
    description="Look up, download, and analyze a Roblox audio asset ID or URL.",
)
@app_commands.describe(
    asset="Roblox audio asset ID or URL (e.g. 1848354536 or roblox.com/library/...).",
)
async def analyze_roblox_slash(interaction: discord.Interaction, asset: str):
    if not await _require_trusted(interaction):
        return
    try:
        asset_id = verify_audio.extract_asset_id(asset)
    except verify_audio.AssetError as err:
        embed = discord.Embed(
            title="Invalid Roblox Asset ID",
            description=str(err),
            color=RED,
        )
        _brand_footer(embed, "audio analysis")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_analyze_rbx_")
    meta = None
    try:
        try:
            meta = await asyncio.to_thread(killa_tools.lookup_roblox_asset, asset_id)
        except Exception:
            meta = {"id": asset_id, "name": f"Asset {asset_id}", "is_audio": True}

        if not meta.get("is_audio", True):
            warn = _analyze_roblox_warning_embed(
                asset_id,
                meta=meta,
                reason=f"Asset `{asset_id}` is a {meta.get('asset_type') or 'non-audio'} asset, not Audio.",
            )
            await interaction.followup.send(embed=warn)
            return

        if meta.get("is_banned"):
            warn = _analyze_roblox_warning_embed(
                asset_id,
                meta=meta,
                reason="This asset was moderated or removed on Roblox.",
            )
            await interaction.followup.send(embed=warn)
            return

        try:
            raw = await asyncio.to_thread(killa_tools.download_asset_audio, asset_id)
        except Exception as dl_err:  # noqa: BLE001
            warn = _analyze_roblox_warning_embed(asset_id, meta=meta, reason=str(dl_err))
            await interaction.followup.send(embed=warn)
            return

        raw_path = os.path.join(workdir, "raw_audio")
        with open(raw_path, "wb") as fh:
            fh.write(raw)

        try:
            info = await asyncio.to_thread(killa_tools.analyze_audio, raw_path)
            wf = os.path.join(workdir, "waveform.png")
            await asyncio.to_thread(killa_tools.render_waveform, raw_path, wf)
        except Exception as parse_err:  # noqa: BLE001
            warn = _analyze_roblox_warning_embed(
                asset_id,
                meta=meta,
                reason=f"Downloaded bytes could not be decoded as audio ({parse_err}).",
            )
            await interaction.followup.send(embed=warn)
            return

        ext = _codec_ext(info.get("codec"))
        attach_name = killa_tools._safe_name(
            meta.get("name") or f"audio_{asset_id}", ext
        )
        playable_path = os.path.join(workdir, attach_name)
        shutil.copyfile(raw_path, playable_path)
        await asyncio.to_thread(killa_tools.strip_audio_metadata, playable_path)

        display_title = meta.get("name") or f"Asset {asset_id}"
        embed = _audio_embed(
            "Roblox Audio Analysis",
            f"**{display_title}** (`{asset_id}`)",
            info,
        )
        if meta.get("creator"):
            embed.add_field(name="Creator", value=str(meta["creator"]), inline=True)
        if meta.get("moderation_state"):
            embed.add_field(name="Moderation", value=str(meta["moderation_state"]), inline=True)
        embed.add_field(
            name="Link",
            value=f"https://www.roblox.com/library/{asset_id}",
            inline=False,
        )
        try:
            await interaction.followup.send(
                embed=embed,
                files=[
                    discord.File(wf, filename="waveform.png"),
                    discord.File(playable_path, filename=attach_name),
                ],
            )
        except discord.HTTPException:
            await interaction.followup.send(
                embed=embed,
                files=[discord.File(wf, filename="waveform.png")],
            )
            await interaction.followup.send(
                "the audio file exceeded Discord's attachment limit, so only the analysis is shown.",
                ephemeral=True,
            )
    except Exception as e:  # noqa: BLE001
        _log(f"analyze roblox failed for {interaction.user}: {e}\n"
             + traceback.format_exc())
        warn = _analyze_roblox_warning_embed(asset_id, meta=meta, reason=str(e))
        await interaction.followup.send(embed=warn)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


bot.tree.add_command(analyze_group)


@bot.tree.command(
    name="copyr8",
    description="CopyR8: Song checking tool for testing against a filter.",
)
@app_commands.describe(
    file="The audio file to process.",
    preset="Auto-fit mode for song checking (filter testing), or a fixed shift preset.",
)
@app_commands.choices(
    preset=[
        app_commands.Choice(name="Auto-Fit (fast)", value="autofit"),
        app_commands.Choice(name="Auto-Fit V2 (closest)", value="autofit-v2"),
        app_commands.Choice(name="Auto-Fit V3 (macro + mixed)", value="autofit-v3"),
        app_commands.Choice(name="Auto-Fit V4 (new macro)", value="autofit-v4"),
        app_commands.Choice(name="Subtle", value="subtle"),
        app_commands.Choice(name="Balanced", value="balanced"),
        app_commands.Choice(name="Stealth", value="stealth"),
        app_commands.Choice(name="Aggressive", value="aggressive"),
    ],
)
async def copyr8_slash(interaction: discord.Interaction,
                     file: discord.Attachment, preset: str):
    # These are module-level; writing them inside the function would otherwise
    # make Python treat them as local and raise UnboundLocalError on the reads
    # below (the auto-fit gate).
    global _copyr8_autofit_running, _copyr8_cooldown_until
    if not await _require_trusted(interaction):
        return
    is_autofit = preset in ("autofit", "autofit-v2", "autofit-v3", "autofit-v4")

    # Auto-fit is throttled: only one runs at a time (it contacts Shazam, which
    # rate-limits by device id), and each user is cooled down between runs. The
    # throttle covers BOTH fits (V1 and V2) -- they hit the same recogniser.
    if is_autofit:
        now = time.time()
        if _copyr8_autofit_running:
            _log(f"copyr8-autofit BLOCKED (already running) for {interaction.user} "
                 f"(id {interaction.user.id})")
            await interaction.response.send_message(
                "The CopyR8 auto-fit is already testing a file right now. Wait for it "
                "to finish, then run this again.", ephemeral=True)
            return
        until = _copyr8_cooldown_until.get(interaction.user.id, 0)
        if now < until:
            remain = int(until - now)
            _log(f"copyr8-autofit BLOCKED (cooldown {remain}s) for {interaction.user} "
                 f"(id {interaction.user.id})")
            await interaction.response.send_message(
                f"This command is on cooldown. Try again in **{remain} second"
                f"{'' if remain == 1 else 's'}**.", ephemeral=True)
            return
        _copyr8_autofit_running = True
    elif preset not in killa_tools.COPYR8_PRESETS:
        await interaction.response.send_message(
            f"Unknown preset `{preset}`. Pick one of: "
            + ", ".join(killa_tools.COPYR8_PRESETS)
            + ", autofit, autofit-v2, autofit-v3, or autofit-v4",
            ephemeral=True)
        return

    try:
        await interaction.response.defer(thinking=True)
    except Exception:
        if is_autofit:
            _copyr8_autofit_running = False
        raise
    workdir = tempfile.mkdtemp(prefix="kb_copyr8_")
    try:
        input_path = await _save_attachment(workdir, file)
        output_path = os.path.join(workdir, "output.mp3")
        spec_path = os.path.join(workdir, "spectrogram.png")
        out_stem = killa_tools.clean_export_stem(file.filename or "audio", fallback="audio")
        src_ext = os.path.splitext(file.filename or ".mp3")[1] or ".mp3"
        clean_source_display = f"{out_stem}{src_ext}"
        base_stem, already_processed = out_stem, False
        while base_stem.lower().endswith("_copyr8"):
            base_stem = base_stem[:-len("_copyr8")]
            already_processed = True
        base_stem = killa_tools.clean_export_stem(base_stem, fallback="audio")
        copyr8_name = f"{base_stem}_copyr8_2.mp3" if already_processed else f"{out_stem}_copyr8.mp3"

        if not is_autofit:
            # The four fixed shift presets: a straight key/speed re-encode, no
            # recogniser loop -- extras for when a known setting is wanted.
            info = await asyncio.to_thread(
                killa_tools.copyr8_process, input_path, output_path, spec_path, preset)
            semi, tempo = killa_tools.COPYR8_PRESETS[preset]
            embed = _audio_embed(
                f"Preset Applied — {preset}",
                copyr8_name,
                info,
                note=f"Shifted by {semi:+.1f} st, tempo {tempo:.2f}x ({preset} preset).",
                image="spectrogram.png",
                tag="copyr8 preset",
            )
            await interaction.followup.send(
                embed=embed,
                files=[discord.File(output_path, filename=copyr8_name),
                       discord.File(spec_path, filename="spectrogram.png")])
            return

        is_v2 = preset == "autofit-v2"
        is_v3 = preset == "autofit-v3"
        is_v4 = preset == "autofit-v4"
        if is_v4:
            fit_label = "CopyR8 Auto-Fit V4"
        elif is_v3:
            fit_label = "CopyR8 Auto-Fit V3"
        elif is_v2:
            fit_label = "CopyR8 Auto-Fit V2"
        else:
            fit_label = "CopyR8 Auto-Fit"

        if is_v3 or is_v4:
            # The macro fits are the same engine with different bundled chains;
            # each reads its own macro path (env-overridable) at dispatch time.
            macro_path = AUTOFIT_V4_MACRO if is_v4 else AUTOFIT_V3_MACRO
            fit = killa_tools.copyr8_autofit_v4 if is_v4 else killa_tools.copyr8_autofit_v3

            def fit_fn(i, o, s, p):
                macro_fn = None
                if macro_path:
                    def macro_fn(in_path, out_path, prog):
                        import audacity_processor
                        return audacity_processor.run_macro_blocking(
                            in_path, out_path, macro_path, prog)
                return fit(i, o, s, p, macro_fn=macro_fn)
        else:
            fit_fn = killa_tools.copyr8_autofit_v2 if is_v2 else killa_tools.copyr8_autofit
        progress_lines = []

        def _progress(msg):
            progress_lines.append(msg)
            _log(f"copyr8-autofit progress: {msg}")

        # Surface each step in the deferred message so the user sees which
        # shift is being tried ("getting the best it can get to").
        msg = await interaction.original_response()
        updater = asyncio.create_task(
            _update_copyr8_progress(msg, progress_lines, fit_label,
                                    status_fn=killa_tools.pacing_status))
        try:
            result = await asyncio.to_thread(
                fit_fn, input_path, output_path, spec_path, _progress)
        finally:
            updater.cancel()
        # Only cool the user down once the run actually reached Shazam. A run
        # that failed immediately (e.g. a missing engine dep) doesn't lock them out.
        _copyr8_cooldown_until[interaction.user.id] = time.time() + COPYR8_COOLDOWN_SECONDS

        active_apis = " + ".join(killa_tools.active_recognition_providers())
        apis_list = ", ".join(killa_tools.active_recognition_providers())

        # "Clean" on its own is not the whole story: the engine corroborates
        # its sweep against the untouched original, because a soft-throttled
        # server answers with an empty match list and that reads exactly like
        # a genuine miss. Say which of the two this run got.
        if not result["clean"]:
            verdict = (f"No candidate passed {apis_list}. This file "
                       "is a best effort, not a confirmed no-match result.")
        elif result.get("verified") is True:
            verdict = (f"The tested candidate returned no matches from {apis_list}, "
                       "and at least one of them identified "
                       "the original. This is a recognition check, not a copyright "
                       "or moderation guarantee.")
        elif result.get("verified") is False:
            verdict = (f"None of {apis_list} identified the original, "
                       "so a clean result proves nothing. The balanced "
                       "protection shift (+2.00 st / 1.07x) was applied instead "
                       "of a trivial one. The result is unverified.")
        else:
            verdict = ("The candidate returned no matches, but an original-file "
                       "control check was inconclusive. The result is unverified.")
        embed = discord.Embed(
            title=f"{fit_label} Complete",
            description=f"Processed **{clean_source_display}** with CopyR8 song checking."
                        + ("\n\nThis file was already a processed copy, so this pass "
                           "stacks a second shift on top of the first. Run it on the "
                           "original track for a single pass."
                           if already_processed else ""),
            color=PURPLE,
        )
        info = result.get("info") or {}
        embed.add_field(name="Pitch", value=f"{result['pitch']:+.2f} st", inline=True)
        embed.add_field(name="Tempo", value=f"{result['tempo']:.2f}x", inline=True)
        # The walk only accepts a tempo past the cap once every same-speed
        # split failed, so when that happens the reply says why instead of
        # letting "1.17x" read like a normal result.
        if result["tempo"] > 1.081:
            embed.add_field(
                name="Speed",
                value=("this one runs faster than the original: the track "
                       "held out against every slower split, so the key was "
                       "dropped as far as it goes before speeding up."),
                inline=False)
        # V2 reports how far the accepted setting sits from the untouched
        # track (lower = sounds closer); V1 has no such measure. The number
        # includes the wobble when one was applied, hence the note.
        if result.get("cost") is not None:
            embed.add_field(name="Deviation",
                            value=f"{result['cost']:.2f} semitone-equivalents"
                                  + (" (incl. tremolo)" if result.get("tremolo_applied") else ""),
                            inline=True)
        # V2 layers a verified tremolo on top of the shift it settles on, so
        # the reply says outright whether the wobble is in the file -- and why
        # not, when the track refused it.
        if result.get("tremolo_applied"):
            embed.add_field(
                name="Tremolo",
                value=(f"{result['tremolo_depth']:.2f} @ {result['tremolo_rate']:g} Hz "
                       "(verified clean)"),
                inline=True)
        elif result.get("tremolo_reason"):
            embed.add_field(name="Tremolo",
                            value=f"not applied — {result['tremolo_reason']}",
                            inline=True)
        # The export puts the copy back at the source's own loudness: a phase
        # vocoder strips the peak density a loud master is built from, which
        # reads as "much quieter than the original" even at full scale. Both
        # figures are shown so a mismatch is visible rather than a surprise.
        loud = result.get("loudness") or {}
        if loud.get("out_lufs") is not None:
            embed.add_field(
                name="Loudness",
                value=(f"{loud['out_lufs']:.1f} LUFS"
                       + (f" (source {loud['source_lufs']:.1f})"
                          if loud.get("source_lufs") is not None else "")),
                inline=True)
        if info.get("duration"):
            embed.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
        if info.get("codec"):
            embed.add_field(name="Codec", value=info.get("codec", "—"), inline=True)
        if info.get("sample_rate"):
            embed.add_field(name="Sample Rate", value=f"{info['sample_rate']} Hz", inline=True)
        if is_v3 or is_v4:
            macro = result.get("macro") or {}
            if macro.get("applied"):
                macro_val = (f"configured macro chain ran "
                             f"({len(macro.get('steps') or [])} steps)")
            else:
                env_hint = "COPYR8_V4_MACRO" if is_v4 else "COPYR8_V3_MACRO"
                macro_val = macro.get("reason") or f"not configured (set {env_hint})"
            embed.add_field(name="Macro", value=macro_val, inline=False)
        embed.add_field(name="Recognition APIs", value=active_apis, inline=False)
        embed.add_field(name="Result", value=verdict, inline=False)
        if not (is_v3 or is_v4):
            embed.add_field(name="Note",
                            value="CopyR8 is for song checking against the filter.",
                            inline=False)
        embed.set_image(url="attachment://spectrogram.png")
        _brand_footer(embed, ("copyr8 auto-fit v4" if is_v4 else
                              "copyr8 auto-fit v3" if is_v3 else
                              "copyr8 auto-fit v2" if is_v2 else
                              "copyr8 auto-fit"))
        _log(f"copyr8-autofit DONE for {interaction.user} (id {interaction.user.id}): "
             f"{fit_label}, pitch {result['pitch']:+.1f} st, "
             f"tempo {result['tempo']:.2f}x, clean={result['clean']}"
             + (""
                if result.get("verified") is None else
                ", verified=True" if result.get("verified") else
                ", verified=False (recogniser doesn't know the track)")
             + (f", tremolo {result['tremolo_depth']:.2f}"
                f"@{result['tremolo_rate']:g}Hz"
                if result.get("tremolo_applied") else
                ", no tremolo" if result.get("tremolo_reason") else "")
             + (f", loudness {loud['out_lufs']:.1f} LUFS (source "
                f"{loud['source_lufs']:.1f})"
                if loud.get("out_lufs") is not None else "")
             + (f", macro {'ran' if (result.get('macro') or {}).get('applied') else 'not configured'}"
                if (is_v3 or is_v4) else "")
             + (", input was already a processed copy"
                if already_processed else ""))
        # An interaction token is only good for 15 minutes, and a long V2/V3
        # search can outlast that (401) -- fall back to the channel, then to
        # the user's DMs, without letting a 401/403 cascade into the handler.
        await _deliver_result(interaction, lambda: {
            "embed": embed,
            "files": [discord.File(output_path, filename=copyr8_name),
                      discord.File(spec_path, filename="spectrogram.png")]})
    except Exception as e:  # noqa: BLE001
        # The traceback names the exact file:line -- several copyr8 failures
        # (like the NoneType.strip one) are data-dependent and otherwise
        # un-diagnosable from str(e) alone.
        _log(f"copyr8 failed for {interaction.user}: {e}\n"
             + traceback.format_exc())
        if not await _safe_followup_send(interaction, f"copyr8 failed: {e}",
                                         ephemeral=True):
            await _safe_channel_send(
                interaction.channel,
                f"{interaction.user.mention} copyr8 failed: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if is_autofit:
            _copyr8_autofit_running = False







@bot.tree.command(
    name="lookup",
    description="Identify a song with Shazam / SongRec: title, artist, album, cover, link.",
)
@app_commands.describe(file="The audio file to identify (mp3/wav/flac/ogg/m4a).")
async def lookup_slash(interaction: discord.Interaction, file: discord.Attachment):
    """Identify a song via ShazamIO (shazam_backend.py) or SongRec (songrec_backend.py).

    Full-track recognition -- no segment or duration cap. Checks ShazamIO and
    SongRec APIs to identify the song.
    """
    if not await _require_trusted(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_lookup_")
    progress_lines = []

    async def _drop_progress():
        """Remove the deferred progress message once the real reply is out,
        so the last "Saved ...; validating audio..." line doesn't linger."""
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def _progress(msg):
        progress_lines.append(msg)
        _log(f"lookup progress: {msg}")
        try:
            await interaction.edit_original_response(
                content="\n".join(progress_lines[-4:]))
        except discord.HTTPException:
            pass

    try:
        input_path = await _save_attachment(workdir, file)
        await _progress(f"Saved {file.filename}; validating audio...")

        result = None
        try:
            result = await RECOGNITION.recognize(input_path, progress=_progress)
        except Exception as exc:  # noqa: BLE001
            _log(f"lookup Shazam check failed: {exc}")

        if not result or not result.get("matched"):
            try:
                if songrec_backend.resolve_songrec():
                    await _progress("Checking SongRec filter API...")
                    sr_result = await songrec_backend.recognize_async(input_path, progress=_progress)
                    if sr_result and sr_result.get("matched"):
                        result = sr_result
            except Exception as exc:  # noqa: BLE001
                _log(f"lookup SongRec check failed: {exc}")

        if not result or not result.get("matched"):
            embed = discord.Embed(
                title="No Match Found",
                description=(
                    f"**{file.filename}** — Neither Shazam nor SongRec found this "
                    "audio in their catalog, or the clip is too short/quiet to match confidently."
                ),
                color=RED,
            )
            _brand_footer(embed, "lookup")
        else:
            offset_note = ""
            if result.get("offset_seconds") is not None:
                offset_note = f" — matched {result['offset_seconds']:.0f}s into the song"
            embed = discord.Embed(
                title=result.get("song_name") or "(unknown title)",
                description=(f"by **{result.get('artist') or 'unknown artist'}**"
                             + offset_note),
                url=result.get("url") or None,
                color=GREEN,
            )
            if result.get("cover_url"):
                embed.set_thumbnail(url=result["cover_url"])
            if result.get("album"):
                embed.add_field(name="Album", value=result["album"], inline=True)
            if result.get("release_date"):
                embed.add_field(name="Released", value=result["release_date"], inline=True)
            provider = result.get("provider", "Shazam")
            source = result.get("url")
            source_val = f"[{provider} page]({source})" if source else provider
            embed.add_field(
                name="Source",
                value=source_val,
                inline=False)
            _brand_footer(embed, "lookup" + (" • cached" if result.get("cached") else ""))
        try:
            await interaction.followup.send(embed=embed)
        except discord.HTTPException:
            await interaction.channel.send(embed=embed)
        await _drop_progress()
        _log(f"lookup DONE for {interaction.user} (id {interaction.user.id}): "
             f"{file.filename!r} -> "
             + ((result.get("song_name", "") + " / " + result.get("artist", ""))
                if (result and result.get("matched")) else "no match"))
    except shazam_backend.InvalidAudioError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
        await _drop_progress()
    except shazam_backend.NetworkError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
        await _drop_progress()
    except shazam_backend.RateLimitedError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
        await _drop_progress()
    except shazam_backend.RecognitionError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
        await _drop_progress()
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"lookup failed: {e}", ephemeral=True)
        await _drop_progress()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _shazam_slash_original_preserved(interaction: discord.Interaction, file: discord.Attachment):
    """Original /shazam implementation (preserved, not used at runtime).

    Kept intact for reference, not called in current flow.
    """
    if not await _require_trusted(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_shazam_")
    try:
        input_path = await _save_attachment(workdir, file)
        result = await asyncio.to_thread(killa_tools._shazam_recognize_original_preserved, input_path)
        if not result.get("matched"):
            embed = discord.Embed(
                title="Song identification: no match",
                description=(
                    f"**{file.filename}** was not matched. "
                    "Try a longer or clearer clip."
                ),
                color=RED,
            )
            _brand_footer(embed, "song id")
            await interaction.followup.send(embed=embed)
            return

        title = result.get("title") or "Recognised Track"
        artist = result.get("artist")
        album = result.get("album")
        released = result.get("released")
        label = result.get("label")
        genres = result.get("genres")
        embed = discord.Embed(
            title=title,
            description=f"**{file.filename}** was identified.",
            color=PURPLE,
        )
        if artist:
            embed.add_field(name="Artist", value=artist, inline=True)
        if album:
            embed.add_field(name="Album", value=album, inline=True)
        if genres:
            embed.add_field(name="Genre", value=genres, inline=True)
        if released:
            embed.add_field(name="Released", value=released, inline=True)
        if label:
            embed.add_field(name="Label", value=label, inline=True)
        url = result.get("url")
        if url:
            embed.add_field(name="Source", value=f"[View]({url})", inline=False)
        cover = result.get("coverart")
        if cover:
            embed.set_thumbnail(url=cover)
        if title and title != "Recognised Track":
            links = killa_tools.search_links(title, artist)
            if links:
                embed.add_field(
                    name="Search",
                    value="\n".join(f"[{k}]({v})" for k, v in links.items()),
                    inline=False,
                )
        _brand_footer(embed, "song id")
        _log(f"shazam DONE for {interaction.user} (id {interaction.user.id}): "
             f"{title!r} by {artist!r}")
        await interaction.followup.send(embed=embed)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"song id failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)




@bot.tree.command(
    name="roblox",
    description="Simulate Roblox audio compression (two-pass ogg) to test your file.",
)
@app_commands.describe(
    file="The audio file to compress.",
    quality="libvorbis quality (-1..10, default 0.5).",
)
async def roblox_slash(interaction: discord.Interaction, file: discord.Attachment,
                       quality: float = None):
    if not await _require(interaction):
        return
    quality = 0.5 if quality is None else quality
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_roblox_")
    try:
        input_path = await _save_attachment(workdir, file)
        pass1 = os.path.join(workdir, "pass1.ogg")
        pass2 = os.path.join(workdir, "pass2.ogg")
        wf = os.path.join(workdir, "waveform.png")
        info, wf = await asyncio.to_thread(killa_tools.roblox_simulate,
                                           input_path, pass1, pass2, wf, quality)
        stem = os.path.splitext(file.filename or "audio")[0] or "audio"
        note = f"Two-pass libvorbis re-encode at quality {quality}."
        try:
            await interaction.followup.send(
                embed=_audio_embed("Roblox Compression Test", f"{stem}.ogg", info, note=note),
                files=[discord.File(wf, filename="waveform.png"),
                       discord.File(pass2, filename=f"{stem}.ogg")])
        except discord.HTTPException:
            # The re-encoded ogg can exceed Discord's upload limit; still post the
            # analysis embed so the compression result isn't lost.
            await interaction.followup.send(
                embed=_audio_embed("Roblox Compression Test", f"{stem}.ogg", info, note=note),
                files=[discord.File(wf, filename="waveform.png")])
            await interaction.followup.send(
                "the compressed file was too large to attach, so only the analysis is shown.",
                ephemeral=True)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"roblox processing failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _sync_all_guilds():
    """Sync the single GLOBAL command set, then delete any stale guild-scoped copies.

    The commands are now registered globally only (a global command appears once
    in every server the bot is in). Older configs also wrote a guild-scoped copy
    of each command, which made the server show every command twice. This deletes
    those leftover guild-scoped registrations so there is exactly one clean set.
    """
    total = 0
    try:
        synced = await bot.tree.sync()   # global scope (guilds + DMs + private)
        total += len(synced)
        print(f"Global command sync done ({len(synced)} commands).")
    except Exception as e:  # noqa: BLE001
        print(f"global sync failed: {e}")
    total += await _remove_guild_command_duplicates()
    return total


async def _remove_guild_command_duplicates():
    """Purge guild-scoped command registrations left in the primary guild.

    Legacy configs registered guild-scoped copies, which made the server show
    every command twice.  Registration is global-only now, so this normally
    has nothing to do: it FETCHES the primary guild's scoped commands first
    (one cheap GET) and only clears + re-syncs when leftovers actually exist.
    Purging every guild on every startup/reload burned the bot's rate-limit
    budget and lagged everything else.
    """
    if not (GUILD_ID and GUILD_ID.isdigit()):
        return 0
    gid = int(GUILD_ID)
    try:
        existing = await bot.tree.fetch_commands(guild=discord.Object(id=gid))
    except Exception as e:  # noqa: BLE001
        print(f"guild purge check failed for {gid}: {e}")
        return 0
    if not existing:
        return 0
    try:
        bot.tree.clear_commands(guild=discord.Object(id=gid))
        removed = await bot.tree.sync(guild=discord.Object(id=gid))
        if removed:
            print(f"purged {len(removed)} duplicate guild command(s) for {gid}.")
        return len(removed)
    except Exception as e:  # noqa: BLE001
        print(f"guild purge failed for {gid}: {e}")
        return 0


def _restart_bot():
    """Replace the running process so edits to the .py files are loaded.

    Discord's slash commands are only reflected after a sync, and a sync can't
    pick up file changes on an already-running process -- so this restart, which
    re-runs setup_hook and re-syncs, is what actually makes edits live. execv
    swaps the image in place; if the platform won't let us, spawn a detached
    replacement and exit so the new process takes over.
    """
    script = os.path.abspath(sys.argv[0])
    try:
        os.execv(sys.executable, [sys.executable, script])
    except (OSError, AttributeError):
        subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script))
        os._exit(0)


@bot.tree.command(
    name="download",
    description="Download a YouTube / web video or audio track via yt-dlp (personal use).",
)
@app_commands.describe(
    url="The video or audio link to download (YouTube, etc.).",
    kind="audio (mp3, default) or video (mp4, capped at 720p).",
)
@app_commands.choices(
    kind=[
        app_commands.Choice(name="Audio (mp3)", value="audio"),
        app_commands.Choice(name="Video (mp4)", value="video"),
    ],
)
async def download_slash(interaction: discord.Interaction, url: str,
                         kind: str = "audio"):
    if not await _require_trusted(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_dl_")
    try:
        info = await asyncio.to_thread(killa_tools.download_media, url, workdir, kind)
        title = info["title"]
        if len(title) > 100:
            title = title[:97] + "..."
        embed = discord.Embed(title="Downloaded", description=title, color=PURPLE)
        embed.add_field(name="Source", value=(info.get("webpage_url") or "—"), inline=False)
        embed.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
        embed.add_field(name="Uploader", value=info.get("uploader", "—"), inline=True)
        embed.add_field(name="Format", value=info.get("kind", kind), inline=True)
        embed.add_field(name="Size", value=f"{info['size'] / 1e6:.1f} MB", inline=True)
        if info.get("note"):
            embed.add_field(name="Note", value=info["note"], inline=False)
        _brand_footer(embed, "download tool")
        path = info["path"]
        filename = os.path.basename(path)
        try:
            await interaction.followup.send(
                embed=embed,
                files=[discord.File(path, filename=filename)])
        except discord.HTTPException:
            # Extremely large outputs can still exceed Discord's limit even after
            # re-encoding; keep the metadata embed so the result isn't lost.
            await interaction.followup.send(
                embed=embed,
                content="(the file was too large to attach, so only the info is shown.)",
                ephemeral=True)
        _log(f"download DONE by {interaction.user} (id {interaction.user.id}) "
             f"kind={kind} url={url} -> {filename}")
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"download failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.tree.command(name="reload", description="Restart the bot to apply code edits (owner only).")
async def reload_slash(interaction: discord.Interaction):
    if not await _require_owner(interaction):
        return
    await interaction.response.defer(thinking=True)
    # No command sync here.  Syncing (global + per-guild purge) before the
    # restart is what made /reload lag the whole bot: Discord rate-limits
    # syncs hard, so every other request queued behind them.  The restart
    # below re-runs the startup sync anyway, which is what actually makes
    # code edits live.  The process swap is intentional (execv) -- that is
    # how the new code loads.
    try:
        names = ", ".join(c.name for c in bot.tree.get_commands())
        _log(f"reload by {interaction.user} (id {interaction.user.id}): "
             f"restarting to apply code edits ({len(bot.tree.get_commands())} command(s) registered)")
        await interaction.followup.send(
            "Restarting to apply code edits. Expect ~15 seconds of downtime; "
            "the command tree re-syncs during startup.\n"
            f"Commands: {names}",
            ephemeral=True)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"reload failed: {e}", ephemeral=True)
        return
    # Give the reply a moment to deliver before swapping the process.
    await asyncio.sleep(1.0)
    _restart_bot()


def _whitelist_user_label(uid: int, target) -> str:
    """Format a clickable `<@uid>` user tag with display/username context."""
    mention = f"<@{uid}>"
    if target is None:
        return f"{mention} (`{uid}`)"
    disp = getattr(target, "display_name", None) or getattr(target, "name", None)
    uname = getattr(target, "name", None)
    if disp and uname and disp.lower() != uname.lower():
        return f"{mention} (**{disp}** · `@{uname}`)"
    if disp:
        return f"{mention} (**{disp}**)"
    return mention


async def _send_whitelist_dm(interaction, uid: int, target) -> tuple:
    """Deliver the verification DM reliably and return (ok: bool, detail: str).

    1. Searches shared guilds (cache + fetch_member) so Discord routes the DM
       through a mutual-server member relationship whenever one exists.
    2. Falls back to a fresh `client.fetch_user(uid)` / `target` and explicitly
       opens the DM channel via `create_dm()` before sending.
    3. Logs exact Discord HTTP/JSON error codes and distinguishes 50007 (no
       mutual server yet vs server DMs disabled) from transient errors.
    """
    client = getattr(interaction, "client", None)
    shared_member = None
    seen_guilds = set()
    candidate_guilds = []
    cur_guild = getattr(interaction, "guild", None)
    if cur_guild is not None:
        candidate_guilds.append(cur_guild)
    for g in getattr(client, "guilds", []) or []:
        if g is not None:
            candidate_guilds.append(g)

    for g in candidate_guilds:
        gid = getattr(g, "id", None)
        if gid is not None:
            if gid in seen_guilds:
                continue
            seen_guilds.add(gid)
        get_m = getattr(g, "get_member", None)
        m = get_m(uid) if callable(get_m) else None
        if m is None:
            fetch_m = getattr(g, "fetch_member", None)
            if callable(fetch_m):
                try:
                    m = await fetch_m(uid)
                except Exception:
                    m = None
        if m is not None:
            shared_member = m
            break

    fresh_user = None
    if shared_member is None and client is not None and hasattr(client, "fetch_user"):
        try:
            fresh_user = await client.fetch_user(uid)
        except Exception:
            fresh_user = None

    recipients = []
    for cand in (shared_member, fresh_user, target):
        if cand is not None and cand not in recipients:
            recipients.append(cand)

    embed = _whitelist_dm_embed()
    last_exc = None
    for attempt in range(2):
        for recipient in recipients:
            try:
                create_dm = getattr(recipient, "create_dm", None)
                if callable(create_dm):
                    dm_channel = await create_dm()
                    await dm_channel.send(embed=embed)
                else:
                    await recipient.send(embed=embed)
                _log(f"whitelist DM delivered to user id {uid}")
                return True, ""
            except discord.Forbidden as exc:
                last_exc = exc
                code = getattr(exc, "code", None)
                _log(f"whitelist DM forbidden for {uid} (code={code}, "
                     f"shared_guild={shared_member is not None}): {exc}")
                break
            except discord.HTTPException as exc:
                last_exc = exc
                status = getattr(exc, "status", None)
                code = getattr(exc, "code", None)
                _log(f"whitelist DM HTTP error for {uid} (status={status}, code={code}): {exc}")
                if status and status < 500 and status != 429:
                    break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _log(f"whitelist DM error for {uid}: {exc}")
        if isinstance(last_exc, discord.Forbidden):
            break
        if attempt == 0:
            await asyncio.sleep(0.8)

    code = getattr(last_exc, "code", None)
    if code == 50007 or isinstance(last_exc, discord.Forbidden):
        if shared_member is None:
            return False, (
                "Discord blocks bots from starting a brand-new DM when the user does not "
                "share a server with the bot yet (even when their DMs are open). Their "
                "verification DM has been queued and will also send automatically as soon "
                "as they run any command with the app."
            )
        return False, (
            "Discord blocked the DM (error 50007: direct messages from server members "
            "are disabled for this server, or the bot is blocked). Their verification "
            "DM has been queued for their next interaction."
        )
    return False, f"Discord could not deliver the DM ({last_exc or 'unknown error'})."


async def _deliver_pending_whitelist_dm(user_obj):
    """Send a queued verification DM when a whitelisted user interacts with the bot."""
    uid = getattr(user_obj, "id", None)
    if uid is None:
        return
    pending = _read_pending_whitelist_dms()
    if uid not in pending:
        return
    try:
        create_dm = getattr(user_obj, "create_dm", None)
        if callable(create_dm):
            dm = await create_dm()
            await dm.send(embed=_whitelist_dm_embed())
        else:
            await user_obj.send(embed=_whitelist_dm_embed())
        allow = _read_staff_whitelist()
        pending.discard(uid)
        _write_staff_whitelist(allow, pending)
        _log(f"delivered queued whitelist verification DM to user id {uid}")
    except Exception as exc:  # noqa: BLE001
        _log(f"queued whitelist DM still blocked for {uid}: {exc}")


@bot.tree.command(
    name="whitelist",
    description="Manage the extra staff allow-list (owner only).",
)
@app_commands.describe(
    action="add, remove, or list.",
    user="The Discord user to add/remove (mention them), or leave empty and use user_id.",
    user_id="Discord user id as an alternative to the @mention (for people not in a shared server).",
)
async def whitelist_slash(interaction: discord.Interaction,
                          action: str, user: discord.User = None,
                          user_id: str = None):
    if not await _require_owner(interaction):
        return
    action = (action or "").strip().lower()
    is_list = (action == "list")
    await interaction.response.defer(thinking=True, ephemeral=is_list)
    allow = _read_staff_whitelist()
    pending_dms = _read_pending_whitelist_dms()

    # /whitelist list — management view, only the requester sees it.
    if is_list:
        entries = []
        for uid in sorted(allow):
            resolved = None
            _, resolved, _ = await _resolve_whitelist_target(interaction, None, str(uid))
            entries.append(_whitelist_user_label(uid, resolved))
        embed = discord.Embed(
            title="Staff Allow-List",
            description=(
                f"{len(allow)} user(s) verified and able to use this bot as an app."
            ) if allow else "No users verified yet.",
            color=PURPLE,
        )
        if entries:
            embed.add_field(name="Whitelisted", value="\n".join(entries)[:1024],
                            inline=False)
        _advertise_fields(embed)
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    uid, target, resolve_err = await _resolve_whitelist_target(
        interaction, user, user_id)
    if resolve_err:
        embed = discord.Embed(
            title="Couldn't resolve that user",
            description=resolve_err,
            color=RED,
        )
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    if uid is None:
        embed = discord.Embed(
            title="Pick a user",
            description=(
                "Pass the Discord `user` to `add` or `remove`, e.g. "
                "`/whitelist add @user`, or use `user_id` for anyone not in a "
                "shared server, e.g. "
                "`/whitelist add user_id: 123456789012345678`. "
                "Use `/whitelist list` to see who's whitelisted."
            ),
            color=PURPLE,
        )
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    mention_tag = f"<@{uid}>"
    label = _whitelist_user_label(uid, target)
    mention_allow = discord.AllowedMentions(users=True, roles=False, everyone=False)

    if action == "add":
        if target is None:
            embed = discord.Embed(
                title="Couldn't find that user",
                description=(
                    f"No Discord user exists with id `{uid}` -- double-check the "
                    "id and try again."
                ),
                color=RED,
            )
            _brand_footer(embed, "staff whitelist")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        already_in = uid in allow
        allow.add(uid)
        _write_staff_whitelist(allow, pending_dms)

        if already_in:
            embed = discord.Embed(
                title="Already Whitelisted",
                description=(
                    f"{label} is already on the staff allow-list. "
                    "Re-sending their verification DM now."
                ),
                color=PURPLE,
            )
        else:
            embed = discord.Embed(
                title="User Whitelisted",
                description=(
                    f"{label} has been verified and can now use **KILLA'S BOSS** "
                    "as an app on their Discord account."
                ),
                color=GREEN,
            )
            embed.add_field(name="Status", value="Verified", inline=True)
            embed.add_field(name="Added by",
                            value=interaction.user.display_name, inline=True)
            embed.add_field(
                name="Use it as an app",
                value=(
                    f"{mention_tag} can add this bot under Discord -> User Settings -> Apps, "
                    "then run slash commands in a DM with the bot or in their own server."
                ),
                inline=False,
            )
        _advertise_fields(embed)
        _brand_footer(embed, "staff whitelist")
        # Include `content=mention_tag` so Discord populates `message.mentions`
        # and renders `<@uid>` as a real clickable user tag even for uncached `user_id` users!
        await interaction.followup.send(
            content=mention_tag,
            embed=embed,
            allowed_mentions=mention_allow,
            ephemeral=False,
        )

        dm_ok, dm_detail = await _send_whitelist_dm(interaction, uid, target)
        if dm_ok:
            pending_dms.discard(uid)
        else:
            pending_dms.add(uid)
        _write_staff_whitelist(allow, pending_dms)

        note = discord.Embed(
            title="Notification sent" if dm_ok else "Verification DM queued",
            description=(
                f"{label} has been DM'd that they're verified."
                if dm_ok else
                f"{label} could not be DM'd right now: {dm_detail}"
            ),
            color=GREEN if dm_ok else PURPLE,
        )
        _brand_footer(note, "staff whitelist")
        await interaction.followup.send(
            embed=note,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=False,
        )
        return

    if action == "remove":
        if uid not in allow:
            embed = discord.Embed(
                title="Not Whitelisted",
                description=f"{label} is not on the staff allow-list.",
                color=RED,
            )
            _advertise_fields(embed)
            _brand_footer(embed, "staff whitelist")
            await interaction.followup.send(
                content=mention_tag,
                embed=embed,
                allowed_mentions=mention_allow,
                ephemeral=False,
            )
            return
        allow.discard(uid)
        pending_dms.discard(uid)
        _write_staff_whitelist(allow, pending_dms)
        embed = discord.Embed(
            title="User Removed From Whitelist",
            description=f"{label} has been removed from the verified list.",
            color=RED,
        )
        embed.add_field(name="Status", value="Access revoked", inline=True)
        embed.add_field(name="Removed by",
                        value=interaction.user.display_name, inline=True)
        _advertise_fields(embed)
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(
            content=mention_tag,
            embed=embed,
            allowed_mentions=mention_allow,
            ephemeral=False,
        )
        return

    # Unknown action.
    embed = discord.Embed(
        title="Add, remove, or list",
        description=(
            "Pass `action` as `add`, `remove`, or `list` "
            "(e.g. `/whitelist add @user` or `/whitelist list`)."
        ),
        color=PURPLE,
    )
    _brand_footer(embed, "staff whitelist")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="groups",
    description="Show the Roblox groups this bot is verified to grant audio for.",
)
async def groups_slash(interaction: discord.Interaction):
    if not await _require_trusted(interaction):
        return
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        embed = await asyncio.to_thread(_build_groups_embed)
    except Exception as e:  # noqa: BLE001
        embed = discord.Embed(
            title="Couldn't load the group list", color=RED,
            description=f"Something went wrong: `{e}`")
    await interaction.followup.send(embed=embed, ephemeral=False)


@bot.event
async def on_interaction(interaction):
    """Log every slash-command use (who, when, and the options) to the console
    and to usage.log, so the command menu is traceable when you run the bot."""
    global _cmd_count
    cmd = interaction.command
    if cmd is None:
        return  # component / modal interactions aren't commands
    _cmd_count += 1
    opts = []
    for o in (interaction.data or {}).get("options", []) or []:
        if isinstance(o, dict) and o.get("name"):
            v = o.get("value")
            if isinstance(v, (list, dict)):
                v = json.dumps(v, default=str)
            opts.append(f"{o['name']}={v}")
    opt_txt = (" " + " ".join(opts)) if opts else ""
    cmd_label = getattr(cmd, "qualified_name", None) or cmd.name
    _log(f"user {interaction.user} (id {interaction.user.id}) used /{cmd_label}{opt_txt}")
    if getattr(interaction, "user", None) is not None:
        asyncio.create_task(_deliver_pending_whitelist_dm(interaction.user))


def _cleanup_stale_workdirs(max_age=24 * 3600):
    """Remove orphaned bot temp work dirs (kb_*) older than ``max_age`` seconds.

    Every work dir is removed in a ``finally``, so these should only linger if
    the process was hard-killed (crash / PC restart) mid-operation. Sweeping the
    old ones at startup keeps the temp folder from slowly filling up.
    """
    tmp = tempfile.gettempdir()
    now = time.time()
    try:
        for name in os.listdir(tmp):
            if not name.startswith("kb_"):
                continue
            d = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(d) > max_age:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


@bot.event
async def on_ready():
    global APP_OWNER_ID, _command_sync_done
    _cleanup_stale_workdirs()
    try:
        APP_OWNER_ID = bot.application.owner.id
    except Exception:
        APP_OWNER_ID = None
    print(f"Audio bot ready: {bot.user} (id {bot.user.id}), app owner {APP_OWNER_ID}")
    _log("SESSION STARTED")
    # Sync ONCE per process.  on_ready also fires on every reconnect; syncing
    # again each time burns the bot's rate-limit budget (global + guild syncs
    # are heavily throttled by Discord) and lagged every other request.  The
    # registration persists on Discord's side, so a reconnect never needs it.
    if not _command_sync_done:
        _command_sync_done = True
        # Awaited (not a background task) so the startup is guaranteed to finish
        # syncing before returning -- otherwise a newly-added command can sit
        # unsynced if the task is interrupted, and /reload would look like it
        # "didn't update".
        await _sync_all_guilds()


# Module-level once-per-process flag for the startup command sync (set in
# on_ready; /reload's restart re-runs it, reconnects do not).
_command_sync_done = False


_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_bot.pid")


def _write_pid_file():
    """Record this process's pid, so the launcher / stop script can find it.

    Ctrl+C followed by "Terminate batch job (Y/N)" can leave python alive with no
    window attached. That orphan still holds the single-instance lock, and the
    next launch would exit "already running" -- which reads as the bot refusing
    to refresh. The stop script matches on the command line anyway; this file
    just makes the running pid visible and lets the launcher report it.
    """
    try:
        with open(_PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _clear_pid_file():
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass


def _acquire_single_instance_lock():
    """Hold an OS-level lock so only ONE bot process runs at a time.

    Two processes using the same token fight over the bot's single Discord
    gateway connection: the online/offline presence mirroring flips between them
    and in-flight slash interactions get dropped with 'Unknown interaction'
    (404 / code 10062). A byte-range lock on a lock file is released by the OS
    when the process dies, so a crash can't leave a stale file blocking a restart.

    Returns the open file handle to hold for the whole process, or None if
    another instance already holds the lock.
    """
    try:
        import msvcrt  # Windows only
    except ImportError:
        return 0  # non-Windows: skip the guard
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_bot.lock")
    f = open(lock_path, "a+b")
    try:
        f.seek(0)
        if f.read(1) == b"":
            f.write(b"\x00")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        # Mark the fd close-on-exec so /reload (os.execv / Popen) doesn't carry this
        # lock into the restarted process, which would make the new one exit as
        # "already running". When the process dies (or execs), the OS releases the lock.
        os.set_inheritable(f.fileno(), False)
    except OSError:
        f.close()
        return None
    return f


if __name__ == "__main__":
    _instance_lock = _acquire_single_instance_lock()
    if _instance_lock is None:
        # Exit 2, not 0: the launcher closes the window on a clean exit, and this
        # message is the whole point of the run.
        print("KILLA'S BOSS is already running. If its window is gone, it is still "
              "alive in the background (a Ctrl+C with \"Terminate batch job\" can "
              "leave it that way) -- run stop_audio_bot.bat, or double-click "
              "run_audio_bot.bat, which clears the old instance for you.",
              flush=True)
        sys.exit(2)
    _write_pid_file()
    _log(f"KILLA'S BOSS starting; {len(bot.tree.get_commands())} commands registered: "
         f"{', '.join(c.name for c in bot.tree.get_commands())}")
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        # Ctrl+C. discord.py closes the gateway connection on the way out; say so
        # plainly and let the window close, instead of a traceback.
        print("\nStopping KILLA'S BOSS (Ctrl+C)...", flush=True)
    except discord.PrivilegedIntentsRequired:
        # Watching the owner's status (presence mirroring) is on, but the two
        # privileged intents aren't enabled in the Developer Portal, so Discord
        # refuses the login. The bot can't run as-is, but we keep this window OPEN
        # (and readable) instead of letting it vanish.
        print("\nDiscord refused the login: MIRROR_PRESENCE needs the privileged "
              "gateway intents enabled in the Developer Portal.\n"
              "  Bot -> Privileged Gateway Intents -> turn ON 'Presence Intent' and "
              "'Server Members Intent', then restart.\n"
              "  Or set MIRROR_PRESENCE=0 in .env to run WITHOUT presence mirroring "
              "right now.\n", flush=True)
        try:
            input("\nPress Enter to close this window after you've read the above...")
        except Exception:  # noqa: BLE001 - no tty (e.g. run detached); don't hang
            pass
    finally:
        # bot.run returns when the process is shutting down (Ctrl+C / close), which
        # is when the single SESSION ENDED summary is written. This beats on_disconnect
        # for the "you ended the session" case because a transient gateway drop or a
        # background reconnect doesn't end the process -- it would otherwise write a
        # misleading SESSION ENDED mid-run. If the process is killed outright, no
        # line is written (there is no clean exit to hook).
        _log_session_end()
        _clear_pid_file()
        if _instance_lock:
            try:
                _instance_lock.close()
            except Exception:  # noqa: BLE001
                pass
