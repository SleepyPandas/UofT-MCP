"""Session persistence tests use synthetic state and a fake OS credential store."""

import json

import pytest
from cryptography.fernet import Fernet

from uoft_mcp.auth_store import (
    EMPTY_STATE,
    OSKeyStore,
    SessionInUse,
    SessionStore,
    SessionStoreError,
    merge_api_cookies,
)


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
        "cookies": [
            {
                "name": "session",
                "value": "SECRET",
                "domain": "example.test",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
                "expires": -1,
            }
        ],
        "origins": [
            {
                "origin": "https://example.test",
                "localStorage": [{"name": "auth", "value": "LOCAL_SECRET"}],
            }
        ],
    }


def test_restore_after_new_process_store_and_forget(tmp_path, state):
    keys = MemoryKeys()
    first = SessionStore(tmp_path, keys)
    first.acquire()
    assert first.load() == EMPTY_STATE
    assert first.save(state)
    assert b"SECRET" not in first.path.read_bytes()
    first.close()
    second = SessionStore(tmp_path, keys)
    second.acquire()
    assert second.load() == state
    second.forget()
    assert keys.value is None
    assert not second.path.exists()
    second.close()


def test_exclusive_lock_and_release(tmp_path):
    first, second = SessionStore(tmp_path), SessionStore(tmp_path)
    first.acquire()
    try:
        with pytest.raises(SessionInUse):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


@pytest.mark.parametrize("kind", ["corrupt", "version", "missing_key", "shape"])
def test_unavailable_state_is_not_authentication(tmp_path, state, kind):
    keys = MemoryKeys()
    store = SessionStore(tmp_path, keys)
    store.acquire()
    store.load()
    store.save(state)
    if kind == "corrupt":
        store.path.write_bytes(b"not encrypted")
    elif kind == "missing_key":
        keys.value = None
    else:
        payload = {
            "version": 99 if kind == "version" else 1,
            "state": state if kind == "version" else [],
        }
        store.path.write_bytes(Fernet(keys.value.encode()).encrypt(json.dumps(payload).encode()))
    assert store.load() == EMPTY_STATE
    assert "unavailable" in store.message
    store.close()


def test_keyring_unavailable_falls_back_without_plaintext(tmp_path, state):
    class LockedKeys(MemoryKeys):
        def get(self):
            raise RuntimeError("SECRET")

    store = SessionStore(tmp_path, LockedKeys())
    store.acquire()
    assert store.load() == EMPTY_STATE
    assert not store.save(state)
    assert not store.path.exists()
    assert "SECRET" not in store.message
    store.close()


def test_memory_only_does_not_load_or_overwrite_existing_login(tmp_path, state):
    keys = MemoryKeys()
    store = SessionStore(tmp_path, keys)
    store.acquire()
    store.load()
    store.save(state)
    original = store.path.read_bytes()
    assert store.load(remember=False) == EMPTY_STATE
    assert not store.save(EMPTY_STATE)
    assert store.path.read_bytes() == original
    store.close()


def test_failed_atomic_save_keeps_previous_state(tmp_path, state, monkeypatch):
    store = SessionStore(tmp_path, MemoryKeys())
    store.acquire()
    store.load()
    store.save(state)
    original = store.path.read_bytes()

    def fail(*args):
        raise OSError("SECRET")

    monkeypatch.setattr("uoft_mcp.auth_store.os.replace", fail)
    assert not store.save(EMPTY_STATE)
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob("session-*.enc"))
    assert "SECRET" not in store.message
    store.close()


def test_cookie_rotation_preserves_browser_storage(state):
    updated = merge_api_cookies(state, {"cookies": [], "origins": []})
    assert updated["cookies"] == []
    assert updated["origins"] == state["origins"]
    updated["origins"].clear()
    assert state["origins"]


def test_insecure_keyring_is_rejected(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: MemoryKeys())
    with pytest.raises(SessionStoreError, match="unavailable"):
        OSKeyStore().get()


def test_requires_lock(tmp_path):
    with pytest.raises(SessionStoreError):
        SessionStore(tmp_path).load()
