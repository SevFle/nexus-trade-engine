from __future__ import annotations

from engine.plugins.restricted_importer import BLOCKED_MODULES

STDLIB_SAFE: frozenset[str] = frozenset(
    {
        "json", "math", "datetime", "decimal", "fractions",
        "collections", "functools", "itertools", "operator",
        "re", "string", "textwrap", "unicodedata",
        "enum", "dataclasses", "typing", "types",
        "statistics", "random", "hashlib", "hmac",
        "base64", "html", "copy", "pprint",
        "logging", "warnings", "contextlib",
        "abc", "numbers", "cmath",
    }
)

STDLIB_IO: frozenset[str] = frozenset(
    {"os", "sys", "io", "_io", "pathlib", "shutil", "tempfile", "fileinput", "glob"}
)

STDLIB_NETWORK: frozenset[str] = frozenset(
    {"socket", "_socket", "http", "urllib", "ftplib", "smtplib", "xmlrpc", "webbrowser"}
)

STDLIB_DANGEROUS: frozenset[str] = frozenset(
    {
        "subprocess", "ctypes", "_ctypes", "multiprocessing",
        "signal", "threading", "_thread", "concurrent",
        "gc", "inspect", "code", "codeop", "ast", "dis",
        "importlib", "pkgutil", "zipimport", "runpy",
        "pickle", "shelve", "marshal",
        "atexit", "sched", "pty", "tty", "pdb", "bdb", "site",
    }
)

ALL_BLOCKED: frozenset[str] = STDLIB_IO | STDLIB_NETWORK | STDLIB_DANGEROUS


def get_default_allowed() -> set[str]:
    return set(STDLIB_SAFE)


def get_default_blocked() -> set[str]:
    return set(BLOCKED_MODULES)
