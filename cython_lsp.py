#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cython-lsp - a small, dependency-free language server for Cython.

It speaks LSP over stdio and provides:

  * completion for Cython/Python keywords, builtins, imports and local symbols
  * diagnostics produced by the real Cython compiler (``cython -3``)
  * hover, go-to-definition and document symbols

The server only needs Python 3.9+ and (for diagnostics) the ``Cython``
package.  Nothing outside the standard library is imported.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import builtins
import json
import keyword
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import quote, unquote

SERVER_NAME = "cython-lsp"
SERVER_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# LSP enum values
# ---------------------------------------------------------------------------

COMPLETION_TEXT = 1
COMPLETION_METHOD = 2
COMPLETION_FUNCTION = 3
COMPLETION_FIELD = 5
COMPLETION_VARIABLE = 6
COMPLETION_CLASS = 7
COMPLETION_MODULE = 9
COMPLETION_KEYWORD = 14
COMPLETION_STRUCT = 22
COMPLETION_TYPE = 25

SYMBOL_MODULE = 2
SYMBOL_CLASS = 5
SYMBOL_METHOD = 6
SYMBOL_FUNCTION = 12
SYMBOL_VARIABLE = 13
SYMBOL_STRUCT = 23
SYMBOL_TYPE = 26

# ---------------------------------------------------------------------------
# Cython knowledge
# ---------------------------------------------------------------------------

CYTHON_KEYWORD_DOCS = {
    "cdef": "Declare a C variable, function, class, struct, union, enum or an external C/C++ block.",
    "cpdef": "Declare a function that is callable from both C and Python.",
    "cimport": "Import declarations from a Cython ``.pxd`` file or from C/C++ headers.",
    "ctypedef": "Create a C type alias, or declare a fused type.",
    "nogil": "Mark a function or a ``with nogil:`` block as not needing the GIL.",
    "gil": "Acquire the GIL explicitly, e.g. ``with gil:``.",
    "prange": "Parallel ``range`` provided by ``cython.parallel``.",
    "fused": "Declare a fused type: ``ctypedef fused number:``.",
    "extern": "Declare an external block: ``cdef extern from \"header.h\":``.",
    "public": "Expose a C attribute to Python: ``cdef public int value``.",
    "readonly": "Expose a C attribute as read-only to Python.",
    "api": "Expose a C function as part of the public Cython C API.",
    "inline": "Request that the C compiler inlines a function.",
    "packed": "Declare a packed struct without padding.",
    "include": "Textually include a ``.pxi`` file.",
    "boundscheck": "Control bounds checking (``cython.boundscheck``).",
    "wraparound": "Control negative-index wraparound (``cython.wraparound``).",
    "nonecheck": "Control ``None`` checking (``cython.nonecheck``).",
    "cdivision": "Control C division semantics (``cython.cdivision``).",
    "overflowcheck": "Control integer overflow checking.",
    "initializedcheck": "Control memoryview initialization checking.",
    "profile": "Enable profiling for a function.",
    "linetrace": "Enable line tracing for coverage.",
    "embedsignature": "Embed the Python signature in docstrings.",
    "binding": "Control function binding for Python subclasses.",
    "language_level": "Set the Python language level (``# cython: language_level=3``).",
}

PYTHON_KEYWORD_DOCS = {
    "def": "Define a function.",
    "class": "Define a class.",
    "return": "Return a value from a function.",
    "yield": "Yield a value from a generator.",
    "import": "Import a Python module.",
    "from": "Import specific names from a module.",
    "as": "Bind an imported name or a context manager to a local name.",
    "with": "Use a context manager.",
    "for": "Iterate over an iterable.",
    "while": "Repeat while a condition is true.",
    "if": "Conditional execution.",
    "elif": "Conditional branch of an ``if`` statement.",
    "else": "Alternative branch of a conditional statement.",
    "try": "Run a block that may raise an exception.",
    "except": "Handle an exception.",
    "finally": "Run cleanup code unconditionally.",
    "raise": "Raise an exception.",
    "lambda": "Create an anonymous function.",
    "global": "Declare a name as module-global.",
    "nonlocal": "Declare a name as belonging to an enclosing function.",
    "assert": "Assert a condition.",
    "del": "Delete a name or an item.",
    "pass": "Do nothing.",
    "break": "Exit the innermost loop.",
    "continue": "Continue with the next loop iteration.",
    "async": "Declare an asynchronous function or context manager.",
    "await": "Await an awaitable object.",
    "match": "Structural pattern matching statement.",
    "case": "A branch of a ``match`` statement.",
}

# Names that are useful in Cython code but are not Python builtins.
CYTHON_EXTRA_DOCS = {
    "NULL": "The C ``NULL`` pointer.",
    "bint": "Cython's C boolean type.",
    "Py_ssize_t": "Signed pointer-sized integer type.",
    "size_t": "Unsigned pointer-sized integer type.",
    "prange": "Parallel range from ``cython.parallel``.",
    "parallel": "Cython's ``cython.parallel`` module.",
    "cython": "The ``cython`` runtime module.",
    "libc": "C standard library declarations (``cimport libc``).",
    "libcpp": "C++ standard library declarations (``cimport libcpp``).",
    "numpy": "NumPy C API declarations (``cimport numpy``).",
    "np": "Conventional alias for ``cimport numpy as np``.",
    "pyximport": "Import ``.pyx`` modules directly from Python.",
    "Cython": "The Cython compiler package.",
    "uint8_t": "Standard integer type from ``libc.stdint``.",
    "int8_t": "Standard integer type from ``libc.stdint``.",
    "uint16_t": "Standard integer type from ``libc.stdint``.",
    "int16_t": "Standard integer type from ``libc.stdint``.",
    "uint32_t": "Standard integer type from ``libc.stdint``.",
    "int32_t": "Standard integer type from ``libc.stdint``.",
    "uint64_t": "Standard integer type from ``libc.stdint``.",
    "int64_t": "Standard integer type from ``libc.stdint``.",
}

# Members of the compile-time ``cython`` pseudo-module, offered after
# ``cython.``.  They cannot be discovered from a .pxd because this namespace
# is provided by the Cython compiler itself.
CYTHON_MODULE_MEMBERS = {
    "sizeof": ("function", "Compile-time C ``sizeof`` of a type or variable."),
    "cast": ("function", "Cast a value to a C type: ``cython.cast(type, value)``."),
    "address": ("function", "Take the address of a C value."),
    "byref": ("function", "Pass a value as a C++ reference."),
    "pointer": ("function", "Cython pointer helper."),
    "deref": ("function", "Dereference a C pointer."),
    "declare": ("function", "Declare C variables/types using Python-like syntax."),
    "NULL": ("variable", "The C ``NULL`` pointer constant."),
    "char": ("type", "C ``char`` type."),
    "short": ("type", "C ``short`` type."),
    "int": ("type", "C ``int`` type."),
    "long": ("type", "C ``long`` type."),
    "float": ("type", "C ``float`` type."),
    "double": ("type", "C ``double`` type."),
    "complex": ("type", "C ``complex`` type."),
    "bint": ("type", "Cython's C boolean type."),
    "object": ("type", "Python object type."),
    "str": ("type", "Python string type."),
    "bytes": ("type", "Python bytes type."),
    "void": ("type", "C ``void`` type."),
    "parallel": ("module", "``cython.parallel`` module."),
    "view": ("module", "``cython.view`` module."),
    "operator": ("module", "``cython.operator`` module."),
    "boundscheck": ("variable", "Compile-time bounds checking directive."),
    "wraparound": ("variable", "Compile-time wraparound directive."),
    "nonecheck": ("variable", "Compile-time ``None`` checking directive."),
    "cdivision": ("variable", "Compile-time C division directive."),
    "overflowcheck": ("variable", "Compile-time overflow checking directive."),
    "initializedcheck": ("variable", "Compile-time memoryview initialization directive."),
    "profile": ("variable", "Compile-time profiling directive."),
    "linetrace": ("variable", "Compile-time line tracing directive."),
    "embedsignature": ("variable", "Compile-time signature embedding directive."),
    "binding": ("variable", "Compile-time function binding directive."),
    "language_level": ("variable", "Compile-time Python language level directive."),
}

COMMON_MODULES = [
    "os",
    "sys",
    "math",
    "time",
    "typing",
    "collections",
    "itertools",
    "functools",
    "pathlib",
    "numpy",
]

COMMON_CIMPORTS = [
    "numpy",
    "libc.stdio",
    "libc.stdlib",
    "libc.string",
    "libc.math",
    "libc.time",
    "libc.stdint",
    "libc.stddef",
    "libcpp.vector",
    "libcpp.string",
    "libcpp.map",
    "libcpp.set",
    "libcpp.unordered_map",
    "libcpp.algorithm",
    "libcpp.memory",
    "cython.parallel",
    "cython.view",
    "cython.operator",
    "cython.boundscheck",
    "cython.wraparound",
    "cython.nonecheck",
    "cython.cdivision",
    "cython.embedsignature",
    "cython.binding",
]

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


