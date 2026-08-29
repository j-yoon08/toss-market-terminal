from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from toss_market_terminal.api_session_lock import ApiSessionLock, ApiSessionLockError


def test_acquire_then_contention_fails_fast(tmp_path: Path) -> None:
    path = tmp_path / "state" / "api-session.lock"
    holder = ApiSessionLock(path)
    holder.acquire()
    try:
        contender = ApiSessionLock(path)
        with pytest.raises(ApiSessionLockError) as caught:
            contender.acquire()
        assert "다른 API" in str(caught.value)
        assert str(path) not in str(caught.value)
    finally:
        holder.release()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_second_fd_in_same_process_also_contends(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    first = ApiSessionLock(path)
    first.acquire()
    second = ApiSessionLock(path)
    with pytest.raises(ApiSessionLockError):
        second.acquire()
    first.release()


def test_release_then_reacquire_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    first = ApiSessionLock(path)
    first.acquire()
    first.release()

    second = ApiSessionLock(path)
    second.acquire()
    try:
        third = ApiSessionLock(path)
        with pytest.raises(ApiSessionLockError):
            third.acquire()
    finally:
        second.release()


def test_closing_fd_without_release_frees_the_flock(tmp_path: Path) -> None:
    """Simulates a crash: the OS drops the flock when the fd is closed."""
    path = tmp_path / "api-session.lock"
    holder = ApiSessionLock(path)
    holder.acquire()
    fd = holder._fd
    assert fd is not None
    os.close(fd)  # crash simulation: bypass release(), just drop the fd
    holder._fd = None

    survivor = ApiSessionLock(path)
    survivor.acquire()
    survivor.release()


def test_context_manager_releases_on_normal_exit(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    with ApiSessionLock(path):
        pass
    other = ApiSessionLock(path)
    other.acquire()
    other.release()


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    with pytest.raises(RuntimeError, match="boom"):
        with ApiSessionLock(path):
            raise RuntimeError("boom")
    other = ApiSessionLock(path)
    other.acquire()
    other.release()


def test_context_manager_releases_on_keyboard_interrupt(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    with pytest.raises(KeyboardInterrupt):
        with ApiSessionLock(path):
            raise KeyboardInterrupt
    other = ApiSessionLock(path)
    other.acquire()
    other.release()


def test_repeated_acquire_release_cycles_reuse_the_same_inode(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    inode = None
    for _ in range(5):
        lock = ApiSessionLock(path)
        lock.acquire()
        current_inode = path.stat().st_ino
        if inode is None:
            inode = current_inode
        assert current_inode == inode
        lock.release()
    assert path.exists()


def test_final_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    path = leaf / "api-session.lock"
    path.symlink_to(target)

    with pytest.raises(ApiSessionLockError) as caught:
        ApiSessionLock(path).acquire()

    assert "UNSAFE_LOCK_FILE" in str(caught.value)
    assert target.read_text(encoding="utf-8") == "unchanged"
    assert str(path) not in str(caught.value)


def test_final_hardlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    target.chmod(0o600)
    path = leaf / "api-session.lock"
    os.link(target, path)

    with pytest.raises(ApiSessionLockError) as caught:
        ApiSessionLock(path).acquire()

    assert "UNSAFE_LOCK_FILE" in str(caught.value)
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_existing_file_wrong_mode_is_rejected(tmp_path: Path) -> None:
    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    path = leaf / "api-session.lock"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)

    with pytest.raises(ApiSessionLockError):
        ApiSessionLock(path).acquire()


def test_existing_file_foreign_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    path = leaf / "api-session.lock"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o600)

    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1234)

    with pytest.raises(ApiSessionLockError):
        ApiSessionLock(path).acquire()


def test_symlink_parent_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ApiSessionLockError):
        ApiSessionLock(linked / "api-session.lock").acquire()
    assert list(real.iterdir()) == []


def test_existing_0755_ancestor_is_preserved(tmp_path: Path) -> None:
    ancestor = tmp_path / "existing"
    ancestor.mkdir(mode=0o755)
    os.chmod(ancestor, 0o755)
    path = ancestor / "private-leaf" / "api-session.lock"

    lock = ApiSessionLock(path)
    lock.acquire()
    lock.release()

    assert stat.S_IMODE(ancestor.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_nested_missing_state_directories_are_created_private(tmp_path: Path) -> None:
    trusted = tmp_path / "home"
    trusted.mkdir(mode=0o700)
    path = trusted / ".local" / "state" / "toss-market-terminal" / "api-session.lock"

    lock = ApiSessionLock(path)
    lock.acquire()
    lock.release()

    for directory in (
        trusted / ".local",
        trusted / ".local" / "state",
        path.parent,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_nested_creation_rejects_world_writable_existing_parent(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    os.chmod(unsafe, 0o777)
    path = unsafe / "state" / "toss-market-terminal" / "api-session.lock"

    with pytest.raises(ApiSessionLockError, match="UNSAFE_LOCK_DIRECTORY"):
        ApiSessionLock(path).acquire()

    assert not (unsafe / "state").exists()


def test_repr_redacts_path(tmp_path: Path) -> None:
    path = tmp_path / "secret-name" / "api-session.lock"
    rendered = repr(ApiSessionLock(path))
    assert str(path) not in rendered
    assert "<redacted>" in rendered


def test_marker_never_contains_secrets(tmp_path: Path) -> None:
    path = tmp_path / "api-session.lock"
    lock = ApiSessionLock(path)
    lock.acquire()
    lock.release()
    contents = path.read_text(encoding="utf-8")
    assert "tsck_live_" not in contents
    assert "tssk_live_" not in contents
    assert "Bearer" not in contents
    assert len(contents.encode("utf-8")) <= 200
