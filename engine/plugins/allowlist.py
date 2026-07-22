"""
Frozen allowlist for the strategy sandbox import policy.

This module replaces the former denylist (``BLOCKED_MODULES``) approach with a
strict *allowlist* model: only modules explicitly listed here may be imported by
strategy code running inside the sandbox.  Everything else is rejected by the
:class:`~engine.plugins.restricted_importer.RestrictedImporter` meta-path finder.

Security rationale
------------------
A denylist is an unwinnable arms-race — every new dangerous module shipped with
CPython (or a third-party package) is a fresh escape vector until someone
remembers to add it to the list.  An allowlist inverts the burden of proof: a
module is guilty (blocked) unless explicitly proven safe and listed here.

Adding a module to ``FROZEN_ALLOWED_MODULES`` requires a security review.
"""

from __future__ import annotations

import builtins as _builtins

# ── The frozen allowlist ──────────────────────────────────────────────
#
# Only the *root* package name needs to be listed; submodules
# (``collections.abc``, ``json.decoder``, …) inherit the root decision.
#
# Categories:
#   • Pure-math / numeric
#   • Data structures & algorithms (stdlib)
#   • Date / time
#   • Text & encoding helpers
#   • Serialization (safe, structured only — NOT pickle/shelve/marshal)
#   • Third-party analytics (vetted)
#   • Internal engine / SDK packages
FROZEN_ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        # ── Pure math / numeric ──
        "math",
        "cmath",
        "numbers",
        "decimal",
        "fractions",
        "statistics",
        # ── Data structures & algorithms ──
        "collections",
        "itertools",
        "functools",
        "operator",
        "heapq",
        "bisect",
        "array",
        "dataclasses",
        "enum",
        "types",
        "weakref",
        # ── Date / time ──
        "datetime",
        "time",
        "calendar",
        # ── Text / encoding ──
        "string",
        "re",
        "textwrap",
        "unicodedata",
        "pprint",
        "difflib",
        # ── Safe serialization (structured) ──
        "json",
        # ── Hashing / encoding helpers (no I/O) ──
        "hashlib",
        "hmac",
        "base64",
        "binascii",
        "uuid",
        # ── Misc safe stdlib ──
        "copy",
        "copyreg",
        "typing",
        "contextlib",
        "warnings",
        "csv",
        "constants",
        # ── Third-party analytics (vetted) ──
        "numpy",
        "polars",
        "pandas",
        "pydantic",
        # ── Networking (gated by SandboxedHttpClient at runtime) ──
        "httpx",
        # ── Logging (structured, no I/O reach) ──
        "structlog",
        # ── Internal packages ──
        "engine",
        "nexus_sdk",
        "sdk",
    }
)

# ── Explicitly-denied modules (interim denylist retained for defence-in-depth)
#
# These are *also* blocked by virtue of not being in the allowlist, but we keep
# an explicit set so that the test-suite can parametrise over known-dangerous
# names and so that a future too-permissive allowlist edit does not silently
# unblock them.
DENYLIST_MODULES: frozenset[str] = frozenset(
    [
        # Filesystem / OS
        "os",
        "_os",
        "io",
        "_io",
        "shutil",
        "pathlib",
        "pathlib2",
        "tempfile",
        "glob",
        "fnmatch",
        "linecache",
        "tokenize",
        "fileinput",
        "mimetypes",
        # Process / subprocess
        "subprocess",
        "_posixsubprocess",
        "multiprocessing",
        "concurrent",
        "concurrent.futures",
        "asyncio.subprocess",
        # Low-level system
        "ctypes",
        "_ctypes",
        "ctypes.util",
        "cffi",
        "gc",
        "signal",
        "resource",
        "fcntl",
        "termios",
        "select",
        "selectors",
        "poll",
        "epoll",
        # Introspection / code object access
        "sys",
        "_sysconfig",
        "sysconfig",
        "platform",
        # NOTE: ``importlib`` is intentionally *not* in the static denylist.
        # Importing it is benign on its own; its danger — dynamic loading via
        # ``importlib.import_module`` / ``importlib.__import__`` — is caught
        # as a *call* both statically (Layer-1 AST validator) and at runtime
        # (RestrictedImporter's call walker). A bare ``import importlib`` is
        # still blocked at runtime because ``importlib`` is absent from
        # :data:`FROZEN_ALLOWED_MODULES` (the allowlist is authoritative).
        "pkgutil",
        "inspect",
        "dis",
        "ast",
        "code",
        "codeop",
        "compileall",
        "py_compile",
        "symtable",
        "tabnanny",
        "token",
        # Threading / concurrency escape
        "threading",
        "_thread",
        "queue",
        # Context-var manipulation (CRITICAL — was the old sandbox gate)
        "contextvars",
        # Deserialization (arbitrary code execution)
        "pickle",
        "_pickle",
        "cPickle",
        "shelve",
        "marshal",
        "dbm",
        # Raw networking
        "socket",
        "_socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "webbrowser",
        "socketserver",
        # Import / runtime manipulation
        "zipimport",
        "runpy",
        "__main__",
        "site",
        "builtins",
        # Persistent hooks
        "atexit",
        "sched",
        "tracemalloc",
        "faulthandler",
        # Terminal / debugger
        "pty",
        "tty",
        "pdb",
        "bdb",
        # Misc dangerous
        "tomllib",
        "configparser",
        "netrc",
        "getpass",
        "nturl2path",
        "idlelib",
        "tkinter",
    ]
)


