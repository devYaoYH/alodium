#!/usr/bin/env python3
"""
Offline tests for the platform-keyring passphrase source: the precedence in
node_backup.config and every failure keyring_store translates.

No real keyring is ever asked. `keyring` is not even imported: keyring_store
takes the importer as a parameter, and these tests hand it a fake module with
the real package's exception hierarchy (KeyringError > NoKeyringError,
KeyringLocked). So the tests pass identically whether or not the package is
installed, and they can never read or write the operator's Keychain.

Covers:

  - source selection: any explicit override beats the keyring; none -> keyring
  - an override short-circuits: the keyring is never asked when one is set
  - the keyring lookup uses the old recipe's coordinates (alodium-restic, login name)
  - an empty keyring is an error naming the service, the account and the fix
  - the package missing, no backend, a locked store and a generic keyring
    error each become an actionable BackupError, never a traceback
  - a plaintext backend (keyrings.alt) is refused
  - put() passes the value in-process to set_password
  - generate(): 48 characters from the alphanumeric alphabet, CSPRNG by default

Run:  python3 scripts/node_backup/test_keyring.py   (from the repo root)
      ./scripts/verify-config.sh                    (runs it with the rest)
Stdlib only.
"""

import importlib
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
importlib.import_module("node_backup")
config = importlib.import_module("node_backup.config")
store = importlib.import_module("node_backup.keyring_store")

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def raises(fn, *needles):
    try:
        fn()
    except config.BackupError as exc:
        return all(n in str(exc) for n in needles)
    return False


def never(*_args):
    raise AssertionError("this source must not be consulted")


# ---- 1. source selection ---------------------------------------------------

check("source: nothing set means the platform keyring",
      config.passphrase_source({}) == config.KEYRING_SOURCE)
for name in config.EXPLICIT_SOURCES:
    check(f"source: {name} overrides the keyring",
          config.passphrase_source({name: "x"}) == name)
check("source: an empty override is not an override",
      config.passphrase_source({"RESTIC_PASSWORD_COMMAND": ""}) == config.KEYRING_SOURCE)
check("source: the explicit order is command > file > literal",
      config.EXPLICIT_SOURCES == ("RESTIC_PASSWORD_COMMAND", "RESTIC_PASSWORD_FILE",
                                  "RESTIC_PASSWORD"))

# The override is a deliberate choice; the keyring must not even be asked, so
# an existing RESTIC_PASSWORD_COMMAND setup needs no keyring package at all.
check("override: a command never consults the keyring",
      config.resolve_passphrase({"RESTIC_PASSWORD_COMMAND": "c"},
                                run_command=lambda c: "cmd", keyring_get=never) == "cmd")
check("override: a file never consults the keyring",
      config.resolve_passphrase({"RESTIC_PASSWORD_FILE": "/f"},
                                read_file=lambda p: "file", keyring_get=never) == "file")
check("override: a literal never consults the keyring",
      config.resolve_passphrase({"RESTIC_PASSWORD": "lit"}, keyring_get=never) == "lit")


# ---- 2. the keyring lookup -------------------------------------------------

asked = []


def recording_get(service, account):
    asked.append((service, account))
    return "from-keyring\n"


value = config.resolve_passphrase({}, keyring_get=recording_get, account="alice")
check("keyring: used when there is no override", value == "from-keyring")
check("keyring: asked once, for service alodium-restic and the given account",
      asked == [("alodium-restic", "alice")], detail=str(asked))
# The migration claim: `security add-generic-password -a "$USER" -s
# alodium-restic` wrote exactly these coordinates.
check("keyring: the service name is the old Keychain recipe's",
      store.SERVICE == "alodium-restic")

asked.clear()
store_default = store.default_account
store.default_account = lambda: "login-name"
try:
    config.resolve_passphrase({}, keyring_get=recording_get)
finally:
    store.default_account = store_default
check("keyring: the account defaults to the login name",
      asked == [("alodium-restic", "login-name")], detail=str(asked))

check("keyring: an empty store is an error naming service, account and the fix",
      raises(lambda: config.resolve_passphrase({}, keyring_get=lambda s, a: None,
                                               account="alice"),
             "alodium-restic", "'alice'", "backup.sh passphrase set"))
check("keyring: an empty string is as empty as None",
      raises(lambda: config.resolve_passphrase({}, keyring_get=lambda s, a: "\n",
                                               account="a"), "passphrase set"))