class Logger:
    """Append-only log file; never writes to stdout/stderr (that is the LSP channel)."""

    def __init__(self) -> None:
        path = os.environ.get("CYTHON_LSP_LOG")
        if path is None:
            cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
            path = os.path.join(cache, "cython-lsp", "server.log")
        self.path = path or None
        self._lock = threading.Lock()

    def _write(self, level: str, message: str) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > 1_000_000:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(
                        "%s %-5s %s\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), level, message)
                    )
        except OSError:
            pass

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


def uri_to_path(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        path = unquote(uri[7:])
        if path.startswith("localhost/"):
            path = path[len("localhost") :]
        # file:///C:/... on Windows
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path
    return uri


def path_to_uri(path: str) -> str:
    return "file://" + quote(os.path.abspath(path))


def position_to_offset(text: str, position: dict) -> int:
    line_number = int(position.get("line", 0) or 0)
    character = int(position.get("character", 0) or 0)
    lines = text.splitlines(keepends=True)
    if not lines:
        return 0
    if line_number < 0:
        line_number = 0
    if line_number >= len(lines):
        return len(text)
    start = sum(len(line) for line in lines[:line_number])
    line = lines[line_number]
    units = 0
    index = 0
    for char in line:
        if units >= character:
            break
        units += 2 if ord(char) > 0xFFFF else 1
        index += 1
    return start + index


def offset_to_position(text: str, offset: int) -> dict:
    offset = max(0, min(offset, len(text)))
    line_number = text.count("\n", 0, offset)
    line_start = text.rfind("\n", 0, offset) + 1
    character = len(text[line_start:offset].encode("utf-16-le")) // 2
    return {"line": line_number, "character": character}


def apply_change(text: str, change: dict) -> str:
    if "range" not in change:
        return change.get("text", "")
    start = position_to_offset(text, change["range"]["start"])
    end = position_to_offset(text, change["range"]["end"])
    if end < start:
        start, end = end, start
    return text[:start] + change.get("text", "") + text[end:]


# ---------------------------------------------------------------------------
# Symbol extraction (deliberately lightweight: this is not a Cython parser)
# ---------------------------------------------------------------------------

CLASS_RE = re.compile(r"^\s*(?:cdef\s+class|class)\s+(?P<name>[A-Za-z_]\w*)")
STRUCT_RE = re.compile(
    r"^\s*(?:cdef\s+)?(?P<kw>struct|union|enum)\s+(?P<name>[A-Za-z_]\w*)\s*(?::|$)"
)
FUNC_RE = re.compile(
    r"^\s*(?:async\s+)?(?P<kw>cpdef|cdef|def)\s+"
    r"(?P<prefix>[^():]*?)(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)"
)
CTYPEDEF_RE = re.compile(
    r"^\s*ctypedef\s+(?P<sig>.+?)\s+(?P<name>[A-Za-z_]\w*)\s*$"
)
CDEF_VAR_RE = re.compile(
    r"^\s*cdef\s+(?P<sig>[^()=]+?)\s+(?P<name>[A-Za-z_]\w*)\s*(?:=.*)?$"
)
ASSIGN_RE = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(?P<module>\.*[\w\.]*)\s+c?import\s+(?P<names>.+)$"
)
IMPORT_RE = re.compile(r"^\s*c?import\s+(?P<names>.+)$")

KIND_PRIORITY = {
    "module": 0,
    "variable": 1,
    "type": 3,
    "method": 4,
    "function": 5,
    "struct": 6,
    "class": 7,
}

KIND_TO_COMPLETION = {
    "class": COMPLETION_CLASS,
    "struct": COMPLETION_STRUCT,
    "type": COMPLETION_TYPE,
    "function": COMPLETION_FUNCTION,
    "method": COMPLETION_METHOD,
    "variable": COMPLETION_VARIABLE,
    "module": COMPLETION_MODULE,
}

KIND_TO_SYMBOL = {
    "class": SYMBOL_CLASS,
    "struct": SYMBOL_STRUCT,
    "type": SYMBOL_TYPE,
    "function": SYMBOL_FUNCTION,
    "method": SYMBOL_METHOD,
    "variable": SYMBOL_VARIABLE,
    "module": SYMBOL_MODULE,
}


def _strip_comment(line: str) -> str:
    index = line.find("#")
    if index >= 0:
        return line[:index].rstrip()
    return line.rstrip()


def collect_symbols(text: str) -> list:
    """Return a list of symbol dicts found in *text* (first definition wins)."""

    found = {}
    for line_number, raw_line in enumerate(text.splitlines()):
        line = raw_line.rstrip("\r")
        code = _strip_comment(line)
        stripped = code.strip()
        if not stripped:
            continue
        indent = len(code) - len(code.lstrip(" \t"))

        match = CLASS_RE.match(code)
        if match:
            _remember(
                found,
                match.group("name"),
                "class",
                line_number,
                match.start("name"),
                stripped,
                indent,
            )
            continue

        match = STRUCT_RE.match(code)
        if match:
            _remember(
                found,
                match.group("name"),
                "struct",
                line_number,
                match.start("name"),
                stripped,
                indent,
            )
            continue

        match = FUNC_RE.match(code)
        if match and match.group("name") not in {
            "extern",
            "public",
            "api",
            "inline",
            "readonly",
            "packed",
            "struct",
            "union",
            "enum",
            "class",
        }:
            kind = "method" if indent > 0 else "function"
            _remember(
                found,
                match.group("name"),
                kind,
                line_number,
                match.start("name"),
                stripped,
                indent,
            )
            continue

        match = CTYPEDEF_RE.match(code)
        if match:
            _remember(
                found,
                match.group("name"),
                "type",
                line_number,
                match.start("name"),
                stripped,
                indent,
            )
            continue

        match = CDEF_VAR_RE.match(code)
        if match:
            _remember(
                found,
                match.group("name"),
                "variable",
                line_number,
                match.start("name"),
                stripped,
                indent,
            )
            continue

        if indent == 0:
            match = ASSIGN_RE.match(code)
            if match:
                _remember(
                    found,
                    match.group("name"),
                    "variable",
                    line_number,
                    match.start("name"),
                    stripped,
                    indent,
                )

    return sorted(found.values(), key=lambda item: (item["line"], item["character"]))


def _remember(
    found: dict,
    name: str,
    kind: str,
    line: int,
    character: int,
    signature: str,
    indent: int,
) -> None:
    if not name:
        return
    old = found.get(name)
    if old is not None and KIND_PRIORITY.get(old["kind"], 0) >= KIND_PRIORITY.get(
        kind, 0
    ):
        return
    found[name] = {
        "name": name,
        "kind": kind,
        "line": line,
        "character": character,
        "signature": signature[:180],
        "indent": indent,
    }


def collect_imports(text: str) -> list:
    """Return (name, detail, kind) tuples for names introduced by import/cimport."""

    imports = {}
    for raw_line in text.splitlines():
        code = _strip_comment(raw_line).strip()
        if not code:
            continue

        match = FROM_IMPORT_RE.match(code)
        if match:
            module = match.group("module")
            for part in match.group("names").split(","):
                part = part.strip()
                if not part or part.startswith("(") or part.startswith(")"):
                    continue
                name, alias = _split_alias(part)
                if name:
                    imports.setdefault(
                        alias or name,
                        (
                            alias or name,
                            "from %s import %s" % (module, name),
                            "module",
                        ),
                    )
            continue

        match = IMPORT_RE.match(code)
        if match:
            for part in match.group("names").split(","):
                part = part.strip()
                if not part:
                    continue
                name, alias = _split_alias(part)
                if name:
                    imports.setdefault(
                        alias or name,
                        (
                            alias or name,
                            "module %s" % name,
                            "module",
                        ),
                    )

    return list(imports.values())


def _split_alias(part: str) -> tuple:
    pieces = re.split(r"\s+as\s+", part)
    name = pieces[0].strip()
    alias = pieces[1].strip() if len(pieces) > 1 else ""
    if not re.match(r"^[A-Za-z_]\w*$", name):
        return "", ""
    if alias and not re.match(r"^[A-Za-z_]\w*$", alias):
        alias = ""
    return name, alias


def word_at(text: str, offset: int) -> tuple:
    """Return (line_text, start_index, end_index, word)."""

    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    index = max(0, min(offset - line_start, len(line)))

    if index < len(line) and not (line[index].isalnum() or line[index] == "_"):
        if index > 0 and (line[index - 1].isalnum() or line[index - 1] == "_"):
            index -= 1

    start = index
    while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
        start -= 1
    end = index
    while end < len(line) and (line[end].isalnum() or line[end] == "_"):
        end += 1
    return line, start, end, line[start:end]


