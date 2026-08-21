#!/usr/bin/env python3
"""Atomic text persistence that preserves immutable-release symlinks.

The logical destination may be a regular path or a symlink exposed by an
immutable release. For symlinks, the complete target chain must already
resolve to a regular file. The temporary file and final ``os.replace`` are
performed beside that real target, so the logical symlink is never replaced.

For non-symlink destinations, a missing file is allowed when its existing
parent directory resolves unambiguously. Broken links, loops, missing parents,
and non-regular targets fail closed.
"""

import os
import stat
import tempfile


class AtomicPersistenceError(RuntimeError):
    """The destination cannot be resolved safely for an atomic write."""


def resolve_atomic_target(destination):
    """Return the real file path that an atomic write must replace safely."""
    logical = os.path.abspath(os.fspath(destination))
    name = os.path.basename(logical)
    if not name:
        raise AtomicPersistenceError('atomic destination must name a file')

    if os.path.islink(logical):
        try:
            target = os.path.realpath(logical, strict=True)
        except (OSError, RuntimeError) as exc:
            raise AtomicPersistenceError(
                f'atomic destination symlink does not resolve safely: {logical}'
            ) from exc
        try:
            target_stat = os.stat(target)
        except OSError as exc:
            raise AtomicPersistenceError(
                f'atomic destination target is unavailable: {target}'
            ) from exc
        if not stat.S_ISREG(target_stat.st_mode):
            raise AtomicPersistenceError(
                f'atomic destination target is not a regular file: {target}'
            )
        return target

    parent = os.path.dirname(logical)
    try:
        real_parent = os.path.realpath(parent, strict=True)
    except (OSError, RuntimeError) as exc:
        raise AtomicPersistenceError(
            f'atomic destination parent does not resolve safely: {parent}'
        ) from exc
    if not os.path.isdir(real_parent):
        raise AtomicPersistenceError(
            f'atomic destination parent is not a directory: {real_parent}'
        )

    target = os.path.join(real_parent, name)
    if os.path.lexists(target):
        try:
            target_stat = os.stat(target)
        except OSError as exc:
            raise AtomicPersistenceError(
                f'atomic destination is unavailable: {target}'
            ) from exc
        if not stat.S_ISREG(target_stat.st_mode):
            raise AtomicPersistenceError(
                f'atomic destination is not a regular file: {target}'
            )
    return target


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_text(destination, text, encoding='utf-8', mode=0o600):
    """Write fully serialized text atomically and return the logical path.

    ``text`` must be serialized before this function is called. A unique
    temporary file is created next to the resolved real target, flushed and
    fsynced, then atomically replaces that target. The parent directory is
    fsynced after the rename. Any pre-replace failure removes the temporary
    file and leaves the previous target intact.
    """
    if not isinstance(text, str):
        raise TypeError('atomic_write_text requires serialized text')

    logical = os.path.abspath(os.fspath(destination))
    target = resolve_atomic_target(logical)
    parent = os.path.dirname(target)
    prefix = f'.{os.path.basename(target)}.'
    temp_path = None
    fd = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=parent)
        with os.fdopen(fd, 'w', encoding=encoding) as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, target)
        temp_path = None
        _fsync_directory(parent)
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return logical
