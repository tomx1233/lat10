"""Tests for the free-tool command gate (_require_trusted).

audio_bot.py needs discord.py, a token and the whole bot runtime to import, so
(like test_bot_delivery.py) the gate functions under test are extracted from
the source with `ast` and executed against a small fake `discord` module and
stubbed predicates. The scenarios pinned here:

  * the owner may run /copyr8 from a DM (unchanged),
  * /whitelist-ed users may run EVERY free command from a DM or another
    server (their grant is id-based),
  * a user with no grant is still denied outside the primary server for
    every free command except /copyr8,
  * /copyr8 (ANYWHERE_FREE_COMMANDS) may be run from a DM or another server by
    a primary-server member holding a Mod / Trusted role, with that membership
    re-resolved against the primary server (member cache, then one API fetch),
  * the outside re-check fails CLOSED when the server or member can't be
    resolved,
  * the in-server behaviour is unchanged.
"""
import ast
import asyncio
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

NODES = (
    "FREE_COMMANDS", "ANYWHERE_FREE_COMMANDS",
    "is_free_command", "_is_anywhere_free_command",
    "_has_mod_role", "_has_trusted_role",
    "_in_primary_guild", "_outside_primary_trusted",
    "_deny_trusted", "_require_trusted",
)


def load_gate():
    tree = ast.parse((ROOT / "audio_bot.py").read_text())
    nodes = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", None)
        if name in NODES:
            nodes.append(node)
    found = {getattr(n, "name", None) or n.targets[0].id for n in nodes}
    missing = set(NODES) - found
    if missing:
        raise AssertionError(f"gate nodes missing from audio_bot.py: {missing}")
    ns = dict(GATE_STUBS)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "audio_bot.py", "exec"), ns)
    return ns


# ---- fake discord surface ----------------------------------------------------- #

class FakeHTTPException(Exception):
    pass


class FakeDiscord:
    Interaction = type("Interaction", (), {})
    Member = type("Member", (), {})
    HTTPException = FakeHTTPException
    NotFound = FakeHTTPException


class FakeRole:
    def __init__(self, rid, name):
        self.id, self.name = rid, name


class FakeMember:
    def __init__(self, uid, roles):
        self.id, self.roles = uid, roles


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeGuildObj:
    def __init__(self, gid, cached=None, fetch=None):
        self.id = gid
        self._cached = cached or {}
        self._fetch = fetch or {}

    def get_member(self, uid):
        return self._cached.get(uid)

    async def fetch_member(self, uid):
        if uid in self._fetch:
            return self._fetch[uid]
        raise FakeDiscord.NotFound("unknown member")


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeInteraction:
    def __init__(self, command_name, guild=None, user=None):
        self.command = types.SimpleNamespace(name=command_name)
        self.guild = guild
        self.user = user or FakeUser(2)
        self.response = FakeResponse()


# stubbed predicates, driven per-test through this dict
FLAGS = {"owner": False, "whitelisted": False, "offline": False, "mod": False,
         "require_called": False, "staff_denied": False}


def reset_flags(**kw):
    FLAGS.update(owner=False, whitelisted=False, offline=False, mod=False,
                 require_called=False, staff_denied=False)
    FLAGS.update(kw)


GATE_STUBS = {
    "discord": FakeDiscord,
    "PRIMARY_GUILD_ID": 100,
    "TRUSTED_ROLE_IDS": {7},
    "TRUSTED_ROLES": {"trusted"},
    "STAFF_ROLE_IDS": {5},
    "VERIFYGROUP_ROLES": {"mod", "head mod"},
    "bot": types.SimpleNamespace(get_guild=lambda gid: GUILD_HOLDER.get(0)),
    "is_owner": lambda i: FLAGS["owner"],
    "is_whitelisted": lambda i: FLAGS["whitelisted"],
    "bot_is_offline": lambda: FLAGS["offline"],
    "is_mod": lambda i: FLAGS["mod"],
    "_require": None,  # replaced below (async stub)
    "_deny_staff": None,  # replaced below (async stub)
}


async def _stub_require(interaction, staff=True):
    FLAGS["require_called"] = True
    return True


async def _stub_deny_staff(interaction):
    FLAGS["staff_denied"] = True


GATE_STUBS["_require"] = _stub_require
GATE_STUBS["_deny_staff"] = _stub_deny_staff

GUILD_HOLDER = {}  # bot.get_guild looks here; tests set/clear it

TRUSTED = FakeRole(7, "Trusted")
MOD = FakeRole(5, "Mod")
RANDOM = FakeRole(9, "Member")