# ── Curated builtins ──────────────────────────────────────────────────
#
# The subset of ``builtins`` exposed to sandboxed code.  Anything that can
# reach the filesystem, execute arbitrary code, or introspect the interpreter
# is stripped.
#
# Removed (dangerous):  open, eval, exec, compile, __import__, globals,
#   locals, vars, dir (replaced by safe version below), getattr (replaced),
#   plus breakpoint, exit, quit, help, input, memoryview, delattr, setattr,
#   type (3-arg form), __build_class__.

_CURATED_BUILTIN_NAMES: tuple[str, ...] = (
    # Numeric / math
    "abs",
    "min",
    "max",
    "sum",
    "round",
    "pow",
    "divmod",
    "all",
    "any",
    "len",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "reversed",
    "sorted",
    "slice",
    "iter",
    "next",
    "format",
    "repr",
    "ascii",
    "chr",
    "ord",
    "hex",
    "oct",
    "bin",
    "hash",
    "id",
    "bool",
    "int",
    "float",
    "complex",
    "str",
    "bytes",
    "bytearray",
    "tuple",
    "list",
    "dict",
    "set",
    "frozenset",
    "object",
    "print",
    "isinstance",
    "issubclass",
    "callable",
    "hasattr",
    "classmethod",
    "staticmethod",
    "property",
    "super",
    "StopIteration",
    "StopAsyncIteration",
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BufferError",
    "EOFError",
    "Exception",
    "FloatingPointError",
    "GeneratorExit",
    "ImportError",
    "IndexError",
    "KeyError",
    "KeyboardInterrupt",
    "LookupError",
    "MemoryError",
    "NameError",
    "NotImplementedError",
    "OSError",
    "OverflowError",
    "PermissionError",
    "RecursionError",
    "ReferenceError",
    "RuntimeError",
    "SyntaxError",
    "IndentationError",
    "SystemError",
    "TimeoutError",
    "TypeError",
    "UnboundLocalError",
    "UnicodeError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "ValueError",
    "ZeroDivisionError",
    "Warning",
    "DeprecationWarning",
    "UserWarning",
    "FutureWarning",
    "PendingDeprecationWarning",
    "ResourceWarning",
    "RuntimeWarning",
    "SyntaxWarning",
    "UnicodeWarning",
    "BaseException",
    "Ellipsis",
    "NotImplemented",
    "True",
    "False",
    "None",
)

CURATED_BUILTINS: dict[str, object] = {
    name: getattr(_builtins, name) for name in _CURATED_BUILTIN_NAMES if hasattr(_builtins, name)
}


__all__ = [
    "CURATED_BUILTINS",
    "DENYLIST_MODULES",
    "FROZEN_ALLOWED_MODULES",
]
