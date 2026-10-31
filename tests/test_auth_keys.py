"""Key storage tests: file fallback, permissions, and unreadable stores. No real keyring."""

import os
import stat
import subprocess
from types import SimpleNamespace

import keyring
import pytest
from cryptography.fernet import Fernet
from test_auth import manager

from uoft_mcp import cli
from uoft_mcp.utilities import auth_keys
from uoft_mcp.utilities.auth_keys import (
    DISABLE_KEYRING_ENV,
    KEY_FILE_ENV,
    FallbackKeyStore,
    FileKeyStore,
    KeyringUnavailable,
    OSKeyStore,
    SessionStoreError,
    default_key_file,
    default_key_store,
    restrict_to_current_user,
)
from uoft_mcp.utilities.auth_store import AUTH_DIR_ENV, EMPTY_STATE, SessionStore

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")


class MemoryKeys:
    def __init__(self):
        self.value = None

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def delete(self):
        self.value = None


@pytest.fixture
def state():
    return {
        "cookies": [{"name": "s", "value": "SECRET", "domain": "example.test", "path": "/"}],
        "origins": [{"origin": "https://example.test", "localStorage": []}],
    }


@pytest.fixture
def no_keyring(monkeypatch):
    """Headless Linux: keyring only offers its priority-0 `fail` backend."""
    monkeypatch.setattr(
        keyring,
        "get_keyring",
        lambda: SimpleNamespace(priority=0, __module__="keyring.backends.fail"),
    )
    monkeypatch.delenv(DISABLE_KEYRING_ENV, raising=False)
    monkeypatch.delenv(KEY_FILE_ENV, raising=False)
    monkeypatch.delenv(AUTH_DIR_ENV, raising=False)


def test_encryption_round_trip_with_file_key(tmp_path, state):
    keys = FileKeyStore(tmp_path / "keys" / "session.key")
    store = SessionStore(tmp_path / "auth", keys)
    store.acquire()
    assert store.load() == EMPTY_STATE
    assert store.save(state)
    ciphertext = store.path.read_bytes()
    assert b"SECRET" not in ciphertext and b"example.test" not in ciphertext
    store.close()

    reopened = SessionStore(tmp_path / "auth", FileKeyStore(tmp_path / "keys" / "session.key"))
    reopened.acquire()
    assert reopened.load() == state
    assert reopened.persistent
    reopened.close()


def test_wrong_key_is_signed_out_not_a_crash(tmp_path, state):
    store = SessionStore(tmp_path, FileKeyStore(tmp_path / "session.key"))
    store.acquire()
    store.load()
    store.save(state)
    store.keys.set(Fernet.generate_key().decode())  # e.g. the key file was replaced
    assert store.load() == EMPTY_STATE
    assert "sign in again" in store.message
    assert store.save(state)  # A fresh login recovers by overwriting the unreadable file.
    assert store.load() == state
    store.close()


@pytest.mark.parametrize("content", [b"", b"not a fernet key", b"\xff\xfe\x00"])
def test_corrupted_key_file_reads_as_no_key(tmp_path, state, content):
    path = tmp_path / "session.key"
    path.write_bytes(content)
    keys = FileKeyStore(path)
    assert keys.get() is None
    store = SessionStore(tmp_path / "auth", keys)
    store.acquire()
    assert store.load() == EMPTY_STATE
    assert store.save(state)  # Generating a new key replaces the damaged one.
    assert Fernet(keys.get().encode())
    store.close()


def test_corrupted_session_file_is_signed_out(tmp_path, state):
    store = SessionStore(tmp_path, FileKeyStore(tmp_path / "session.key"))
    store.acquire()
    store.load()
    store.save(state)
    store.path.write_bytes(store.path.read_bytes()[:-8])  # Truncated ciphertext.
    assert store.load() == EMPTY_STATE
    store.path.write_bytes(b"")
    assert store.load() == EMPTY_STATE
    store.close()


def test_forget_removes_session_and_key_file(tmp_path, state):
    keys = FileKeyStore(tmp_path / "session.key")
    store = SessionStore(tmp_path / "auth", keys)
    store.acquire()
    store.load()
    store.save(state)
    assert keys.path.exists()
    store.forget()
    assert not store.path.exists() and not keys.path.exists()
    store.close()


def test_missing_keyring_backend_falls_back_to_key_file(tmp_path, state, no_keyring):
    with pytest.raises(KeyringUnavailable):
        OSKeyStore().get()
    keys = FallbackKeyStore(OSKeyStore(), FileKeyStore(tmp_path / "session.key"))
    store = SessionStore(tmp_path / "auth", keys)
    store.acquire()
    store.load()
    assert store.persistent
    assert store.save(state)
    assert (tmp_path / "session.key").exists()
    store.close()

    again = SessionStore(
        tmp_path / "auth",
        FallbackKeyStore(OSKeyStore(), FileKeyStore(tmp_path / "session.key")),
    )
    again.acquire()
    assert again.load() == state
    again.forget()
    assert not (tmp_path / "session.key").exists()
    again.close()


