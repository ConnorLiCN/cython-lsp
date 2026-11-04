; inherits: python

; Cython declarations such as `cpdef f(...):`, `cdef class C:`,
; `cdef extern from "...":` are not valid Python, so tree-sitter-python
; wraps them in ERROR nodes.  Indent after their trailing colon.
(ERROR
  ":" @indent @extend)
((ERROR) @indent @extend
  (#match? @indent ":\\s*$"))
; Fallback for Cython-only header tails (nogil/noexcept/...) whose colon is
; not part of the surrounding ERROR node.  Normal Python suites are already
; covered by the inherited function/class/if/... patterns.
":" @indent @extend
