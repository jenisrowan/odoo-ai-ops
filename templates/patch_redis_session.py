#!/usr/bin/env python3
"""
Patch mangono-odoo-redis-session for Odoo 19 compatibility.

Odoo 19 removed/changed several internal APIs that this module relies on:
  1. `odoo.conf` -> removed; use `odoo.tools.config` instead
  2. `odoo.tools.func.lazy_property` -> deprecated; use `functools.cached_property`
  3. `odoo.tools.config.misc` -> removed; use `getattr` with fallback
"""
import re
import pathlib

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

# Fix AttributeError: 'Session' object has no attribute 'expiration' in Odoo 19
code = code.replace(
    "expiration = session.expiration or self.anon_expiration",
    "expiration = getattr(session, 'expiration', None) or self.anon_expiration"
)
code = code.replace(
    "expiration = session.expiration or self.expiration",
    "expiration = getattr(session, 'expiration', None) or self.expiration"
)

# Fix AttributeError: 'RedisSessionStore' object has no attribute 'delete_old_sessions'
code = code.replace(
    "    def save(self, session):",
    "    def delete_old_sessions(self, session):\n"
    "        pass\n\n"
    "    def get_missing_session_identifiers(self, identifiers):\n"
    "        missing = set()\n"
    "        for identifier in set(identifiers):\n"
    "            if not self.redis.exists(self.build_key(identifier)):\n"
    "                missing.add(identifier)\n"
    "        return missing\n\n"
    "    def save(self, session):"
)

session_file.write_text(code)
print(f"  Patched {session_file}")

print("All Odoo 19 patches applied successfully.")