class GateTests(unittest.TestCase):
    def setUp(self):
        reset_flags()
        GUILD_HOLDER.clear()
        self.ns = load_gate()
        self.gate = self.ns["_require_trusted"]

    def run_gate(self, coro):
        return asyncio.run(coro)

    # -- owner -----------------------------------------------------------------
    def test_owner_in_dm_allowed(self):
        FLAGS["owner"] = True
        inter = FakeInteraction("copyr8", guild=None, user=FakeUser(1))
        self.assertTrue(self.run_gate(self.gate(inter)))
        self.assertEqual(inter.response.sent, [])

    # -- /whitelist-ed users run every free command anywhere --------------------- #
    def test_verify_in_dm_whitelisted_allowed(self):
        FLAGS["whitelisted"] = True
        inter = FakeInteraction("verify", guild=None)
        self.assertTrue(self.run_gate(self.gate(inter)))

    def test_download_other_server_whitelisted_allowed(self):
        FLAGS["whitelisted"] = True
        inter = FakeInteraction("download", guild=FakeGuildObj(999))
        self.assertTrue(self.run_gate(self.gate(inter)))

    def test_verify_in_dm_unwhitelisted_denied(self):
        inter = FakeInteraction("verify", guild=None)
        self.assertFalse(self.run_gate(self.gate(inter)))
        content = inter.response.sent[0][0]
        self.assertIn("only work inside the server", content)

    def test_lookup_in_other_server_denied(self):
        inter = FakeInteraction("lookup", guild=FakeGuildObj(999))
        self.assertFalse(self.run_gate(self.gate(inter)))
        self.assertIn("only work inside the server", inter.response.sent[0][0])

    # -- /copyr8 outside the primary server -------------------------------------- #
    def test_copyr8_dm_whitelisted_allowed(self):
        FLAGS["whitelisted"] = True
        inter = FakeInteraction("copyr8", guild=None)
        self.assertTrue(self.run_gate(self.gate(inter)))

    def test_copyr8_dm_trusted_role_via_cache_allowed(self):
        GUILD_HOLDER[0] = FakeGuildObj(100, cached={2: FakeMember(2, [RANDOM, TRUSTED])})
        inter = FakeInteraction("copyr8", guild=None)
        self.assertTrue(self.run_gate(self.gate(inter)))

    def test_copyr8_dm_mod_role_via_fetch_allowed(self):
        GUILD_HOLDER[0] = FakeGuildObj(100, fetch={2: FakeMember(2, [MOD])})
        inter = FakeInteraction("copyr8", guild=None)
        self.assertTrue(self.run_gate(self.gate(inter)))

    def test_copyr8_dm_untrusted_denied(self):
        GUILD_HOLDER[0] = FakeGuildObj(100, fetch={2: FakeMember(2, [RANDOM])})
        inter = FakeInteraction("copyr8", guild=None)
        self.assertFalse(self.run_gate(self.gate(inter)))
        content = inter.response.sent[0][0]
        self.assertIn("outside the server", content)
        self.assertIn("Trusted", content)

    def test_copyr8_dm_fetch_fails_denied(self):
        # fail-closed: the member can't be resolved at all
        GUILD_HOLDER[0] = FakeGuildObj(100)
        inter = FakeInteraction("copyr8", guild=None)
        self.assertFalse(self.run_gate(self.gate(inter)))

    def test_copyr8_dm_no_guild_denied(self):
        # fail-closed: the bot can't see the primary server
        inter = FakeInteraction("copyr8", guild=None)
        self.assertFalse(self.run_gate(self.gate(inter)))

    def test_copyr8_other_server_whitelisted_allowed(self):
        FLAGS["whitelisted"] = True
        inter = FakeInteraction("copyr8", guild=FakeGuildObj(999))
        self.assertTrue(self.run_gate(self.gate(inter)))

    # -- away-lockdown ordering --------------------------------------------------- #
    def test_offline_lockdown_blocks_trusted_dm(self):
        FLAGS["offline"] = True
        GUILD_HOLDER[0] = FakeGuildObj(100, fetch={2: FakeMember(2, [TRUSTED])})
        inter = FakeInteraction("copyr8", guild=None)
        self.assertFalse(self.run_gate(self.gate(inter)))
        self.assertTrue(FLAGS["staff_denied"])

    # -- in-server behaviour unchanged --------------------------------------------- #
    def test_in_guild_trusted_role_allowed(self):
        inter = FakeInteraction("copyr8",
                                guild=FakeGuildObj(100),
                                user=FakeMember(2, [TRUSTED]))
        self.assertTrue(self.run_gate(self.gate(inter)))

    def test_in_guild_no_role_denied(self):
        inter = FakeInteraction("copyr8",
                                guild=FakeGuildObj(100),
                                user=FakeMember(2, [RANDOM]))
        self.assertFalse(self.run_gate(self.gate(inter)))
        self.assertIn("Trusted", inter.response.sent[0][0])

    # -- safety net ----------------------------------------------------------------- #
    def test_non_free_command_falls_back_to_staff_gate(self):
        inter = FakeInteraction("roblox", guild=None)
        self.assertTrue(self.run_gate(self.gate(inter)))
        self.assertTrue(FLAGS["require_called"])


if __name__ == "__main__":
    unittest.main()