def _attribute_at(text: str, offset: int) -> tuple:
    line_start = text.rfind("\n", 0, offset) + 1
    before_cursor = text[line_start:offset]
    match = re.search(
        r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)?$", before_cursor
    )
    if match and match.group(2):
        return match.group(1), match.group(2)
    return "", ""


def builtin_documentation(name: str) -> str:
    try:
        value = getattr(builtins, name)
    except AttributeError:
        return ""
    doc = (getattr(value, "__doc__", "") or "").strip()
    if not doc:
        return "**%s** - Python builtin" % name
    lines = doc.splitlines()
    if len(lines) > 14:
        lines = lines[:14] + ["..."]
    body = "\n".join(lines).strip()
    return "**%s** - Python builtin\n\n%s" % (name, body)


def completion_item(
    label: str,
    kind: int,
    detail: str = "",
    documentation: str = "",
    sort_text: str = "",
    text_edit: dict = None,
) -> dict:
    item = {"label": label, "kind": kind}
    if detail:
        item["detail"] = detail
    if documentation:
        item["documentation"] = {"kind": "markdown", "value": documentation}
    if sort_text:
        item["sortText"] = sort_text
    if text_edit:
        item["textEdit"] = text_edit
    return item


def _range_between(text: str, start: int, end: int) -> dict:
    return {
        "start": offset_to_position(text, start),
        "end": offset_to_position(text, end),
    }


_CYTHON_INCLUDES = {"resolved": False, "path": ""}
_PXD_EXPORTS = {}

_PXD_DECL_RE = re.compile(
    r"^(?P<ret>(?:[A-Za-z_]\w*\s+|\*+\s*)+)(?P<name>[A-Za-z_]\w*)\s*\("
)
_PXD_BARE_FUNC_RE = re.compile(
    r"^(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)"
    r"\s*(?:nogil|noexcept|except[^:]*)?\s*:?\s*$"
)
_PXD_VAR_RE = re.compile(
    r"^(?:[A-Za-z_]\w*(?:\s+|\s*\*+\s*))+([A-Za-z_]\w*)\s*$"
)
_PXD_ENUM_RE = re.compile(r"^enum\s*:\s*(?P<name>[A-Za-z_]\w*)")
_PXD_TYPEDEF_RE = re.compile(
    r"^ctypedef\s+(?:struct|union|enum)\s+(?P<name>[A-Za-z_]\w*)"
)
_PXD_ALIAS_RE = re.compile(
    r'^ctypedef\s+.+?\s+(?P<name>[A-Za-z_]\w*)(?:\s+".*")?\s*$'
)
_PXD_SKIP = {
    "cdef", "cpdef", "def", "from", "import", "cimport", "return", "if",
    "elif", "else", "for", "while", "with", "try", "except", "finally",
    "pass", "raise", "enum", "extern", "public", "readonly", "api",
    "inline", "nogil", "gil", "ctypedef", "struct", "union", "class",
    "include", "IF", "ELIF", "ELSE", "DEF",
}
_PXD_TYPE_WORDS = {
    "void", "int", "char", "long", "short", "unsigned", "signed", "float",
    "double", "const", "volatile", "size_t", "ssize_t", "Py_ssize_t",
    "bint", "object",
}


def _cython_includes_dir() -> str:
    """Locate Cython's ``Includes`` directory (contains libc/libcpp .pxd)."""

    if _CYTHON_INCLUDES["resolved"]:
        return _CYTHON_INCLUDES["path"]

    candidates = []
    try:
        from importlib.util import find_spec

        spec = find_spec("Cython")
        if spec and spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                candidates.append(os.path.join(location, "Includes"))
    except Exception:
        pass
    for entry in sys.path:
        if entry:
            candidates.append(os.path.join(entry, "Cython", "Includes"))

    found = ""
    for candidate in candidates:
        if os.path.isdir(candidate):
            found = candidate
            break
    _CYTHON_INCLUDES["resolved"] = True
    _CYTHON_INCLUDES["path"] = found
    return found


def _add_pxd_export(
    exports: list,
    seen: set,
    name: str,
    kind: str,
    signature: str,
    doc: str = "",
    line: int = -1,
    character: int = 0,
) -> None:
    if not name or name in seen or name in _PXD_SKIP or name in _PXD_TYPE_WORDS:
        return
    seen.add(name)
    if signature.endswith((",", "(")):
        signature += " ..."
    exports.append(
        {
            "name": name,
            "kind": kind,
            "signature": signature[:300],
            "doc": (doc or "").strip(),
            "line": line,
            "character": character,
        }
    )


def _parse_pxd_exports(path: str) -> list:
    exports = []
    seen = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return exports

    pending_comments = []
    class_indent = None
    for index, raw_line in enumerate(lines):
        if not raw_line.strip():
            pending_comments = []
            continue
        if raw_line.lstrip().startswith("#"):
            pending_comments.append(raw_line.lstrip().lstrip("#").strip())
            continue

        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if class_indent is not None:
            if indent <= class_indent:
                class_indent = None
            else:
                pending_comments = []
                continue
        inline_comment = ""
        if "#" in raw_line:
            inline_comment = raw_line.split("#", 1)[1].strip()
        doc = "\n".join(pending_comments).strip()
        if not doc:
            doc = inline_comment
        pending_comments = []

        declaration = stripped
        first = declaration.split(None, 1)[0]
        if first in ("cdef", "cpdef"):
            rest = declaration[len(first) :].strip()
            if rest.startswith("extern"):
                continue
            class_match = re.match(r"class\s+([A-Za-z_]\w*)", rest)
            if class_match:
                name = class_match.group(1)
                _add_pxd_export(
                    exports,
                    seen,
                    name,
                    "class",
                    stripped,
                    doc,
                    index,
                    line.find(name),
                )
                class_indent = indent
                continue
            struct_match = re.match(
                r"(?:struct|union|enum)\s+([A-Za-z_]\w*)", rest
            )
            if struct_match:
                name = struct_match.group(1)
                _add_pxd_export(
                    exports,
                    seen,
                    name,
                    "type",
                    stripped,
                    doc,
                    index,
                    line.find(name),
                )
                continue
            rest = re.sub(
                r"^(?:public|readonly|api|inline|packed|extern)\s+",
                "",
                rest,
            )
            if not rest:
                continue
            declaration = rest
            first = declaration.split(None, 1)[0]

        if first == "ctypedef":
            match = _PXD_TYPEDEF_RE.match(stripped) or _PXD_ALIAS_RE.match(stripped)
            if match:
                name = match.group("name")
                _add_pxd_export(
                    exports,
                    seen,
                    name,
                    "type",
                    stripped,
                    doc,
                    index,
                    line.find(name),
                )
            continue
        if first in _PXD_SKIP:
            continue

        match = _PXD_ENUM_RE.match(declaration)
        if match:
            name = match.group("name")
            _add_pxd_export(
                exports, seen, name, "variable", stripped, doc, index, line.find(name)
            )
            continue

        match = _PXD_DECL_RE.match(declaration)
        if match:
            name = match.group("name")
            _add_pxd_export(
                exports, seen, name, "function", stripped, doc, index, line.find(name)
            )
            continue

        # Cython declarations may omit the return type, e.g.
        # `cpdef sieve(int n)` in a .pxd file.
        match = _PXD_BARE_FUNC_RE.match(declaration)
        if match:
            name = match.group("name")
            _add_pxd_export(
                exports, seen, name, "function", stripped, doc, index, line.find(name)
            )
            continue

        no_initializer = declaration.split("=", 1)[0].rstrip()
        match = _PXD_VAR_RE.match(no_initializer)
        if match and indent == 0:
            name = match.group(1)
            _add_pxd_export(
                exports, seen, name, "variable", stripped, doc, index, line.find(name)
            )
    return exports


_PXD_FILE_CACHE = {}
_STUB_FILE_CACHE = {}
_LOCAL_MODULE_CACHE = {}


def _parse_pxd_cached(path: str) -> list:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    cached = _PXD_FILE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    exports = _parse_pxd_exports(path)
    _PXD_FILE_CACHE[path] = (mtime, exports)
    return exports


def _search_roots(document_path: str, root_path: str) -> list:
    roots = []

    def add(path: str) -> None:
        if not path:
            return
        absolute = os.path.abspath(path)
        if os.path.isdir(absolute) and absolute not in roots:
            roots.append(absolute)

    add(root_path)
    if document_path:
        add(os.path.dirname(os.path.abspath(document_path)))
    add(os.getcwd())
    return roots


