#!/usr/bin/env python3
"""Generate bfsrc/bfc0.bf -- a Brainfuck program that compiles Brainfuck.

bfc0 reads a Brainfuck program on stdin and writes a complete, runnable
PE32+ console executable on stdout. It emits the stage-0 image verbatim
(headers, entry stub, import table -- all constants) with the compiled
x86-64 code spliced in, so its output needs no further processing.

How the emitted Brainfuck works
-------------------------------
Tape layout:

    0  c      the current source byte
    1  t      test scratch: c is copied here and destroyed by the test
    2  tmp    holds a second copy of c, and doubles as the match flag
    3  zero   permanently 0, used to emit 0x00 bytes cheaply
    4+ pool   one cell per distinct non-zero byte value; ".` on such a cell
              emits that byte, so the compiler can print any constant byte

Per source byte, six equality tests run in sequence (`+ - > < . ,`). Each test:

    [->+>+<<]   move c into t and tmp
    >>[-<<+>>]  restore c from tmp (tmp becomes 0)
    <-----------  t -= K   (K = the opcode's ASCII value)
    >+<         flag = 1
    [->-<]      if t != 0 then flag = 0 and t = 0     -> flag = (c == K)
    >[          if it matched:
        <emit the opcode's fixed machine-code sequence>
        <<[-]+>>    poison c so later tests cannot match
        [-]         clear the flag
    ]
    <<          back to c

`[` and `]` are compiled to `ud2` by this compiler, so a program that uses
loops stops immediately instead of silently computing something wrong.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "bootstrap"))
import bfcodegen as cg      # noqa: E402
import mkstage0 as st       # noqa: E402

C, T, TMP, ZERO, POOL0 = 0, 1, 2, 3, 4

ORDER = [ord('+'), ord('-'), ord('>'), ord('<'), ord('.'), ord(',')]


class Emitter:
    """Builds Brainfuck text while tracking where the tape pointer is."""

    def __init__(self, pool):
        self.pool = pool          # {byte value: cell index}
        self.cur = 0
        self.parts = []

    def move(self, cell):
        d = cell - self.cur
        if d:
            self.parts.append(">" * d if d > 0 else "<" * -d)
            self.cur = cell

    def out_byte(self, v):
        """Emit one constant byte."""
        if v == 0:
            self.move(ZERO)
        else:
            self.move(self.pool[v])
        self.parts.append(".")

    def out_bytes(self, bs):
        """Emit a run of bytes, batching consecutive zeros."""
        i = 0
        while i < len(bs):
            v = bs[i]
            if v == 0:
                j = i
                while j < len(bs) and bs[j] == 0:
                    j += 1
                self.move(ZERO)
                self.parts.append("." * (j - i))
                i = j
            else:
                self.out_byte(v)
                i += 1

    def raw(self, text):
        self.parts.append(text)

    def text(self):
        return "".join(self.parts)


def collect_pool():
    """Every non-zero byte the compiler may ever print, in first-use order."""
    seen = []
    seen_set = set()

    def note(bs):
        for v in bs:
            if v and v not in seen_set:
                seen_set.add(v)
                seen.append(v)

    note(prefix_bytes())
    for op in ORDER:
        note(cg.OPS[op])
    return {v: POOL0 + i for i, v in enumerate(seen)}


def prefix_bytes():
    """The stage-0 image up to the staged-code offset: headers, stub, .rdata."""
    return st.build(b"\xc3")[:st.SRC_RAW]


def setup_code(pool):
    """Load each pool cell with its byte value (using the shorter of +/-).

    Returns (brainfuck_text, final_cell) so callers know where the pointer
    ends up: forgetting this is exactly the bug that made the first version
    emit every prefix byte from the wrong cell.
    """
    out = []
    cells = sorted(pool.items(), key=lambda kv: kv[1])
    cur = 0
    for value, cell in cells:
        d = cell - cur
        if d:
            out.append(">" * d if d > 0 else "<" * -d)
            cur = cell
        if value <= 128:
            out.append("+" * value)
        else:
            out.append("-" * (256 - value))
    return "".join(out), cur


def test_code(e, op):
    """The six-step equality test for one opcode, plus its emit body."""
    e.raw("[->+>+<<]")        # c -> t, tmp ; c = 0
    e.cur = C
    e.raw(">>[-<<+>>]")       # tmp -> c ; tmp = 0
    e.cur = TMP
    e.move(T)                 # pointer at t
    e.raw("-" * op)           # t -= K
    e.raw(">+<")              # flag = 1
    # if t != 0 then clear the flag and t.  NOTE: `[->-<]` would decrement the
    # flag once per iteration, so it only lands on 0 when t == 1; the correct
    # idiom clears both cells on the first iteration.
    e.raw("[>[-]<[-]]")       # flag = (t == 0), i.e. c == K
    e.move(TMP)
    e.raw("[")
    e.out_bytes(cg.OPS[op])   # emit the machine code for this opcode
    e.move(C)
    e.raw("[-]+")             # poison c: no later test can match
    e.move(TMP)
    e.raw("[-]")              # clear the flag so the loop exits
    e.raw("]")
    e.move(C)


def build_bf():
    pool = collect_pool()
    setup, setup_end = setup_code(pool)

    # part 1: the constant image prefix
    e = Emitter(pool)
    e.cur = setup_end
    e.out_bytes(prefix_bytes())

    # part 2: the compiler loop (must start by reading into cell C)
    e2 = Emitter(pool)
    e2.cur = e.cur
    e2.move(C)
    e2.raw(",")
    e2.raw("[")
    for op in ORDER:
        test_code(e2, op)
    e2.raw(",")
    e2.raw("]")
    e2.out_bytes(cg.SEQ_RET)   # the trailing `ret` every program needs

    # part 3: padding so the file covers .rdata's raw data
    e3 = Emitter(pool)
    e3.cur = e2.cur
    e3.move(ZERO)
    e3.raw("." * (st.FILE_SIZE - st.SRC_RAW))

    return setup + e.text() + e2.text() + e3.text(), pool, setup_end


def main():
    bf, pool, setup_end = build_bf()
    out = os.path.join(ROOT, "bfsrc", "bfc0.bf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="\n") as f:
        f.write(bf + "\n")
    print("wrote %s (%d bytes of Brainfuck)" % (out, len(bf)))
    print("  pool cells : %d distinct byte values, tape cells 4..%d"
          % (len(pool), POOL0 + len(pool) - 1))
    print("  setup ends : cell %d" % setup_end)
    print("  prefix     : %d bytes of image emitted" % st.SRC_RAW)
    print("  padding    : %d zero bytes" % (st.FILE_SIZE - st.SRC_RAW))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