def test_keyring_is_preferred_and_stale_file_key_removed():
    class Files(MemoryKeys):
        pass

    primary, files = MemoryKeys(), Files()
    files.value = "stale"
    keys = FallbackKeyStore(primary, files)
    assert keys.get() == "stale"  # No key in the keyring: a previously written file key is used.
    keys.set("fresh")
    assert primary.value == "fresh" and files.value is None
    assert keys.get() == "fresh"


def test_locked_keyring_does_not_fall_back_to_file(tmp_path):
    class Locked(MemoryKeys):
        def get(self):
            raise RuntimeError("locked")

        def set(self, value):
            raise RuntimeError("locked")

    files = FileKeyStore(tmp_path / "session.key")
    keys = FallbackKeyStore(Locked(), files)
    with pytest.raises(RuntimeError):
        keys.get()
    with pytest.raises(RuntimeError):
        keys.set("x")
    assert not files.path.exists()  # Never shadow a key that is merely locked.


def test_default_store_selection_and_env_overrides(tmp_path, monkeypatch, no_keyring):
    assert isinstance(default_key_store(), FallbackKeyStore)
    assert default_key_file().name == "session.key"
    assert default_key_file(tmp_path) == tmp_path / "session.key"
    monkeypatch.setenv(KEY_FILE_ENV, str(tmp_path / "custom.key"))
    assert default_key_file(tmp_path) == tmp_path / "custom.key"
    monkeypatch.setenv(DISABLE_KEYRING_ENV, "1")
    assert isinstance(default_key_store(), FileKeyStore)
    monkeypatch.setenv(AUTH_DIR_ENV, str(tmp_path / "elsewhere"))
    assert SessionStore().directory == tmp_path / "elsewhere"
    assert SessionStore(tmp_path / "explicit").directory == tmp_path / "explicit"


@posix_only
def test_posix_key_and_session_files_are_private(tmp_path, state):
    keys = FileKeyStore(tmp_path / "keys" / "session.key")
    store = SessionStore(tmp_path / "auth", keys)
    store.acquire()
    store.load()
    assert store.save(state)
    assert stat.S_IMODE(keys.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    store.close()


@posix_only
def test_posix_loose_key_file_is_tightened(tmp_path):
    keys = FileKeyStore(tmp_path / "session.key")
    keys.set("x" * 5)  # Invalid key text is fine for a permission check.
    keys.path.write_text(Fernet.generate_key().decode())
    keys.path.chmod(0o644)
    assert keys.get() is not None
    assert stat.S_IMODE(keys.path.stat().st_mode) == 0o600


@posix_only
def test_posix_key_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    keys = FileKeyStore(tmp_path / "session.key")
    keys.set(Fernet.generate_key().decode())
    monkeypatch.setattr(os, "getuid", lambda: os.stat(keys.path).st_uid + 1)
    with pytest.raises(SessionStoreError, match="another user"):
        keys.get()


def test_windows_acl_command_and_failure_are_handled(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(auth_keys, "_is_windows", lambda: True)
    monkeypatch.setattr(auth_keys.subprocess, "run", run)
    path = tmp_path / "session.key"
    path.write_text("x")
    restrict_to_current_user(path)
    assert calls[0][0] == "icacls" and "/inheritance:r" in calls[0]
    assert calls[0][-1].endswith(":F")
    restrict_to_current_user(tmp_path, directory=True)
    assert calls[1][-1].endswith("(OI)(CI)F")

    monkeypatch.setattr(
        auth_keys.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 5)
    )
    with pytest.raises(SessionStoreError):
        restrict_to_current_user(path)

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(auth_keys.subprocess, "run", missing)
    with pytest.raises(SessionStoreError):
        FileKeyStore(tmp_path / "new.key").set(Fernet.generate_key().decode())
    assert not (tmp_path / "new.key").exists()
    assert not list(tmp_path.glob("key-*.tmp"))


@windows_only
def test_windows_key_file_acl_excludes_other_groups(tmp_path):
    keys = FileKeyStore(tmp_path / "session.key")
    keys.set(Fernet.generate_key().decode())
    listing = subprocess.run(
        ["icacls", str(keys.path)], capture_output=True, text=True, check=True
    ).stdout
    for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert broad not in listing
    assert keys.get() is not None


@pytest.mark.anyio
async def test_logout_alias_clears_the_store(tmp_path):
    auth, _ = manager(tmp_path, keys=FileKeyStore(tmp_path / "session.key"))
    await auth.login()
    await auth.wait_for_login()
    assert auth.store.path.exists() and (tmp_path / "session.key").exists()
    args = cli.parser().parse_args(["auth", "logout"])
    assert await cli.run_auth(args, lambda: auth) == 0
    assert not auth.store.path.exists() and not (tmp_path / "session.key").exists()