def _resolve_module_file(
    module: str, document_path: str, root_path: str, extensions: list
) -> str:
    module = (module or "").strip()
    if not module:
        return ""

    roots = _search_roots(document_path, root_path)
    if module.startswith("."):
        base = (
            os.path.dirname(os.path.abspath(document_path))
            if document_path
            else os.getcwd()
        )
        roots = [base] + [root for root in roots if root != base]
        parts = [part for part in module.lstrip(".").split(".") if part]
    else:
        parts = [part for part in module.split(".") if part]

    for root in roots:
        if not parts:
            for extension in extensions:
                candidate = os.path.join(root, "__init__" + extension)
                if os.path.isfile(candidate):
                    return candidate
            continue
        for extension in extensions:
            candidate = os.path.join(root, *parts) + extension
            if os.path.isfile(candidate):
                return candidate
            candidate = os.path.join(root, *parts, "__init__" + extension)
            if os.path.isfile(candidate):
                return candidate
    return ""


def _resolve_python_module(module: str, document_path: str, root_path: str) -> str:
    path = _resolve_module_file(module, document_path, root_path, [".pyi"])
    if path:
        return path
    path = _resolve_module_file(module, document_path, root_path, [".py"])
    if path:
        return path

    if module.startswith(".") or "." in module:
        return ""
    try:
        import importlib.util

        spec = importlib.util.find_spec(module)
    except Exception:
        return ""
    if spec is None:
        return ""
    origin = spec.origin or ""
    if origin.endswith(".py"):
        stub = origin[:-3] + ".pyi"
        return stub if os.path.isfile(stub) else origin
    if origin.endswith(".pyi"):
        return origin
    return ""


def _local_modules(document_path: str, root_path: str, extensions: list) -> list:
    roots = _search_roots(document_path, root_path)
    key = (tuple(roots), tuple(extensions))
    now = time.time()
    cached = _LOCAL_MODULE_CACHE.get(key)
    if cached and now - cached[0] < 3.0:
        return cached[1]

    skipped = {
        "__pycache__", "build", "dist", ".git", ".hg", ".svn",
        ".venv", "venv", "env", "node_modules", ".mypy_cache", ".tox",
    }
    found = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            relative_dir = os.path.relpath(dirpath, root)
            depth = 0 if relative_dir == "." else relative_dir.count(os.sep) + 1
            if depth >= 3:
                dirnames[:] = []
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".") and name not in skipped
            ]
            for filename in filenames:
                for extension in extensions:
                    if not filename.endswith(extension):
                        continue
                    relative = os.path.relpath(
                        os.path.join(dirpath, filename), root
                    )
                    module = relative[: -len(extension)].replace(os.sep, ".")
                    if module.endswith(".__init__"):
                        module = module[: -len(".__init__")]
                    if module and not module.startswith("__"):
                        found.add(module)
                    break
            if len(found) >= 500:
                break

    result = sorted(found)
    _LOCAL_MODULE_CACHE[key] = (now, result)
    return result


def _import_bindings(text: str) -> dict:
    bindings = {}
    for raw_line in text.splitlines():
        code = raw_line.split("#", 1)[0].strip()
        if not code:
            continue

        match = re.match(r"^from\s+([.\w]+)\s+(c?import)\s+(.+)$", code)
        if match:
            module = match.group(1)
            is_cimport = match.group(2) == "cimport"
            names = match.group(3).strip().strip("()")
            for part in names.split(","):
                name, alias = _split_alias(part.strip().strip("()"))
                if name and name != "*":
                    bindings[alias or name] = {
                        "module": module,
                        "name": name,
                        "cimport": is_cimport,
                    }
            continue

        match = re.match(r"^(c?import)\s+(.+)$", code)
        if match:
            is_cimport = match.group(1) == "cimport"
            names = match.group(2).strip().strip("()")
            for part in names.split(","):
                name, alias = _split_alias(part.strip().strip("()"))
                if not name:
                    continue
                bound = alias or name.split(".")[0]
                bindings[bound] = {
                    "module": name,
                    "name": None,
                    "cimport": is_cimport,
                }
    return bindings


def _module_comment(lines: list) -> str:
    comments = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
        elif stripped:
            break
    return "\n".join(comments).strip()


def _leading_comment(lines: list, lineno: int) -> str:
    comments = []
    index = lineno - 2
    while index >= 0:
        stripped = lines[index].strip()
        if stripped.startswith("@"):
            index -= 1
            continue
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
            index -= 1
            continue
        break
    return "\n".join(reversed(comments)).strip()


def _inline_comment(line: str) -> str:
    if "#" not in line:
        return ""
    return line.split("#", 1)[1].strip()


def _stub_signature(lines: list, lineno: int) -> str:
    parts = []
    depth = 0
    for index in range(lineno - 1, min(len(lines), lineno + 20)):
        stripped = lines[index].strip()
        if not stripped and parts:
            break
        parts.append(stripped)
        depth += stripped.count("(") + stripped.count("[") + stripped.count("{")
        depth -= stripped.count(")") + stripped.count("]") + stripped.count("}")
        if depth <= 0 and (
            stripped.endswith(":") or stripped.endswith("...") or stripped == "..."
        ):
            break
    signature = " ".join(parts)
    signature = re.sub(r":\s*(?:\.\.\.|pass)?\s*$", "", signature).strip()
    return signature[:300]


def _parse_stub(path: str) -> dict:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"path": path, "doc": "", "symbols": {}}
    cached = _STUB_FILE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
    except OSError:
        return {"path": path, "doc": "", "symbols": {}}

    data = {"path": path, "doc": "", "symbols": {}}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _STUB_FILE_CACHE[path] = (mtime, data)
        return data

    lines = source.splitlines()
    data["doc"] = ast.get_docstring(tree) or _module_comment(lines)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            doc = ast.get_docstring(node) or _leading_comment(lines, node.lineno)
            data["symbols"][name] = {
                "name": name,
                "kind": kind,
                "signature": _stub_signature(lines, node.lineno),
                "doc": (doc or "").strip(),
                "line": node.lineno - 1,
                "character": line_text.find(name),
                "path": path,
            }
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            line_text = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            doc = _leading_comment(lines, node.lineno) or _inline_comment(line_text)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                if name.startswith("__"):
                    continue
                data["symbols"][name] = {
                    "name": name,
                    "kind": "variable",
                    "signature": line_text.strip(),
                    "doc": (doc or "").strip(),
                    "line": node.lineno - 1,
                    "character": line_text.find(name),
                    "path": path,
                }

    _STUB_FILE_CACHE[path] = (mtime, data)
    return data


def _lookup_module_symbol(
    module: str,
    name,
    is_cimport: bool,
    document_path: str,
    root_path: str,
):
    if is_cimport:
        path = _cimport_path(module, document_path, root_path)
        if not path:
            return None
        exports = _parse_pxd_cached(path)
        if name is None:
            return {"path": path, "symbol": None, "module": module, "source": "pxd"}
        for export in exports:
            if export["name"] == name:
                return {
                    "path": path,
                    "symbol": export,
                    "module": module,
                    "source": "pxd",
                }
        return None

    path = _resolve_python_module(module, document_path, root_path)
    if not path:
        # ``import module`` can still refer to a compiled Cython extension
        # whose public declarations live in a .pxd file.
        return _lookup_module_symbol(
            module, name, True, document_path, root_path
        )
    data = _parse_stub(path)
    if name is None:
        return {
            "path": path,
            "symbol": None,
            "module": module,
            "module_doc": data["doc"],
            "source": "python",
        }
    symbol = data["symbols"].get(name)
    if symbol is None:
        return None
    return {
        "path": path,
        "symbol": symbol,
        "module": module,
        "source": "python",
    }


def _resolve_imported_symbol(
    word: str,
    text: str,
    document_path: str,
    root_path: str,
    alias: str = "",
):
    bindings = _import_bindings(text)
    if alias:
        entry = bindings.get(alias)
        if not entry:
            return None
        return _lookup_module_symbol(
            entry["module"], word, entry["cimport"], document_path, root_path
        )

    entry = bindings.get(word)
    if not entry:
        return None
    return _lookup_module_symbol(
        entry["module"], entry["name"], entry["cimport"], document_path, root_path
    )


def _resolved_display_path(path: str, root_path: str) -> str:
    if not path:
        return ""
    if root_path:
        try:
            relative = os.path.relpath(path, root_path)
            if not relative.startswith(".."):
                return relative
        except ValueError:
            pass
    return path


