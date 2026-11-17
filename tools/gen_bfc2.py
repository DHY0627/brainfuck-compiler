#!/usr/bin/env python3
"""Generate `bfsrc/bfc2.bf`: a Brainfuck program that reads a Brainfuck
program on stdin and writes a complete Windows PE32+ executable to stdout.

Unlike bfc1 (which streamed C source for gcc to compile), bfc2 emits the actual
machine bytes: the fixed PAK prefix from `tools/pak.py` followed by one record
per BF instruction and the terminator record.  The produced .exe carries a
self-reading loader stub, so the program runs natively; no assembler, linker or
C compiler is involved anywhere in the chain.

Semantics of the emitted records are defined by tools/pak.py; the byte values
below are taken from that module so the two can never drift apart.

Cell map (12 cells, untouched tape beyond them):
    0   C     current input byte
    1   AUX   copy helper (restores C)
    2   T     scratch for the equality test
    3..10     one-hot flags, one per BF instruction
    11  O     byte builder: O = value, print O, clear O
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pak  # noqa: E402

OUT = os.path.join(ROOT, "bfsrc", "bfc2.bf")

C, AUX, T, O = 0, 1, 2, 11
FLAG0 = 3
KEYS = b"+-><.,[]"


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
    """Print every byte of `data` directly with '.', never staging the stream."""
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
    e.mv(AUX); e.raw("[")
    e.mv(C);   e.raw("+")
    e.mv(AUX); e.raw("-")
    e.mv(AUX); e.raw("]")


def test_equal(e, flag, k):
    """flag := (C == k); uses T as scratch and leaves T == 0."""
    copy_c_to_t(e)
    e.mv(T); e.raw("-" * k)
    e.plus(flag, 1)
    e.mv(T); e.raw("[")
    e.mv(flag); e.raw("-")
    e.mv(T); e.raw("[-]")
    e.mv(T); e.raw("]")


def build():
    e = Emit()

    # 1. fixed PE prefix (headers + import table + loader stub)
    emit_bytes(e, pak.PREFIX)

    # 2. read the first source byte (0 from ',' means EOF, like bfc.exe)
    e.mv(C); e.raw(",")

    # 3. dispatch loop: classify, emit that instruction's record, read next
    e.mv(C); e.raw("[")
    for i, ch in enumerate(KEYS):
        test_equal(e, FLAG0 + i, ch)
    for i, ch in enumerate(KEYS):
        f = FLAG0 + i
        e.mv(f); e.raw("[")
        emit_bytes(e, pak.RECORDS[ch])
        e.clear(f)
        e.mv(f); e.raw("]")
    e.mv(C); e.raw(",")
    e.mv(C); e.raw("]")

    # 4. terminator record: exit(0) via ExitProcess
    emit_bytes(e, pak.REC_END)
    return e.text()


def main():
    src = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="ascii", newline="") as f:
        f.write(src)
    instructions = sum(src.count(ch) for ch in "+-<>.,[]")
    print("wrote %s" % OUT)
    print("  %d bytes (%d BF instructions, %d non-instruction)"
          % (len(src), instructions, len(src) - instructions))
    print("  fixed prefix: %d bytes, terminator: %d bytes, %d instruction records"
          % (len(pak.PREFIX), len(pak.REC_END), len(KEYS)))


if __name__ == "__main__":
    main()
