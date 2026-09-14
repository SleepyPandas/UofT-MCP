"""Encrypted local browser state; no credentials or response bodies belong here."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock, Timeout
from platformdirs import user_data_path

EMPTY_STATE: dict[str, Any] = {"cookies": [], "origins": []}
KEY_SERVICE = "uoft-mcp"
KEY_ACCOUNT = "browser-state-v1"


class SessionStoreError(Exception):
    """A sanitized, user-facing storage failure."""


class SessionInUse(SessionStoreError):
    """Another local MCP owns authentication."""


class KeyStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...
    def delete(self) -> None: ...


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
        raise SessionStoreError("Secure OS storage is unavailable.")

    def get(self) -> str | None:
        return self._backend().get_password(KEY_SERVICE, KEY_ACCOUNT)

    def set(self, value: str) -> None:
        self._backend().set_password(KEY_SERVICE, KEY_ACCOUNT, value)

    def delete(self) -> None:
        backend = self._backend()
        if backend.get_password(KEY_SERVICE, KEY_ACCOUNT) is not None:
            backend.delete_password(KEY_SERVICE, KEY_ACCOUNT)


def merge_api_cookies(browser_state: dict, api_state: dict) -> dict:
    """API contexts own current cookies; retain browser local storage unchanged."""
    merged = deepcopy(browser_state)
    merged["cookies"] = deepcopy(api_state["cookies"])
    merged["origins"] = deepcopy(browser_state.get("origins", []))
    return merged


class SessionStore:
    """One OS account, one lock owner, one encrypted snapshot shared by both apps."""

    def __init__(self, directory: Path | None = None, keys: KeyStore | None = None):
        self.directory = directory or user_data_path("uoft-mcp", appauthor=False) / "auth"
        self.path = self.directory / "session.enc"
        self.keys = keys or OSKeyStore()
        self._lock = FileLock(self.directory / "session.lock", thread_local=False)
        self._owned = False
        self.persistent = False
        self.message = "Saved sessions have not been opened."

    def acquire(self) -> None:
        if self._owned:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                self.directory.chmod(0o700)
            self._lock.acquire(timeout=0)
        except Timeout:
            raise SessionInUse(
                "UofT session is in use. Close authentication in the other MCP client and retry."
            ) from None
        except OSError:
            raise SessionStoreError("Cannot open the local UofT session directory.") from None
        self._owned = True

    def _require_lock(self) -> None:
        if not self._owned:
            raise SessionStoreError("Open the session store before using it.")

    def load(self, remember: bool = True) -> dict:
        self._require_lock()
        self.persistent = False
        if not remember:
            self.message = "Memory-only session; this login will not be saved."
            return deepcopy(EMPTY_STATE)
        try:
            key = self.keys.get()
        except Exception:
            self.message = "Secure OS storage unavailable; using a memory-only session."
            return deepcopy(EMPTY_STATE)
        self.persistent = True
        self.message = "Remembering this session on this computer."
        if not self.path.exists():
            return deepcopy(EMPTY_STATE)
        try:
            if key is None:
                raise ValueError
            payload = json.loads(Fernet(key.encode()).decrypt(self.path.read_bytes()))
            state = payload["state"]
            if (
                payload["version"] != 1
                or not isinstance(state, dict)
                or not isinstance(state["cookies"], list)
                or not isinstance(state["origins"], list)
            ):
                raise ValueError
        except (InvalidToken, ValueError, KeyError, TypeError, AttributeError, OSError):
            self.message = "Saved session unavailable or incompatible; sign in again."
            return deepcopy(EMPTY_STATE)
        return state

    def save(self, state: dict) -> bool:
        self._require_lock()
        if not self.persistent:
            return False
        temporary: str | None = None
        try:
            key = self.keys.get()
            if key is None:
                key = Fernet.generate_key().decode()
                self.keys.set(key)
            encrypted = Fernet(key.encode()).encrypt(
                json.dumps({"version": 1, "state": state}).encode()
            )
            fd, temporary = tempfile.mkstemp(prefix="session-", suffix=".enc", dir=self.directory)
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.message = "Session saved securely on this computer."
            return True
        except Exception:
            self.persistent = False
            self.message = "Could not save securely; current session is memory-only."
            return False
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass  # Encrypted residue is removed by Forget; never mask the result.

    def forget(self) -> None:
        self._require_lock()
        try:
            self.path.unlink(missing_ok=True)
            for path in self.directory.glob("session-*.enc"):
                path.unlink(missing_ok=True)
        except OSError:
            raise SessionStoreError("Could not remove saved UofT session files.") from None
        try:
            self.keys.delete()
        except Exception:
            raise SessionStoreError(
                "Saved session files removed, but the OS key could not be removed. "
                "Unlock your secure credential store and run Forget again."
            ) from None
        self.persistent = False
        self.message = "Local UofT session forgotten."

    def close(self) -> None:
        if self._owned:
            self._lock.release()
            self._owned = False