def _resolved_markdown(result: dict, root_path: str = "") -> str:
    symbol = result.get("symbol")
    path = result.get("path", "")
    display_path = _resolved_display_path(path, root_path)
    if symbol is None:
        value = result.get("module_doc") or "Module documentation."
        if display_path:
            value += "\n\n`%s`" % display_path
        return value

    language = "cython" if result.get("source") == "pxd" else "python"
    parts = []
    signature = symbol.get("signature", "")
    if signature:
        parts.append("```%s\n%s\n```" % (language, signature))
    doc = symbol.get("doc", "")
    if doc:
        parts.append(doc)
    location = display_path
    line = symbol.get("line", -1)
    if location and line is not None and line >= 0:
        location += ":%d" % (line + 1)
    if location:
        parts.append("`%s`" % location)
    return "\n\n".join(parts) if parts else "No documentation."


def _resolved_location(result: dict):
    path = result.get("path", "")
    if not path:
        return None
    symbol = result.get("symbol")
    if symbol is None:
        line = 0
        character = 0
        name = ""
    else:
        line = max(0, int(symbol.get("line", 0)))
        character = max(0, int(symbol.get("character", 0)))
        name = symbol.get("name", "")
    return {
        "uri": path_to_uri(path),
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": line, "character": character + len(name)},
        },
    }


def _module_exports(module: str) -> list:
    """Return names declared by a Cython include module (e.g. libc.string)."""

    if module in _PXD_EXPORTS:
        return _PXD_EXPORTS[module]
    exports = []
    includes = _cython_includes_dir()
    if includes:
        relative = module.replace(".", os.sep) + ".pxd"
        pxd_path = os.path.join(includes, relative)
        if os.path.isfile(pxd_path):
            exports = _parse_pxd_exports(pxd_path)
    _PXD_EXPORTS[module] = exports
    return exports


def _cimport_path(module: str, document_path: str, root_path: str) -> str:
    path = _resolve_module_file(module, document_path, root_path, [".pxd"])
    if path:
        return path
    includes = _cython_includes_dir()
    if includes:
        candidate = os.path.join(includes, module.replace(".", os.sep) + ".pxd")
        if os.path.isfile(candidate):
            return candidate
    return ""


def _module_exports_for(module: str, document_path: str, root_path: str) -> list:
    path = _cimport_path(module, document_path, root_path)
    if path:
        return _parse_pxd_cached(path)
    return _module_exports(module)


_MODULE_MEMBERS_CACHE = {}


def _include_submodules(package: str) -> list:
    includes = _cython_includes_dir()
    directory = os.path.join(includes, package)
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        entry[:-4]
        for entry in entries
        if entry.endswith(".pxd") and entry != "__init__.pxd"
    ]


def _members_for_module(
    module: str, is_cimport: bool, document_path: str, root_path: str
) -> list:
    if is_cimport:
        path = _cimport_path(module, document_path, root_path)
        exports = _parse_pxd_cached(path) if path else []
        if not exports:
            return []
        return [
            {
                "name": export["name"],
                "kind": export["kind"],
                "detail": "from " + module,
                "doc": export.get("doc")
                or "```cython\n%s\n```" % export.get("signature", ""),
                "signature": export.get("signature", ""),
                "path": path,
                "line": export.get("line", 0),
                "character": export.get("character", 0),
            }
            for export in exports
        ]

    path = _resolve_python_module(module, document_path, root_path)
    if not path:
        return _members_for_module(module, True, document_path, root_path)
    data = _parse_stub(path)
    return [
        {
            "name": symbol["name"],
            "kind": symbol["kind"],
            "detail": "from " + module,
            "doc": symbol.get("doc")
            or "```python\n%s\n```" % symbol.get("signature", ""),
            "signature": symbol.get("signature", ""),
            "path": path,
            "line": symbol.get("line", 0),
            "character": symbol.get("character", 0),
        }
        for symbol in data["symbols"].values()
    ]


def _module_members(
    module: str, document_path: str = "", root_path: str = ""
) -> list:
    if module in ("cython", "libc", "libcpp"):
        if module in _MODULE_MEMBERS_CACHE:
            return _MODULE_MEMBERS_CACHE[module]
        members = []
        if module == "cython":
            for name, (kind, doc) in CYTHON_MODULE_MEMBERS.items():
                members.append(
                    {"name": name, "kind": kind, "detail": "cython", "doc": doc}
                )
        else:
            for name in _include_submodules(module):
                members.append(
                    {
                        "name": name,
                        "kind": "module",
                        "detail": module,
                        "doc": "Cython module ``%s.%s``." % (module, name),
                    }
                )
        _MODULE_MEMBERS_CACHE[module] = members
        return members

    pxd_path = _cimport_path(module, document_path, root_path)
    if pxd_path:
        return [
            {
                "name": export["name"],
                "kind": export["kind"],
                "detail": "from " + module,
                "doc": export.get("doc")
                or "```cython\n%s\n```" % export.get("signature", ""),
                "signature": export.get("signature", ""),
                "path": pxd_path,
                "line": export.get("line", 0),
                "character": export.get("character", 0),
            }
            for export in _parse_pxd_cached(pxd_path)
        ]

    python_path = _resolve_python_module(module, document_path, root_path)
    if python_path:
        data = _parse_stub(python_path)
        return [
            {
                "name": symbol["name"],
                "kind": symbol["kind"],
                "detail": "from " + module,
                "doc": symbol.get("doc")
                or "```python\n%s\n```" % symbol.get("signature", ""),
                "signature": symbol.get("signature", ""),
                "path": python_path,
                "line": symbol.get("line", 0),
                "character": symbol.get("character", 0),
            }
            for symbol in data["symbols"].values()
        ]

    return []


def _matching_modules(prefix: str, modules: list) -> list:
    """Return modules that start with what the user already typed.

    Filtering on the server side prevents unrelated modules from showing up
    while the cursor is inside a partially typed path.  For example, inside
    ``from libc.`` only ``libc.*`` modules make sense, and ``libc.numpy``
    should suggest nothing.
    """

    result = []
    for module in modules:
        if module in result:
            continue
        if prefix and not module.startswith(prefix):
            continue
        result.append(module)
    return result


