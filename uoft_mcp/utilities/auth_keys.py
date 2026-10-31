"""Encryption-key storage: the OS keyring first, a private key file when none exists."""

import getpass
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet
from platformdirs import user_config_path

KEY_SERVICE = "uoft-mcp"
KEY_ACCOUNT = "browser-state-v1"
KEY_FILE_NAME = "session.key"
KEY_FILE_ENV = "UOFT_MCP_KEY_FILE"
DISABLE_KEYRING_ENV = "UOFT_MCP_DISABLE_KEYRING"


class SessionStoreError(Exception):
    """A sanitized, user-facing storage failure."""


class KeyStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...
    def delete(self) -> None: ...


class KeyringUnavailable(SessionStoreError):
    """No secure OS credential backend exists (as opposed to a locked or failing one)."""


class OSKeyStore:
    """Use only the secure OS backends shipped by keyring, never plaintext plugins."""

    def _backend(self):
        import keyring

        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        allowed = {
            "keyring.backends.Windows",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
        }
        for candidate in candidates:
            if type(candidate).__module__ in allowed and candidate.priority > 0:
                return candidate
        raise KeyringUnavailable("Secure OS storage is unavailable.")

    def get(self) -> str | None:
        return self._backend().get_password(KEY_SERVICE, KEY_ACCOUNT)

    def set(self, value: str) -> None:
        self._backend().set_password(KEY_SERVICE, KEY_ACCOUNT, value)

    def delete(self) -> None:
        backend = self._backend()
        if backend.get_password(KEY_SERVICE, KEY_ACCOUNT) is not None:
            backend.delete_password(KEY_SERVICE, KEY_ACCOUNT)


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_principal() -> str:
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def restrict_to_current_user(path: Path, directory: bool = False) -> None:
    """Make `path` readable only by the current OS user; raise if that cannot be ensured.

    POSIX uses mode bits. Windows replaces the ACL with a single entry for the current
    user and removes inherited entries, which `chmod` cannot express.
    """
    try:
        if not _is_windows():
            path.chmod(0o700 if directory else 0o600)
            return
        grant = f"{_windows_principal()}:" + ("(OI)(CI)F" if directory else "F")
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise OSError("icacls failed")
    except (OSError, subprocess.SubprocessError):
        raise SessionStoreError("Could not restrict local session file permissions.") from None


def _has_loose_permissions(path: Path) -> bool:
    """POSIX only: group/other access, or a file owned by someone else."""
    info = path.stat()
    return bool(info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)) or info.st_uid != os.getuid()


class FileKeyStore:
    """A Fernet key in a file only the current user can read (like an SSH private key)."""

    def __init__(self, path: Path):
        self.path = path

    def get(self) -> str | None:
        try:
            if not _is_windows() and _has_loose_permissions(self.path):
                if self.path.stat().st_uid != os.getuid():
                    raise SessionStoreError("Session key file belongs to another user.")
                restrict_to_current_user(self.path)  # Tighten rather than trust a loose file.
            value = self.path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except UnicodeDecodeError:
            return None
        except OSError:
            raise SessionStoreError("Cannot read the session key file.") from None
        try:
            Fernet(value.encode())
        except ValueError:
            return None  # A damaged key is the same as no key: the old session is unreadable.
        return value

    def set(self, value: str) -> None:
        temporary: str | None = None
        try:
            created = not self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created and not _is_windows():
                self.path.parent.chmod(0o700)
            fd, temporary = tempfile.mkstemp(prefix="key-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            restrict_to_current_user(Path(temporary))  # Before the key is visible by name.
            os.replace(temporary, self.path)
            temporary = None
        except SessionStoreError:
            raise
        except OSError:
            raise SessionStoreError("Cannot write the session key file.") from None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise SessionStoreError("Cannot remove the session key file.") from None


class FallbackKeyStore:
    """Prefer `primary`; use `fallback` only when no secure OS backend exists at all.

    A locked or failing keyring is not a missing keyring: its errors propagate so a
    stored key is never silently shadowed by a different one.
    """

    def __init__(self, primary: KeyStore, fallback: KeyStore):
        self.primary = primary
        self.fallback = fallback

    def get(self) -> str | None:
        try:
            key = self.primary.get()
        except KeyringUnavailable:
            key = None
        if key is not None:
            return key
        return self.fallback.get()

    def set(self, value: str) -> None:
        try:
            self.primary.set(value)
        except KeyringUnavailable:
            self.fallback.set(value)
            return
        try:
            self.fallback.delete()  # A stale file key must not outlive the keyring key.
        except SessionStoreError:
            pass

    def delete(self) -> None:
        try:
            self.primary.delete()
        except KeyringUnavailable:
            pass
        finally:
            self.fallback.delete()


def default_key_file(directory: Path | None = None) -> Path:
    """`UOFT_MCP_KEY_FILE`, else beside an explicitly chosen auth dir, else the config dir."""
    override = os.environ.get(KEY_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if directory is not None:
        return directory / KEY_FILE_NAME
    return user_config_path("uoft-mcp", appauthor=False) / KEY_FILE_NAME


def default_key_store(directory: Path | None = None) -> KeyStore:
    """`directory` is only set when the auth dir was chosen explicitly (argument or env)."""
    files = FileKeyStore(default_key_file(directory))
    if os.environ.get(DISABLE_KEYRING_ENV):
        return files
    return FallbackKeyStore(OSKeyStore(), files)
