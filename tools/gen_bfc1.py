#!/usr/bin/env python3
"""Generate `bfsrc/bfc1.bf`: a Brainfuck program that reads a Brainfuck
program on stdin and streams its C translation to stdout.

The emitted C is byte-for-byte identical to `tools/bf2c.py::translate()`
(`tools/verify_bfc1.py` and `bootstrap/selfhost.py` prove it).

How it works
------------
The program is an 8-way dispatch loop; every input byte selects exactly one
of eight fixed C fragments (see tools/bf2c.py KEYS/TEMPLATES) and prints it.
There is no lookahead, no buffering of output and no address arithmetic:
the whole compiler lives in 12 tape cells, so it also runs on the default
30000-cell tape of the programs bfc.exe generates.

Cell map
--------
    0   C     current input byte
    1   AUX   copy helper (restores C)
    2   T     scratch: T = C - k  for the equality test
    3..10     one flag per instruction (one-hot)
    11  O     byte builder: O = value, print O, clear O

Per input byte:
    for each k in KEYS:                 # 8 tests
        T = C (C preserved via AUX)
        T -= k
        flag_k = (T == 0)               # set 1, cleared iff T != 0
    for each k in KEYS:                 # exactly one guard fires
        flag_k [ emit TEMPLATES[k] ; flag_k = 0 ]

Output text is written one character at a time with '.' -- i.e. straight to
the console, never staged in cells.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bf2c  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "bfsrc", "bfc1.bf")

C, AUX, T, O = 0, 1, 2, 11
FLAG0 = 3
KEYS = bf2c.KEYS


class Emit:
    """Tracks the data pointer so generated code uses minimal '<'/'>' moves."""

    def __init__(self):
        self.parts = []
        self.pos = 0

    def raw(self, s):
        self.parts.append(s)

    def mv(self, cell):
        d = cell - self.pos
        if d > 0:
            self.raw(">" * d)
        elif d < 0:
            self.raw("<" * (-d))
        self.pos = cell

    def plus(self, cell, n):
        if n:
            self.mv(cell)
            self.raw("+" * n)

    def clear(self, cell):
        self.mv(cell)
        self.raw("[-]")

    def text(self):
        return "".join(self.parts)


def emit_bytes(e, data):
    """Print `data` on the console, one byte at a time, direct '.'."""
    for b in data:
        e.mv(O)
        if b:
            e.raw("+" * b)
        e.raw(".")
        e.raw("[-]")


def copy_c_to_t(e):
    """T = C, C preserved.  Requires T == 0 and AUX == 0 on entry."""
    e.mv(C);   e.raw("[")
    e.mv(T);   e.raw("+")
    e.mv(AUX); e.raw("+")
    e.mv(C);   e.raw("-")
    e.mv(C);   e.raw("]")
    # restore C from AUX
    e.mv(AUX); e.raw("[")
    e.mv(C);   e.raw("+")
    e.mv(AUX); e.raw("-")
    e.mv(AUX); e.raw("]")


def test_equal(e, flag, k):
    """flag := (C == k); uses T as scratch and leaves T == 0."""
    copy_c_to_t(e)                 # T = C
    e.mv(T); e.raw("-" * k)        # T = C - k   (wraps, never 0 unless equal)
    e.plus(flag, 1)                # flag = 1
    e.mv(T); e.raw("[")            # if T != 0 -> not equal
    e.mv(flag); e.raw("-")         #   flag = 0
    e.mv(T); e.raw("[-]")          #   T = 0
    e.mv(T); e.raw("]")


def build():
    e = Emit()

    # 1. fixed prologue of the generated C program
    emit_bytes(e, bf2c.PRELUDE.encode("ascii"))

    # 2. read the first source byte; 0 means EOF (bfc.exe semantics)
    e.mv(C); e.raw(",")

    # 3. while C != 0: dispatch + emit, then read the next byte
    e.mv(C); e.raw("[")
    for i, ch in enumerate(KEYS):
        test_equal(e, FLAG0 + i, ord(ch))
    for i, ch in enumerate(KEYS):
        f = FLAG0 + i
        e.mv(f); e.raw("[")
        emit_bytes(e, bf2c.TEMPLATES[ch].encode("ascii"))
        e.clear(f)
        e.mv(f); e.raw("]")
    e.mv(C); e.raw(",")
    e.mv(C); e.raw("]")

    # 4. fixed epilogue
    emit_bytes(e, bf2c.EPILOGUE.encode("ascii"))
    return e.text()


def main():
    src = build()
    with open(OUT, "w", encoding="ascii", newline="") as f:
        f.write(src)
    instructions = sum(src.count(ch) for ch in "+-<>.,[]")
    print("wrote %s" % OUT)
    print("  %d bytes (%d BF instructions, %d non-instruction)"
          % (len(src), instructions, len(src) - instructions))


if __name__ == "__main__":
    main()