def build_completion_items(
    text: str, offset: int, document_path: str = "", root_path: str = ""
) -> list:
    line_start = text.rfind("\n", 0, offset) + 1
    before_cursor = text[line_start:offset]
    local_pxd = _local_modules(document_path, root_path, [".pxd"])
    local_python = _local_modules(document_path, root_path, [".pyi", ".py"])

    match = re.match(r"^\s*cimport\s+([\w\.]*)$", before_cursor)
    if match:
        edit_range = _range_between(text, line_start + match.start(1), offset)
        return [
            completion_item(
                module,
                COMPLETION_MODULE,
                "Cython module",
                "```cython\ncimport %s\n```" % module,
                "0" + module.lower(),
                text_edit={"range": edit_range, "newText": module},
            )
            for module in _matching_modules(
                match.group(1), COMMON_CIMPORTS + local_pxd
            )
        ]

    match = re.match(r"^\s*import\s+([\w\.]*)$", before_cursor)
    if match:
        edit_range = _range_between(text, line_start + match.start(1), offset)
        return [
            completion_item(
                module,
                COMPLETION_MODULE,
                "Python module",
                "```python\nimport %s\n```" % module,
                "0" + module.lower(),
                text_edit={"range": edit_range, "newText": module},
            )
            for module in _matching_modules(
                match.group(1), COMMON_MODULES + local_python
            )
        ]

    match = re.match(r"^\s*from\s+([\w\.]*)$", before_cursor)
    if match:
        prefix = match.group(1)
        modules = _matching_modules(
            prefix,
            COMMON_MODULES + COMMON_CIMPORTS + local_python + local_pxd,
        )
        edit_range = _range_between(text, line_start + match.start(1), offset)
        return [
            completion_item(
                module,
                COMPLETION_MODULE,
                "module",
                "```python\nfrom %s import ...\n```" % module,
                "0" + module.lower(),
                text_edit={"range": edit_range, "newText": module},
            )
            for module in modules
        ]

    match = re.match(r"^\s*from\s+([\w\.]+)\s+cimport\s+(.*)$", before_cursor)
    if match:
        module = match.group(1)
        exports = _module_exports_for(module, document_path, root_path)
        if exports:
            names_text = match.group(2)
            comma = names_text.rfind(",")
            raw_segment = names_text[comma + 1 :]
            leading = len(raw_segment) - len(raw_segment.lstrip())
            segment = raw_segment.strip()
            if not segment or re.fullmatch(r"[A-Za-z_]\w*", segment) is not None:
                edit_start = line_start + match.start(2) + comma + 1 + leading
                edit_range = _range_between(text, edit_start, offset)
                selected = [
                    export
                    for export in exports
                    if not segment or export["name"].startswith(segment)
                ]
                return [
                    completion_item(
                        export["name"],
                        KIND_TO_COMPLETION.get(
                            export["kind"], COMPLETION_FUNCTION
                        ),
                        "from " + module,
                        export.get("doc")
                        or "```cython\n%s\n```" % export.get("signature", ""),
                        "0" + export["name"].lower(),
                        text_edit={"range": edit_range, "newText": export["name"]},
                    )
                    for export in selected
                ]

    match = re.match(r"^\s*from\s+([\w\.]+)\s+import\s+(.*)$", before_cursor)
    if match:
        module = match.group(1)
        candidates = []
        language = "python"
        path = _resolve_python_module(module, document_path, root_path)
        if path:
            for symbol in _parse_stub(path)["symbols"].values():
                candidates.append(
                    {
                        "name": symbol["name"],
                        "kind": symbol["kind"],
                        "doc": symbol.get("doc", ""),
                        "signature": symbol.get("signature", ""),
                    }
                )
        if not candidates:
            language = "cython"
            for export in _module_exports_for(module, document_path, root_path):
                candidates.append(
                    {
                        "name": export["name"],
                        "kind": export["kind"],
                        "doc": export.get("doc", ""),
                        "signature": export.get("signature", ""),
                    }
                )

        if candidates:
            names_text = match.group(2)
            comma = names_text.rfind(",")
            raw_segment = names_text[comma + 1 :]
            leading = len(raw_segment) - len(raw_segment.lstrip())
            segment = raw_segment.strip()
            if not segment or re.fullmatch(r"[A-Za-z_]\w*", segment) is not None:
                edit_start = line_start + match.start(2) + comma + 1 + leading
                edit_range = _range_between(text, edit_start, offset)
                selected = [
                    candidate
                    for candidate in candidates
                    if not segment or candidate["name"].startswith(segment)
                ]
                return [
                    completion_item(
                        candidate["name"],
                        KIND_TO_COMPLETION.get(
                            candidate["kind"], COMPLETION_FUNCTION
                        ),
                        "from " + module,
                        candidate["doc"]
                        or "```%s\n%s\n```"
                        % (language, candidate["signature"]),
                        "0" + candidate["name"].lower(),
                        text_edit={
                            "range": edit_range,
                            "newText": candidate["name"],
                        },
                    )
                    for candidate in selected
                ]

    match = re.search(
        r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)?$", before_cursor
    )
    if match:
        module = match.group(1)
        prefix = match.group(2) or ""
        bindings = _import_bindings(text)
        entry = bindings.get(module)
        if module in ("cython", "libc", "libcpp"):
            members = _module_members(module, document_path, root_path)
        elif entry and entry.get("name") is None:
            target = entry["module"]
            if target in ("cython", "libc", "libcpp"):
                members = _module_members(target, document_path, root_path)
            else:
                members = _members_for_module(
                    target,
                    entry["cimport"],
                    document_path,
                    root_path,
                )
        else:
            members = _module_members(module, document_path, root_path)
        if members:
            edit_range = _range_between(text, offset - len(prefix), offset)
            lowered = prefix.lower()
            return [
                completion_item(
                    member["name"],
                    KIND_TO_COMPLETION.get(member["kind"], COMPLETION_TEXT),
                    member["detail"],
                    member["doc"],
                    "0" + member["name"].lower(),
                    text_edit={"range": edit_range, "newText": member["name"]},
                )
                for member in members
                if not lowered or member["name"].lower().startswith(lowered)
            ]

    items = []
    seen = set()

    def add(
        label: str,
        kind: int,
        detail: str = "",
        documentation: str = "",
        sort_text: str = "",
    ) -> None:
        if not label or label in seen:
            return
        seen.add(label)
        items.append(
            completion_item(
                label, kind, detail, documentation, sort_text or "5" + label.lower()
            )
        )

    for symbol in collect_symbols(text):
        add(
            symbol["name"],
            KIND_TO_COMPLETION.get(symbol["kind"], COMPLETION_TEXT),
            symbol["signature"],
            "Defined at line %d." % (symbol["line"] + 1),
            "0" + symbol["name"].lower(),
        )

    for name, detail, kind in collect_imports(text):
        documentation = "Imported name."
        resolved = _resolve_imported_symbol(
            name, text, document_path, root_path
        )
        if resolved is not None:
            symbol = resolved.get("symbol")
            if symbol is not None:
                documentation = symbol.get("doc") or (
                    "```%s\n%s\n```"
                    % (
                        "cython" if resolved.get("source") == "pxd" else "python",
                        symbol.get("signature", ""),
                    )
                )
            else:
                documentation = resolved.get("module_doc") or documentation
        add(
            name,
            KIND_TO_COMPLETION.get(kind, COMPLETION_MODULE),
            detail,
            documentation,
            "1" + name.lower(),
        )

    for name, doc in CYTHON_KEYWORD_DOCS.items():
        add(name, COMPLETION_KEYWORD, "Cython keyword", doc, "2" + name.lower())

    for name in keyword.kwlist + getattr(keyword, "softkwlist", []):
        add(
            name,
            COMPLETION_KEYWORD,
            "Python keyword",
            PYTHON_KEYWORD_DOCS.get(name, "Python keyword."),
            "3" + name.lower(),
        )

    for name, doc in CYTHON_EXTRA_DOCS.items():
        add(name, COMPLETION_TEXT, "Cython name", doc, "4" + name.lower())

    for name in dir(builtins):
        if name.startswith("_") and name not in {
            "__name__",
            "__file__",
            "__doc__",
            "__package__",
        }:
            continue
        value = getattr(builtins, name)
        if name in {"None", "True", "False", "NotImplemented", "Ellipsis"}:
            kind = COMPLETION_VARIABLE
        elif isinstance(value, type):
            kind = COMPLETION_CLASS
        else:
            kind = COMPLETION_FUNCTION
        add(name, kind, "Python builtin", "", "6" + name.lower())

    return items


# ---------------------------------------------------------------------------
# Diagnostics via the Cython compiler
# ---------------------------------------------------------------------------