def failing_get(service, account):
    raise config.BackupError("store is locked")


check("keyring: a BackupError from the store propagates unchanged",
      raises(lambda: config.resolve_passphrase({}, keyring_get=failing_get, account="a"),
             "store is locked"))


# ---- 3. keyring_store against a fake package -------------------------------

class KeyringError(Exception):
    pass


class NoKeyringError(KeyringError, RuntimeError):
    pass


class KeyringLocked(KeyringError):
    pass


def fake_importer(backend_module="keyring.backends.macOS", get_raises=None,
                  stored=None, calls=None):
    """An importer serving a fake `keyring` and `keyring.errors`."""
    stored = {} if stored is None else stored
    calls = [] if calls is None else calls
    backend_cls = type("Keyring", (), {"__module__": backend_module})

    def get_password(service, account):
        calls.append(("get", service, account))
        if get_raises:
            raise get_raises
        return stored.get((service, account))

    def set_password(service, account, value):
        calls.append(("set", service, account))
        stored[(service, account)] = value

    mods = {
        "keyring": types.SimpleNamespace(get_keyring=backend_cls,
                                         get_password=get_password,
                                         set_password=set_password),
        "keyring.errors": types.SimpleNamespace(KeyringError=KeyringError,
                                                NoKeyringError=NoKeyringError,
                                                KeyringLocked=KeyringLocked),
    }
    return lambda name: mods[name]


def missing_importer(name):
    raise ModuleNotFoundError(f"No module named '{name}'")


check("store: the package missing is an error with the install recipe",
      raises(lambda: store.get("s", "a", importer=missing_importer),
             "keyring", "requirements-host.txt"))
check("store: ...which also names the package-free overrides",
      raises(lambda: store.get("s", "a", importer=missing_importer),
             "RESTIC_PASSWORD_COMMAND", "RESTIC_PASSWORD_FILE"))
check("store: no backend (headless Linux) points at RESTIC_PASSWORD_FILE",
      raises(lambda: store.get("s", "a", importer=fake_importer(
          get_raises=NoKeyringError("No recommended backend"))), "RESTIC_PASSWORD_FILE"))
check("store: a locked store says so",
      raises(lambda: store.get("s", "a", importer=fake_importer(
          get_raises=KeyringLocked("locked"))), "locked"))
check("store: any other keyring error is translated, not raised raw",
      raises(lambda: store.get("s", "a", importer=fake_importer(
          get_raises=KeyringError("boom"))), "refused to read", "boom"))
check("store: a plaintext keyrings.alt backend is refused before any read",
      raises(lambda: store.get("s", "a", importer=fake_importer(
          backend_module="keyrings.alt.file")), "plain file"))
check("store: backend_refusal accepts the platform backends",
      all(store.backend_refusal(m) == "" for m in
          ("keyring.backends.macOS", "keyring.backends.Windows",
           "keyring.backends.SecretService")))

calls = []
stored = {("alodium-restic", "alice"): "secret"}
check("store: get returns the stored value",
      store.get("alodium-restic", "alice",
                importer=fake_importer(stored=stored, calls=calls)) == "secret")
check("store: get of a missing entry is None, not an error",
      store.get("alodium-restic", "bob", importer=fake_importer(stored=stored)) is None)

calls.clear()
store.put("alodium-restic", "carol", "new-secret",
          importer=fake_importer(stored=stored, calls=calls))
check("store: put writes through set_password with service and account",
      stored.get(("alodium-restic", "carol")) == "new-secret"
      and calls == [("set", "alodium-restic", "carol")], detail=str(calls))
check("store: backend_name reports module and class",
      store.backend_name(importer=fake_importer()) == "keyring.backends.macOS.Keyring")


# ---- 4. generate -----------------------------------------------------------

generated = store.generate()
check("generate: 48 characters", len(generated) == store.GENERATED_LENGTH == 48)
check("generate: alphanumeric only, so it survives paper",
      generated.isascii() and generated.isalnum(), detail=generated)
check("generate: two calls differ (CSPRNG by default)", store.generate() != generated)
check("generate: uses the injected choice for every character",
      store.generate(choice=lambda alphabet: "Z") == "Z" * 48)


print()
if FAIL == 0:
    print("test_keyring: PASS")
    sys.exit(0)
print(f"test_keyring: FAIL ({FAIL} failures)")
sys.exit(1)
