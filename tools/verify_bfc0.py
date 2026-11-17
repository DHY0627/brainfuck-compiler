#!/usr/bin/env python3
"""Simulate bfsrc/bfc0.bf in Python and compare it with the reference model.

This catches logic errors in the generated Brainfuck before bfc.exe ever sees
it, which matters a lot because Brainfuck is not readable.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bfcodegen as cg  # noqa: E402


def run_bf(program, stdin=b"", cells=30000, max_steps=200_000_000):
    """A compact Brainfuck interpreter with precomputed bracket jumps."""
    code = [c for c in program if c in "+-<>.,[]"]
    jump = {}
    stack = []
    for i, c in enumerate(code):
        if c == "[":
            stack.append(i)
        elif c == "]":
            j = stack.pop()
            jump[i] = j
            jump[j] = i
    assert not stack, "unbalanced brackets in the generated program"
    tape = bytearray(cells)
    p = 0
    ip = 0
    out = bytearray()
    inp = stdin
    ii = 0
    n = len(code)
    steps = 0
    while ip < n:
        c = code[ip]
        if c == "+":
            tape[p] = (tape[p] + 1) & 255
        elif c == "-":
            tape[p] = (tape[p] - 1) & 255
        elif c == ">":
            p += 1
        elif c == "<":
            p -= 1
        elif c == ".":
            out.append(tape[p])
        elif c == ",":
            tape[p] = inp[ii] if ii < len(inp) else 0
            ii += 1
        elif c == "[":
            if tape[p] == 0:
                ip = jump[ip]
        elif c == "]":
            if tape[p] != 0:
                ip = jump[ip]
        ip += 1
        steps += 1
        if steps > max_steps:
            raise RuntimeError("step limit exceeded")
    return bytes(out), p, steps


def main():
    bf = open(os.path.join(ROOT, "bfsrc", "bfc0.bf")).read()
    cases = [
        b"",
        b"+",
        b"+" * 65 + b".",
        b"+" * 72 + b"." + b"+" * 33 + b".",
        b"+++>+++<.>.",
        b",.",
        b",.,.,.",
        b"+-<>.,",
        b"comment text ignored " + b"+" * 66 + b".",
    ]
    ok = True
    for src in cases:
        want = cg.build_exe(src)
        try:
            got, p, steps = run_bf(bf, src)
        except RuntimeError as e:
            print("FAIL %-30r %s" % (src[:24], e))
            ok = False
            continue
        good = got == want
        ok &= good
        print("%s %-30r -> %d bytes, %d steps, tape cell %d"
              % ("OK  " if good else "FAIL", src[:24], len(got), steps, p))
        if not good:
            for i in range(min(len(got), len(want))):
                if got[i] != want[i]:
                    print("      first difference at 0x%X: got %02X want %02X"
                          % (i, got[i], want[i]))
                    break
            print("      got len %d want len %d" % (len(got), len(want)))
    print("all cases match:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
