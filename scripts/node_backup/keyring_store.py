"""
keyring_store — the platform secret store, and the one third-party import.

The restic passphrase lives in whatever secret store the host OS provides:
the macOS Keychain, Windows Credential Manager, or the Secret Service on
Linux. Python's `keyring` package is the one interface to all three, so this
module is the only place that imports it. The import is lazy: backup.py stays
stdlib-only unless the keyring is the chosen source, so an operator who keeps
RESTIC_PASSWORD_COMMAND in backup.env never needs it installed.

Every keyring failure becomes a BackupError that says what to do next, and
the import is injected so the tests exercise each failure without the package
or a real store.

The coordinates, service `alodium-restic` with the login name as the account,
are the ones the old `security add-generic-password -a "$USER" -s
alodium-restic` recipe wrote. keyring's macOS backend looks up generic
passwords by exactly (kSecAttrService, kSecAttrAccount), so an item created
that way is read unchanged.
"""

import getpass
import importlib

from .config import BackupError

SERVICE = "alodium-restic"

# Pinned in scripts/requirements-host.txt. The venv lives under ~/.alodium
# because backup.sh prefers its interpreter: PEP 668 Pythons (Homebrew,
# Debian/Ubuntu, so WSL2 too) refuse a bare `pip install`.
INSTALL_HINT = (
    "python3 -m venv ~/.alodium/venv && "
    "~/.alodium/venv/bin/pip install -r scripts/requirements-host.txt "
    "(backup.sh uses that interpreter when it exists)")

# Backends that satisfy keyring's interface but do not keep a secret secret.
# keyrings.alt's PlaintextKeyring writes the passphrase to a file under
# ~/.local/share — worse than RESTIC_PASSWORD_FILE, because nobody chose it.
_REFUSED_BACKEND_PREFIXES = ("keyrings.alt",)


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
GENERATED_LENGTH = 48   # ~285 bits; alphanumeric so it survives being copied onto paper


def generate(choice=None) -> str:
    """A fresh passphrase from the OS CSPRNG, the same shape as the old recipe
    (`tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48`)."""
    if choice is None:
        import secrets
        choice = secrets.choice
    return "".join(choice(_ALPHABET) for _ in range(GENERATED_LENGTH))


def default_account() -> str:
    """The login name: $USER on macOS/Linux, %USERNAME% on Windows."""
    return getpass.getuser()


def backend_refusal(module_name: str) -> str:
    """Why this backend is refused, or '' if it is acceptable."""
    if module_name.startswith(_REFUSED_BACKEND_PREFIXES):
        return (f"keyring backend '{module_name}' stores secrets in a plain file; "
                f"refusing it. Uninstall keyrings.alt, or set "
                f"RESTIC_PASSWORD_FILE in backup.env if a file is what you want")
    return ""


def _load(importer):
    try:
        keyring = importer("keyring")
        errors = importer("keyring.errors")
    except ImportError:
        raise BackupError(
            "the passphrase comes from the platform keyring by default, but the "
            f"Python 'keyring' package is not installed. Install it: {INSTALL_HINT}. "
            "Or set RESTIC_PASSWORD_COMMAND / RESTIC_PASSWORD_FILE in backup.env "
            "to use a source that needs no package.") from None
    backend = keyring.get_keyring()
    refusal = backend_refusal(type(backend).__module__)
    if refusal:
        raise BackupError(refusal)
    return keyring, errors, backend


def _translate(errors, exc, verb) -> BackupError:
    if isinstance(exc, errors.NoKeyringError):
        # A headless Linux box without a Secret Service daemon, usually.
        return BackupError(
            f"no usable platform keyring to {verb} the passphrase ({exc}). On a "
            f"headless host, set RESTIC_PASSWORD_FILE in backup.env (chmod 600) "
            f"instead")
    if isinstance(exc, errors.KeyringLocked):
        return BackupError(f"the platform keyring is locked — unlock it and retry ({exc})")
    return BackupError(f"platform keyring refused to {verb} the passphrase: {exc}")


def backend_name(importer=importlib.import_module) -> str:
    """The backend keyring would use, for `backup.sh passphrase check`."""
    _, _, backend = _load(importer)
    return f"{type(backend).__module__}.{type(backend).__name__}"


def get(service: str, account: str, importer=importlib.import_module) -> str | None:
    """The stored passphrase, or None if the store has no such entry."""
    keyring, errors, _ = _load(importer)
    try:
        return keyring.get_password(service, account)
    except errors.KeyringError as exc:
        raise _translate(errors, exc, "read") from None


def put(service: str, account: str, value: str, importer=importlib.import_module) -> None:
    """Store the passphrase in-process: it never reaches argv or a subprocess."""
    keyring, errors, _ = _load(importer)
    try:
        keyring.set_password(service, account, value)
    except errors.KeyringError as exc:
        raise _translate(errors, exc, "store") from None
