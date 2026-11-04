; inherits: python
; Cython keywords are plain identifiers for the Python grammar, so promote
; them to keywords.  This file is only used when Helix looks up queries by
; language name; when it uses the grammar name, Python queries apply directly.

; `from ... cimport ...` is invalid Python, so tree-sitter wraps the line in
; an ERROR node and the imported module/names fall back to @variable.
; Highlight identifiers in that error like Python import names.
((ERROR
  (identifier) @namespace) @_cimport_error
  (#match? @_cimport_error "cimport")
  (#not-any-of? @namespace "cimport" "from"))

((identifier) @keyword
  (#any-of? @keyword
    "cdef" "cpdef" "cimport" "ctypedef" "nogil" "gil" "prange" "fused"
    "extern" "public" "readonly" "api" "inline" "packed" "include"
    "DEF" "IF" "ELIF" "ELSE"))

; C/Cython types.  Python's queries already know float/int/str/..., but
; char, double, long, size_t and the fixed-width integer types are Cython-only.
((identifier) @type.builtin
  (#any-of? @type.builtin
    "char" "double" "long" "short" "signed" "unsigned" "void"
    "float" "int" "bool" "object" "complex" "bytes" "bytearray"
    "str" "dict" "list" "tuple" "set" "frozenset" "memoryview"
    "bint" "size_t" "ssize_t" "Py_ssize_t" "Py_UCS4" "intptr_t" "uintptr_t"
    "int8_t" "int16_t" "int32_t" "int64_t"
    "uint8_t" "uint16_t" "uint32_t" "uint64_t"))