class CythonChecker:
    def __init__(self, logger: Logger, root_path: str = "") -> None:
        self.logger = logger
        self.root_path = root_path
        self._lock = threading.Lock()
        self._command = None
        self._resolved = False

    def resolve_command(self):
        with self._lock:
            if self._resolved:
                return self._command

            command = None
            override = os.environ.get("CYTHON_LSP_CYTHON", "").strip()
            if override:
                try:
                    command = shlex.split(override)
                except ValueError:
                    command = None

            if not command:
                python = os.environ.get("CYTHON_LSP_PYTHON", "").strip()
                if not python:
                    python = sys.executable
                try:
                    probe = subprocess.run(
                        [python, "-c", "import Cython"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                    )
                    if probe.returncode == 0:
                        command = [python, "-m", "cython"]
                except (OSError, subprocess.SubprocessError):
                    command = None

            if not command:
                executable = shutil.which("cython")
                if executable:
                    command = [executable]

            self._command = command
            self._resolved = True
            self.logger.info("cython command: %r" % (command,))
            return command

    def check(self, text: str, path: str) -> list:
        command = self.resolve_command()
        if not command:
            return [
                self._diagnostic(
                    0,
                    0,
                    "Cython was not found. Install it with "
                    "`python3 -m pip install Cython`, or set "
                    "CYTHON_LSP_CYTHON to the compiler command.",
                    text,
                )
            ]

        extension = os.path.splitext(path)[1].lower()
        if extension not in (".pyx", ".pxd", ".pxi"):
            extension = ".pyx"

        temp_dir = tempfile.mkdtemp(prefix="cython-lsp-")
        try:
            document_dir = os.path.dirname(os.path.abspath(path)) or os.getcwd()
            buffer_name = "buffer" + extension
            source_path = os.path.join(temp_dir, buffer_name)
            with open(source_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)

            if extension == ".pxd":
                # Compile a tiny module that cimports the .pxd.  This avoids
                # false "declared but not defined" errors from compiling a
                # declaration file directly.
                target = os.path.join(temp_dir, "check.pyx")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("cimport buffer\n")
            elif extension == ".pxi":
                target = os.path.join(temp_dir, "check.pyx")
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write('include "%s"\n' % buffer_name)
            else:
                target = source_path

            arguments = list(command) + ["-3"]
            if (
                re.search(r"\bcimport\s+libcpp\b", text)
                or re.search(
                    r"^\s*#\s*distutils:\s*language\s*=\s*c\+\+", text, re.MULTILINE
                )
                or re.search(
                    r"^\s*#\s*cython:\s*language\s*=\s*c\+\+", text, re.MULTILINE
                )
            ):
                arguments.append("--cplus")
            include_dirs = [document_dir, temp_dir]
            if self.root_path:
                include_dirs.append(self.root_path)
            for include_dir in include_dirs:
                if include_dir and os.path.isdir(include_dir):
                    arguments.extend(["-I", include_dir])
            arguments.extend(
                ["-o", os.path.join(temp_dir, "buffer.c"), target]
            )

            environment = os.environ.copy()
            environment.setdefault("PYTHONIOENCODING", "utf-8")

            try:
                process = subprocess.run(
                    arguments,
                    cwd=temp_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                return [
                    self._diagnostic(
                        0,
                        0,
                        "Cython compilation timed out after 30 seconds.",
                        text,
                    )
                ]
            except OSError as error:
                return [
                    self._diagnostic(
                        0,
                        0,
                        "Could not run Cython (%s): %s" % (" ".join(command), error),
                        text,
                    )
                ]

            output = (process.stderr or "") + "\n" + (process.stdout or "")
            diagnostics = self._parse_output(output, buffer_name, text)
            if process.returncode != 0 and not diagnostics:
                message = self._fallback_message(output)
                diagnostics = [self._diagnostic(0, 0, message, text)]
            return diagnostics
        except Exception:
            self.logger.error("diagnostics failed:\n" + traceback.format_exc())
            return [
                self._diagnostic(
                    0, 0, "cython-lsp internal error while running Cython.", text
                )
            ]
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _parse_output(self, output: str, buffer_name: str, text: str) -> list:
        lines = text.splitlines()
        diagnostics = []
        seen = set()
        position_re = re.compile(
            r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.+)$"
        )

        for raw_line in output.splitlines():
            match = position_re.match(raw_line.strip())
            if not match:
                continue
            filename = os.path.basename(match.group("file").replace("\\", "/"))
            if filename != buffer_name:
                continue
            try:
                line_number = int(match.group("line")) - 1
                column = int(match.group("col")) - 1
            except ValueError:
                continue
            message = match.group("msg").strip()
            if not message:
                continue
            key = (line_number, column, message)
            if key in seen:
                continue
            seen.add(key)
            severity = 1
            lowered = message.lower()
            if lowered.startswith("warning:") or " warning: " in lowered:
                severity = 2
            diagnostics.append(
                self._diagnostic(line_number, column, message, text, severity)
            )
            if len(diagnostics) >= 100:
                break

        diagnostics.sort(
            key=lambda item: (
                item["range"]["start"]["line"],
                item["range"]["start"]["character"],
            )
        )
        return diagnostics

    def _fallback_message(self, output: str) -> str:
        skip = {
            "Error compiling Cython file:",
            "Compiler crash traceback from this point on:",
        }
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line in skip or line == "..." or set(line) == {"-"}:
                continue
            if line.startswith("File '"):
                continue
            return line[:500]
        return "Cython compilation failed."

    def _diagnostic(
        self,
        line_number: int,
        column: int,
        message: str,
        text: str,
        severity: int = 1,
    ) -> dict:
        lines = text.splitlines() or [""]
        line_number = max(0, min(line_number, len(lines) - 1))
        line = lines[line_number]
        column = max(0, min(column, len(line)))
        if column < len(line):
            token = re.match(r"[A-Za-z_]\w*", line[column:])
            end_character = column + (len(token.group(0)) if token else 1)
        else:
            end_character = column
        return {
            "range": {
                "start": {"line": line_number, "character": column},
                "end": {"line": line_number, "character": end_character},
            },
            "severity": severity,
            "source": "cython",
            "message": message,
        }


# ---------------------------------------------------------------------------
# JSON-RPC / LSP plumbing
# ---------------------------------------------------------------------------


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Connection:
    def __init__(self, reader: asyncio.StreamReader, logger: Logger) -> None:
        self.reader = reader
        self.logger = logger

    async def read_message(self):
        headers = {}
        while True:
            line = await self.reader.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            try:
                name, value = line.decode("ascii", "replace").split(":", 1)
            except ValueError:
                continue
            headers[name.strip().lower()] = value.strip()

        if "content-length" not in headers:
            return None
        try:
            length = int(headers["content-length"])
        except ValueError:
            return None
        if length <= 0 or length > 64 * 1024 * 1024:
            return None
        body = await self.reader.readexactly(length)
        return json.loads(body.decode("utf-8"))

    def send(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        stream = sys.stdout.buffer
        stream.write(b"Content-Length: %d\r\n\r\n" % len(data))
        stream.write(data)
        stream.flush()

    def reply(self, message_id, result) -> None:
        self.send({"jsonrpc": "2.0", "id": message_id, "result": result})

    def reply_error(self, message_id, code: int, message: str) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": code, "message": message},
            }
        )

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# The language server
# ---------------------------------------------------------------------------


