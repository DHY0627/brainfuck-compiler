#!/usr/bin/env python3
"""Verify that bfsrc/bfc1.bf really is the compiler described by tools/bf2c.py.

Runs bfc1.bf in a pure-Python Brainfuck interpreter and compares its stdout
with tools/bf2c.py::translate() for a battery of inputs.  This catches logic
errors *before* bfc.exe is asked to turn the 28 KB program into an exe.

EOF semantics of ',' must match bfc.exe: reading past the end stores 0.
"""

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bf2c  # noqa: E402

BFC1 = os.path.join(ROOT, "bfsrc", "bfc1.bf")
EXAMPLES = os.path.join(ROOT, "examples")

OPS = {"+": 0, "-": 1, ">": 2, "<": 3, ".": 4, ",": 5, "[": 6, "]": 7}


def load(path):
    code = [OPS[chr(c)] for c in open(path, "rb").read() if chr(c) in OPS]
    jump = [0] * len(code)
    stack = []
    for i, op in enumerate(code):
        if op == 6:            # '['
            stack.append(i)
        elif op == 7:          # ']'
            j = stack.pop()
            jump[i] = j
            jump[j] = i
    assert not stack, "unbalanced brackets in %s" % path
    return code, jump


def run(code, jump, stdin_bytes, tape_size=30000):
    tape = bytearray(tape_size)
    out = bytearray()
    p = 0
    pc = 0
    n = len(stdin_bytes)
    ip = 0
    while pc < len(code):
        op = code[pc]
        if op == 0:                     # +
            tape[p] = (tape[p] + 1) & 255
        elif op == 1:                   # -
            tape[p] = (tape[p] - 1) & 255
        elif op == 2:                   # >
            p += 1
            if p >= tape_size:
                raise RuntimeError("tape overflow")
        elif op == 3:                   # <
            p -= 1
            if p < 0:
                raise RuntimeError("tape underflow")
        elif op == 4:                   # .
            out.append(tape[p])
        elif op == 5:                   # ,
            tape[p] = stdin_bytes[ip] if ip < n else 0
            ip += 1
            if ip > n:
                ip = n
        elif op == 6:                   # [
            if tape[p] == 0:
                pc = jump[pc]
        else:                           # ]
            if tape[p] != 0:
                pc = jump[pc]
        pc += 1
    return bytes(out)


def main():
    code, jump = load(BFC1)
    print("bfc1.bf: %d instructions, %d bytes" % (len(code), os.path.getsize(BFC1)))

    cases = []

    def add(name, data):
        cases.append((name, data))

    add("empty", b"")
    add("all-ops", b"+-<>.,[]")
    add("ops-plus-junk", b"hi + there\n- > < . , [ ] \t end")
    hello = open(os.path.join(EXAMPLES, "hello.bf"), "rb").read()
    add("hello.bf", hello)
    add("nested.bf", open(os.path.join(EXAMPLES, "nested.bf"), "rb").read())
    add("cat.bf", open(os.path.join(EXAMPLES, "cat.bf"), "rb").read())
    add("zeroing.bf", open(os.path.join(EXAMPLES, "zeroing.bf"), "rb").read())
    add("all-nonzero-bytes", bytes(range(1, 256)))
    rnd = random.Random(20261016)
    add("random-ascii", bytes(rnd.randrange(1, 128) for _ in range(200)))

    failed = 0
    for name, data in cases:
        want = bf2c.translate(data)
        got = run(code, jump, data)
        ok = got == want
        if not ok:
            failed += 1
        print("  [%s] %-20s %d source bytes -> %d bytes of C"
              % ("PASS" if ok else "FAIL", name, len(data), len(want)))
        if not ok:
            for i in range(min(len(got), len(want))):
                if got[i] != want[i]:
                    print("        first diff at byte %d: got %r want %r"
                          % (i, got[i:i + 40], want[i:i + 40]))
                    break
            else:
                print("        length differs: got %d want %d" % (len(got), len(want)))

    print()
    if failed:
        print("%d/%d FAILED" % (failed, len(cases)))
        return 1
    print("%d/%d cases match tools/bf2c.py exactly" % (len(cases), len(cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
