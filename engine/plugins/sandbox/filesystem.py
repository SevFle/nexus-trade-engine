"""
Layer 4: Filesystem isolation for the plugin sandbox.

The :class:`PathValidator` enforces a **whitelist** of allowed directories.
Every filesystem access originating from sandboxed strategy code must pass
through :meth:`PathValidator.validate`, which:

1. Canonicalises the requested path with :func:`os.path.realpath`.
   ``realpath`` resolves symlinks *and* collapses ``..`` / ``.`` /
   redundant-separator components to a canonical absolute path — in one
   step.  This is the single defence that closes both classic filesystem
   escape vectors:

   * **symlink traversal** — a symlink *inside* an allowed directory that
     points *outside* (e.g. ``/allowed/secret -> /etc/passwd``) resolves
     to the outside target and is rejected by the containment check.
   * **path traversal** — ``../../../etc/passwd`` is collapsed to its
     canonical absolute form before the containment check, so a relative
     escape from the sandbox working directory is rejected.

2. Confirms the canonical path falls *within* one of the whitelisted
   roots, using an exact-match or ``<root><sep>`` prefix match so that a
   directory named ``/data/allowed_evil`` is **not** mistaken for a
   child of ``/data/allowed``.

3. Optionally gates write access (the sandbox working directory may be
   writable while declared read-only artifacts are not).

The validator is intentionally framework-agnostic: it takes a list of
directory paths and exposes :meth:`make_open_hook` /
:meth:`make_path_hook` factories so the host (the
:class:`~engine.plugins.sandbox.StrategySandbox`) can install
permission-checking wrappers around ``builtins.open``, ``io.open``, and
``os`` path-taking functions.  This module is host-side code — sandboxed
strategy code cannot import ``engine.*`` (the import allowlist denies
it), so the ``os`` references below are unreachable by attacker code.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

__all__ = ["PathValidator"]


class PathValidator:
    """Whitelist-based filesystem path validator for the plugin sandbox.

    Parameters
    ----------
    allowed_dirs:
        One or more directory paths that constitute the whitelist.  Each
        is canonicalised with :func:`os.path.realpath` at construction
        time, so symlinks in the *whitelist itself* are resolved once
        and never re-evaluated per request (closing a whitelist-swap
        race).  A single string/``PathLike`` is accepted for
        convenience.
    allow_writable:
        If ``False`` (the default) :meth:`validate` rejects any request
        flagged as a write.  Set to ``True`` to permit writes within the
        whitelisted roots (e.g. for the sandbox scratch directory).

    Examples
    --------
    >>> validator = PathValidator(["/data/allowed"])
    >>> validator.validate("/data/allowed/prices.csv")  # ok
    '/data/allowed/prices.csv'
    >>> validator.validate("/etc/passwd")               # blocked
    Traceback (most recent call last):
        ...
    PermissionError: File access to '/etc/passwd' is not allowed in the sandbox: ...
    """

    def __init__(
        self,
        allowed_dirs: Iterable[str | os.PathLike[str]] | str | os.PathLike[str],
        *,
        allow_writable: bool = False,
    ) -> None:
        if isinstance(allowed_dirs, (str, os.PathLike)):
            allowed_dirs = [allowed_dirs]

        # Canonicalise each whitelisted root once.  ``realpath`` resolves
        # symlinks *in the whitelist* so a root configured as a symlink
        # (e.g. ``/var/data`` -> ``/srv/nexus/data``) is matched against
        # its real target.  Roots are deduplicated and sorted for
        # deterministic behaviour; the empty whitelist is permitted and
        # denies everything.
        seen: set[str] = set()
        canonical: list[str] = []
        for raw in allowed_dirs:
            root = os.path.realpath(os.fspath(raw))
            if root not in seen:
                seen.add(root)
                canonical.append(root)
        canonical.sort()

        self._allowed: tuple[str, ...] = tuple(canonical)
        self._allow_writable: bool = allow_writable
        # The hooks produced by this validator are only active while this
        # flag is truthy.  The host sandbox installs the hooks once and
        # toggles the flag around strategy evaluation so that non-sandbox
        # code (e.g. engine internals sharing the process) is unaffected.
        self._active: bool = True

    # ── properties / state ───────────────────────────────────────────

    @property
    def allowed_dirs(self) -> list[str]:
        """Canonicalised whitelisted roots (defensive copy)."""
        return list(self._allowed)

    @property
    def allow_writable(self) -> bool:
        return self._allow_writable

    @property
    def active(self) -> bool:
        """Whether produced hooks should enforce validation."""
        return self._active

    def activate(self) -> None:
        """Enable enforcement for hooks created from this validator."""
        self._active = True

    def deactivate(self) -> None:
        """Disable enforcement for hooks created from this validator.

        Hooks installed via :meth:`make_open_hook` /
        :meth:`make_path_hook` become pass-throughs while inactive.
        """
        self._active = False

    # ── public validation API ────────────────────────────────────────

    def is_allowed(self, path: Any, *, write: bool = False) -> bool:
        """Return ``True`` if *path* may be accessed in the sandbox.

        Never raises — non-path inputs (ints, ``None``, objects without
        ``__fspath__``) simply return ``False``.
        """
        try:
            resolved = self._resolve(path)
        except (TypeError, ValueError):
            return False
        if write and not self._allow_writable:
            return False
        return self._within(resolved)

    def validate(self, path: Any, *, write: bool = False) -> str:
        """Validate *path* against the whitelist.

        Returns the canonicalised path on success.  Raises
        :class:`PermissionError` with a clear, human-readable message on
        any violation: non-path input, file-descriptor access, write to
        a read-only sandbox, or a path that resolves outside the
        whitelist.
        """
        # File descriptors (ints) bypass path checks entirely, so reject
        # them unconditionally — a sandboxed strategy must never reach a
        # raw fd.
        if isinstance(path, bool):
            # ``bool`` is a subclass of ``int``; reject explicitly so
            # ``validate(True)`` is not silently treated as fd 1.
            raise PermissionError(
                "Boolean is not a valid sandboxed filesystem path."
            )
        if isinstance(path, int):
            raise PermissionError(
                "File descriptor access is not allowed in the sandbox; "
                "filesystem access requires a whitelisted path."
            )
        if path is None:
            raise PermissionError("None is not a valid sandboxed filesystem path.")

        try:
            requested = os.fspath(path)
        except TypeError as exc:
            raise PermissionError(
                f"Object of type {type(path).__name__!r} is not a valid "
                f"sandboxed filesystem path."
            ) from exc

        # ``bytes`` paths are decoded to ``str`` so the canonical form and
        # all error messages are consistently textual.  ``surrogateescape``
        # mirrors the OS filesystem encoding so arbitrary bytes round-trip.
        if isinstance(requested, bytes):
            requested = os.fsdecode(requested)

        resolved = os.path.realpath(requested)

        if write and not self._allow_writable:
            raise PermissionError(
                f"Write access to {requested!r} is not allowed in the sandbox."
            )
        if not self._within(resolved):
            raise PermissionError(
                f"File access to {requested!r} is not allowed in the sandbox: "
                f"canonical path {resolved!r} is outside the whitelisted "
                f"directories."
            )
        return resolved

    # ── hook factories ───────────────────────────────────────────────

    def make_open_hook(
        self,
        original_open: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Wrap a ``builtins.open`` / ``io.open`` callable.

        The returned hook has the same signature as :func:`open`.  When
        the validator is :attr:`active`, it validates ``file`` against
        the whitelist (treating write/append/exclusive/update modes as
        writes) before delegating to *original_open*.  When inactive the
        call passes through unchanged.
        """
        validator = self

        def open_hook(
            file: Any,
            mode: str = "r",
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if validator._active:
                is_write = any(token in mode for token in ("w", "a", "x", "+"))
                validator.validate(file, write=is_write)
            return original_open(file, mode, *args, **kwargs)

        return open_hook

    def make_path_hook(
        self,
        original_func: Callable[..., Any],
        *,
        write: bool = False,
    ) -> Callable[..., Any]:
        """Wrap an ``os`` function that takes a path as its first argument.

        Suitable for ``os.stat``, ``os.listdir``, ``os.remove``,
        ``os.unlink``, ``os.mkdir``, ``os.rmdir``, ``os.scandir``, etc.
        Set ``write=True`` for mutating calls (``remove``, ``mkdir`` …)
        so the write policy is enforced.

        When the validator is inactive the wrapped call passes through.
        """
        validator = self

        def path_hook(path: Any, *args: Any, **kwargs: Any) -> Any:
            if validator._active:
                validator.validate(path, write=write)
            return original_func(path, *args, **kwargs)

        return path_hook

    def make_two_path_hook(
        self,
        original_func: Callable[..., Any],
        *,
        write: bool = False,
    ) -> Callable[..., Any]:
        """Wrap an ``os`` function taking two path arguments (e.g. ``rename``/``replace``).

        Both ``src`` and ``dst`` are validated.  When the validator is
        inactive the wrapped call passes through.
        """
        validator = self

        def two_path_hook(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            if validator._active:
                validator.validate(src, write=write)
                validator.validate(dst, write=write)
            return original_func(src, dst, *args, **kwargs)

        return two_path_hook

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _resolve(path: Any) -> str:
        """Canonicalise *path* via ``realpath`` (symlinks + ``..`` resolved)."""
        if isinstance(path, bool):
            raise TypeError("bool is not a path")
        if isinstance(path, int):
            raise TypeError("int is not a path")
        requested = os.fspath(path)
        if isinstance(requested, bytes):
            requested = os.fsdecode(requested)
        return os.path.realpath(requested)

    def _within(self, resolved: str) -> bool:
        """Return ``True`` if *resolved* falls inside a whitelisted root.

        Uses an exact match or ``<root><sep>`` prefix match so that a
        sibling directory sharing a textual prefix (``/data/allowed``
        vs ``/data/allowed_evil``) is **not** mistaken for containment.
        """
        sep = os.sep
        for root in self._allowed:
            if resolved == root:
                return True
            if resolved.startswith(root + sep):
                return True
        return False