class CythonLanguageServer:
    def __init__(self, connection: Connection, logger: Logger) -> None:
        self.connection = connection
        self.logger = logger
        self.documents = {}
        self.diagnostics_generation = {}
        self.diagnostics_tasks = {}
        self.compile_lock = asyncio.Semaphore(1)
        self.checker = CythonChecker(logger)
        self.root_path = ""
        self.diagnostics_enabled = True
        self.shutdown_received = False
        self.exit_requested = False

    # -- main loop ---------------------------------------------------------

    async def serve(self) -> int:
        self.logger.info("cython-lsp %s started" % SERVER_VERSION)
        while not self.exit_requested:
            try:
                message = await self.connection.read_message()
            except asyncio.IncompleteReadError:
                break
            except (ConnectionResetError, BrokenPipeError):
                break
            except json.JSONDecodeError as error:
                self.logger.error("invalid JSON-RPC message: %s" % error)
                continue
            except Exception:
                self.logger.error("read failed:\n" + traceback.format_exc())
                break
            if message is None:
                break
            try:
                await self.handle_message(message)
            except Exception:
                self.logger.error("message handling failed:\n" + traceback.format_exc())
        self.logger.info("cython-lsp stopped")
        return 0 if self.shutdown_received else 1

    async def handle_message(self, message: dict) -> None:
        method = message.get("method")
        if not method:
            return
        if "id" not in message:
            await self.handle_notification(method, message.get("params") or {})
            return
        message_id = message.get("id")
        try:
            result = await self.handle_request(method, message.get("params") or {})
        except RpcError as error:
            self.connection.reply_error(message_id, error.code, error.message)
        except Exception as error:
            self.logger.error(
                "request %s failed:\n%s" % (method, traceback.format_exc())
            )
            self.connection.reply_error(message_id, -32603, str(error))
        else:
            self.connection.reply(message_id, result)

    async def handle_request(self, method: str, params: dict):
        if method == "initialize":
            return self.initialize(params)
        if method == "shutdown":
            self.shutdown_received = True
            return None
        if method == "textDocument/completion":
            return self.completion(params)
        if method == "textDocument/hover":
            return self.hover(params)
        if method == "textDocument/definition":
            return self.definition(params)
        if method == "textDocument/documentSymbol":
            return self.document_symbols(params)
        if method in ("textDocument/didOpen", "textDocument/didChange"):
            raise RpcError(-32600, "%s is a notification" % method)
        raise RpcError(-32601, "Method not found: %s" % method)

    async def handle_notification(self, method: str, params: dict) -> None:
        if method == "initialized":
            return
        if method == "textDocument/didOpen":
            self.did_open(params)
            return
        if method == "textDocument/didChange":
            self.did_change(params)
            return
        if method == "textDocument/didSave":
            self.did_save(params)
            return
        if method == "textDocument/didClose":
            self.did_close(params)
            return
        if method == "workspace/didChangeConfiguration":
            self.did_change_configuration(params)
            return
        if method == "exit":
            self.exit_requested = True
            return
        if method in ("$/cancelRequest", "$/setTrace"):
            return
        self.logger.info("ignored notification: %s" % method)

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, params: dict) -> dict:
        root = params.get("rootUri") or params.get("rootPath") or ""
        if isinstance(root, str) and root.startswith("file://"):
            self.root_path = uri_to_path(root)
        elif isinstance(root, str):
            self.root_path = root
        else:
            self.root_path = ""
        self.checker.root_path = self.root_path

        options = params.get("initializationOptions") or {}
        if isinstance(options, dict):
            if options.get("diagnostics") is False:
                self.diagnostics_enabled = False
            python_path = options.get("pythonPath")
            if isinstance(python_path, str) and python_path:
                os.environ.setdefault("CYTHON_LSP_PYTHON", python_path)

        self.logger.info(
            "initialize (root=%r, diagnostics=%s)"
            % (self.root_path, self.diagnostics_enabled)
        )
        return {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": 1,
                    "save": {"includeText": True},
                },
                "completionProvider": {
                    "triggerCharacters": [".", "'", '"', "<"],
                    "resolveProvider": False,
                },
                "hoverProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def did_open(self, params: dict) -> None:
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        if not uri:
            return
        self.documents[uri] = {
            "text": document.get("text", ""),
            "version": document.get("version", 0) or 0,
            "path": uri_to_path(uri),
            "language_id": document.get("languageId", "cython"),
        }
        self.schedule_diagnostics(uri)

    def did_change(self, params: dict) -> None:
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        existing = self.documents.get(uri)
        if existing is None:
            return
        text = existing["text"]
        for change in params.get("contentChanges") or []:
            text = apply_change(text, change)
        existing["text"] = text
        if "version" in document:
            existing["version"] = document.get("version") or 0
        else:
            existing["version"] = existing.get("version", 0) + 1
        self.schedule_diagnostics(uri)

    def did_save(self, params: dict) -> None:
        uri = (params.get("textDocument") or {}).get("uri")
        document = self.documents.get(uri)
        if document is None:
            return
        if "text" in params and params["text"] is not None:
            document["text"] = params["text"]
        self.schedule_diagnostics(uri)

    def did_close(self, params: dict) -> None:
        uri = (params.get("textDocument") or {}).get("uri")
        if not uri:
            return
        self.diagnostics_generation[uri] = (
            self.diagnostics_generation.get(uri, 0) + 1
        )
        task = self.diagnostics_tasks.pop(uri, None)
        if task is not None:
            task.cancel()
        self.documents.pop(uri, None)
        self.connection.notify(
            "textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []}
        )

    def did_change_configuration(self, params: dict) -> None:
        settings = params.get("settings") or {}
        if isinstance(settings, dict) and isinstance(settings.get("cython"), dict):
            cython_settings = settings["cython"]
            if "diagnostics" in cython_settings:
                self.diagnostics_enabled = bool(cython_settings["diagnostics"])

    # -- diagnostics -------------------------------------------------------

    def schedule_diagnostics(self, uri: str) -> None:
        if not self.diagnostics_enabled:
            return
        generation = self.diagnostics_generation.get(uri, 0) + 1
        self.diagnostics_generation[uri] = generation
        # Do not cancel a previous run that may already be inside the Cython
        # subprocess: old runs notice the generation change and discard their
        # result, while the semaphore keeps at most one compiler alive.
        self.diagnostics_tasks[uri] = asyncio.create_task(
            self._diagnostics_after_delay(uri, generation)
        )

    async def _diagnostics_after_delay(self, uri: str, generation: int) -> None:
        try:
            await asyncio.sleep(0.30)
            if self.diagnostics_generation.get(uri) != generation:
                return
            document = self.documents.get(uri)
            if document is None:
                return
            text = document["text"]
            version = document.get("version", 0)
            path = document.get("path") or uri_to_path(uri)
            async with self.compile_lock:
                diagnostics = await asyncio.to_thread(
                    self.checker.check, text, path
                )
            if self.diagnostics_generation.get(uri) != generation:
                return
            if uri not in self.documents:
                return
            self.connection.notify(
                "textDocument/publishDiagnostics",
                {
                    "uri": uri,
                    "version": version,
                    "diagnostics": diagnostics,
                },
            )
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.error("diagnostics failed:\n" + traceback.format_exc())
        finally:
            if self.diagnostics_tasks.get(uri) is asyncio.current_task():
                self.diagnostics_tasks.pop(uri, None)

    # -- feature handlers --------------------------------------------------

    def _document(self, params: dict):
        uri = (params.get("textDocument") or {}).get("uri", "")
        return uri, self.documents.get(uri)

    def completion(self, params: dict) -> dict:
        _, document = self._document(params)
        if document is None:
            return {"isIncomplete": False, "items": []}
        text = document["text"]
        offset = position_to_offset(text, params.get("position") or {})
        return {
            "isIncomplete": False,
            "items": build_completion_items(
                text,
                offset,
                document.get("path", ""),
                self.root_path,
            ),
        }

    def hover(self, params: dict):
        _, document = self._document(params)
        if document is None:
            return None
        text = document["text"]
        offset = position_to_offset(text, params.get("position") or {})
        _, start, end, word = word_at(text, offset)
        if not word:
            return None
        document_path = document.get("path", "")

        value = ""
        attribute_alias, attribute_member = _attribute_at(text, offset)
        resolved = None
        if attribute_alias and attribute_member:
            resolved = _resolve_imported_symbol(
                attribute_member,
                text,
                document_path,
                self.root_path,
                alias=attribute_alias,
            )
        if resolved is not None:
            value = _resolved_markdown(resolved, self.root_path)
        if word in CYTHON_KEYWORD_DOCS:
            value = "**%s** - Cython keyword\n\n%s" % (
                word,
                CYTHON_KEYWORD_DOCS[word],
            )
        else:
            for symbol in collect_symbols(text):
                if symbol["name"] == word:
                    value = "```cython\n%s\n```\n\nDefined at line %d." % (
                        symbol["signature"],
                        symbol["line"] + 1,
                    )
                    break
        if not value and word in PYTHON_KEYWORD_DOCS:
            value = "**%s** - Python keyword\n\n%s" % (
                word,
                PYTHON_KEYWORD_DOCS[word],
            )
        if not value and word in CYTHON_EXTRA_DOCS:
            value = "**%s** - Cython name\n\n%s" % (
                word,
                CYTHON_EXTRA_DOCS[word],
            )
        if not value:
            resolved = _resolve_imported_symbol(
                word, text, document_path, self.root_path
            )
            if resolved is not None:
                value = _resolved_markdown(resolved, self.root_path)
        if not value and hasattr(builtins, word):
            value = builtin_documentation(word)
        if not value:
            return None

        line_start = text.rfind("\n", 0, offset) + 1
        return {
            "contents": {"kind": "markdown", "value": value},
            "range": {
                "start": offset_to_position(text, line_start + start),
                "end": offset_to_position(text, line_start + end),
            },
        }

    def definition(self, params: dict):
        uri, document = self._document(params)
        if document is None:
            return None
        text = document["text"]
        offset = position_to_offset(text, params.get("position") or {})
        _, _, _, word = word_at(text, offset)
        if not word:
            return None
        document_path = document.get("path", "")

        attribute_alias, attribute_member = _attribute_at(text, offset)
        if attribute_alias and attribute_member:
            resolved = _resolve_imported_symbol(
                attribute_member,
                text,
                document_path,
                self.root_path,
                alias=attribute_alias,
            )
            location = _resolved_location(resolved) if resolved else None
            if location:
                return location

        for symbol in collect_symbols(text):
            if symbol["name"] != word:
                continue
            start = offset_to_position(
                text, _line_start_offset(text, symbol["line"]) + symbol["character"]
            )
            end = dict(start)
            end["character"] += len(word)
            return {
                "uri": uri,
                "range": {"start": start, "end": end},
            }

        resolved = _resolve_imported_symbol(
            word, text, document_path, self.root_path
        )
        location = _resolved_location(resolved) if resolved else None
        return location

    def document_symbols(self, params: dict) -> list:
        _, document = self._document(params)
        if document is None:
            return []
        text = document["text"]
        lines = text.splitlines()
        result = []
        for symbol in collect_symbols(text):
            kind = KIND_TO_SYMBOL.get(symbol["kind"])
            if kind is None or symbol["kind"] == "variable":
                continue
            line_number = symbol["line"]
            line_text = lines[line_number] if line_number < len(lines) else ""
            start = offset_to_position(
                text, _line_start_offset(text, line_number) + symbol["character"]
            )
            end = dict(start)
            end["character"] += len(symbol["name"])
            result.append(
                {
                    "name": symbol["name"],
                    "kind": kind,
                    "range": {
                        "start": {"line": line_number, "character": 0},
                        "end": {
                            "line": line_number,
                            "character": len(line_text),
                        },
                    },
                    "selectionRange": {"start": start, "end": end},
                    "detail": symbol["signature"],
                }
            )
        return result


def _line_start_offset(text: str, line_number: int) -> int:
    """Return the character offset of the beginning of *line_number*."""

    if line_number <= 0:
        return 0
    offset = 0
    current = 0
    for line in text.splitlines(keepends=True):
        if current == line_number:
            return offset
        offset += len(line)
        current += 1
    return len(text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_server() -> int:
    logger = Logger()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    connection = Connection(reader, logger)
    server = CythonLanguageServer(connection, logger)
    return await server.serve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Language server for Cython (.pyx/.pxd/.pxi)."
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="use stdio (the only transport; accepted for compatibility)",
    )
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--check",
        metavar="FILE",
        help="run the Cython checker on FILE and print JSON diagnostics",
    )
    arguments = parser.parse_args()

    if arguments.version:
        print("%s %s" % (SERVER_NAME, SERVER_VERSION))
        return 0

    if arguments.check:
        logger = Logger()
        checker = CythonChecker(logger, root_path=os.getcwd())
        path = os.path.abspath(arguments.check)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        print(json.dumps(checker.check(text, path), indent=2))
        return 0

    try:
        return asyncio.run(run_server())
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
