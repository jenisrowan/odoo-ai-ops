#!/usr/bin/env python3
"""
Patch mangono-odoo-redis-session for Odoo 19 compatibility.

Odoo 19 removed/changed several internal APIs that this module relies on:
  1. `odoo.conf` -> removed; use `odoo.tools.config` instead
  2. `odoo.tools.func.lazy_property` -> deprecated; use `functools.cached_property`
  3. `odoo.tools.config.misc` -> removed; use `getattr` with fallback
  4. Session rotation reworked (soft rotation + session identifiers + device
     tracking). The stock module hard-deletes the session on every rotation and
     never refreshes create_time, so past the rotation interval it rotates on
     *every* request and drops concurrent in-flight requests (intermittent
     logouts). It also lacks delete_old_sessions/get_missing_session_identifiers/
     delete_from_identifiers. We port Odoo 19's real rotate() semantics and add
     the identifier helpers, Redis-backed, and adopt core's 84-char sid scheme so
     the identifier prefix is stable across a soft rotation.
"""
import pathlib
from importlib import metadata

# This patch does textual surgery on a specific release of the module, so it is
# pinned to exactly the version installed by templates/Dockerfile.odoo. The two
# MUST be bumped together: on a version change, re-verify every anchor below and
# update EXPECTED_VERSION. We fail the image build rather than silently ship a
# store patched against the wrong source.
EXPECTED_VERSION = "1.4.1"
_installed = metadata.version("mangono-odoo-redis-session")
if _installed != EXPECTED_VERSION:
    raise SystemExit(
        f"patch_redis_session: mangono-odoo-redis-session=={_installed} installed "
        f"but this patch targets =={EXPECTED_VERSION}. Pin the same version in "
        f"templates/Dockerfile.odoo, or update this patch to match the new source."
    )

ADDON_DIR = pathlib.Path(
    "/usr/local/lib/python3.12/dist-packages/odoo/addons/redis_session_store"
)

# --- Patch __init__.py ---
init_file = ADDON_DIR / "__init__.py"
code = init_file.read_text()

# 1. Replace `odoo.conf.server_wide_modules` with `odoo.tools.config['server_wide_modules']`
code = code.replace(
    "odoo.conf.server_wide_modules",
    'odoo.tools.config["server_wide_modules"]',
)

# 1.5 Replace deprecated lazy_property with functools.cached_property
code = "import functools\n" + code
code = code.replace("odoo.tools.func.lazy_property", "functools.cached_property")

# 2. Fix Python 3.12 `cached_property` monkey-patching and cached instance
# When `session_store` is attached to the class dynamically, Python 3.12 requires
# __set_name__ to be called manually or `attrname` & `lock` to be populated.
# Additionally, `root` may have already evaluated the property, so we must clear it.
code = code.replace(
    "odoo.http.Application.session_store = session_store",
    "odoo.http.Application.session_store = session_store\n"
    '        if hasattr(session_store, "__set_name__"):\n'
    '            session_store.__set_name__(odoo.http.Application, "session_store")\n'
    '        else:\n'
    '            import threading\n'
    '            session_store.attrname = "session_store"\n'
    '            session_store.lock = threading.RLock()\n'
    '        if hasattr(odoo.http, "root"):\n'
    '            odoo.http.root.__dict__.pop("session_store", None)'
)

init_file.write_text(code)
print(f"  Patched {init_file}")

# --- Patch redis_config.py ---
config_file = ADDON_DIR / "redis_config.py"
code = config_file.read_text()

# 3. Replace `odoo_config.misc.get(...)` with `getattr(odoo_config, 'misc', {}).get(...)`
code = code.replace(
    'odoo_config.misc.get("redis_sessions_store", {})',
    'getattr(odoo_config, "misc", {}).get("redis_sessions_store", {})',
)

config_file.write_text(code)
print(f"  Patched {config_file}")

# --- Patch redis_session.py ---
session_file = ADDON_DIR / "redis_session.py"
code = session_file.read_text()

# Odoo 19's ported soft-rotation logic needs the `time` module.
code = code.replace(
    "import json\nimport logging\nimport warnings",
    "import json\nimport logging\nimport time\nimport warnings",
)

# Fix AttributeError: 'Session' object has no attribute 'expiration' in Odoo 19
code = code.replace(
    "expiration = session.expiration or self.anon_expiration",
    "expiration = getattr(session, 'expiration', None) or self.anon_expiration"
)
code = code.replace(
    "expiration = session.expiration or self.expiration",
    "expiration = getattr(session, 'expiration', None) or self.expiration"
)

# Replace the pre-19 rotate() with Odoo 19's real rotation semantics and add the
# session-identifier helpers the store is now expected to provide. Ports
# http.FilesystemSessionStore, Redis-backed via our own save/get/delete.
OLD_ROTATE = (
    "    def rotate(self, session, env):\n"
    "        self.delete(session)\n"
    "        session.sid = self.generate_key()\n"
    "        if session.uid and env:\n"
    "            session.session_token = security.compute_session_token(session, env)\n"
    "        self.save(session)"
)
NEW_ROTATE = '''\
    def generate_key(self, salt=None):
        # Odoo 19 splits the sid into a stable identifier (first
        # STORED_SESSION_BYTES chars, stored as res.device.log.session_identifier)
        # and a rotating remainder. Reuse core's 84-char base64 scheme; the
        # inherited werkzeug default is a 40-char sha1 with no room for a rotating
        # suffix, which would make soft rotation a no-op (next_sid == old sid).
        return http.FilesystemSessionStore.generate_key(self, salt)

    def is_valid_key(self, key):
        return http.FilesystemSessionStore.is_valid_key(self, key)

    def rotate(self, session, env, soft=False):
        # Ported from Odoo 19 http.FilesystemSessionStore.rotate. A soft rotation
        # (every SESSION_ROTATION_INTERVAL for logged-in users) keeps the old
        # session alive in Redis for a short grace period so concurrent in-flight
        # requests still carrying the old cookie are not logged out, and refreshes
        # create_time so rotation happens once per interval, not on every request.
        if soft:
            static = session.sid[:http.STORED_SESSION_BYTES]
            recent_session = self.get(session.sid)
            if "next_sid" in recent_session:
                # A concurrent request already rotated; adopt its new sid.
                session.sid = recent_session["next_sid"]
                return
            next_sid = static + self.generate_key()[http.STORED_SESSION_BYTES:]
            session["next_sid"] = next_sid
            session["deletion_time"] = time.time() + http.SESSION_DELETION_TIMER
            self.save(session)
            # Prepare the new session; the old one is GC'd by delete_old_sessions.
            session["gc_previous_sessions"] = True
            session.sid = next_sid
            del session["deletion_time"]
            del session["next_sid"]
        else:
            self.delete(session)
            session.sid = self.generate_key()
        if session.uid:
            assert env, "saving this session requires an environment"
            session.session_token = security.compute_session_token(session, env)
        session.should_rotate = False
        session["create_time"] = time.time()
        self.save(session)

    def delete_old_sessions(self, session):
        # Ported from Odoo 19: once the grace period elapses, purge the previous
        # (pre-rotation) session sharing this session's identifier prefix.
        if "gc_previous_sessions" in session:
            if session["create_time"] + http.SESSION_DELETION_TIMER < time.time():
                self.delete_from_identifiers([session.sid[:http.STORED_SESSION_BYTES]])
                del session["gc_previous_sessions"]
                self.save(session)

    def get_missing_session_identifiers(self, identifiers):
        # Return the identifiers (first STORED_SESSION_BYTES chars of a sid) with
        # no live session in Redis; used by the res.device revocation cron. Scan
        # the session keyspace once (non-blocking SCAN) and diff, instead of one
        # lookup per identifier over a potentially huge candidate set.
        identifiers = set(identifiers)
        if not identifiers:
            return identifiers
        prefix_len = len(self.prefix)
        existing = set()
        for key in self.redis.scan_iter(match=f"{self.prefix}*", count=1000):
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            existing.add(key[prefix_len:][:http.STORED_SESSION_BYTES])
        return identifiers - existing

    def delete_from_identifiers(self, identifiers):
        # Delete every session whose sid starts with one of the given identifiers
        # (device revocation and previous-session GC). The regex check mirrors
        # core and prevents deleting sessions from a forged identifier.
        keys = []
        for identifier in identifiers:
            if not http._session_identifier_re.match(identifier):
                raise ValueError(
                    "Identifier format incorrect, did you pass in a string instead of a list?"
                )
            keys.extend(
                self.redis.scan_iter(match=f"{self.build_key(identifier)}*", count=1000)
            )
        # Delete one key at a time: on a cluster (ElastiCache/Valkey Serverless)
        # the full sids sharing an identifier prefix still hash to different
        # slots, so a single multi-key DELETE would raise CROSSSLOT.
        for key in keys:
            self.redis.delete(key)'''
# The version is pinned above, so this anchor is guaranteed present; the check
# only exists so a silently-failing str.replace can never ship a no-op patch.
if OLD_ROTATE not in code:
    raise SystemExit("patch_redis_session: rotate() anchor not found in redis_session.py.")
code = code.replace(OLD_ROTATE, NEW_ROTATE)

session_file.write_text(code)
print(f"  Patched {session_file}")

print("All Odoo 19 patches applied successfully.")
