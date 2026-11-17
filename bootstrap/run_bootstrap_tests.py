#!/usr/bin/env python3
"""End-to-end tests for the bootstrap chain.

    bfc.exe bfsrc/bfc0.bf -o bfsrc/bfc0.exe      (C++ compiler builds stage 2)
    bfsrc/bfc0.exe < prog.bf > prog.exe          (stage 2 compiles BF -> exe)

Every produced exe is validated with the independent PE parser and then run,
comparing raw stdout and the process exit code against a reference BF
interpreter.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BFC = os.path.join(ROOT, "bfc.exe")
BFC0_BF = os.path.join(ROOT, "bfsrc", "bfc0.bf")
BFC0_EXE = os.path.join(ROOT, "bfsrc", "bfc0.exe")
OUT = os.path.join(HERE, "out")
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pecheck              # noqa: E402
import bfcodegen as cg      # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -- " + str(detail)) if (detail and not cond) else ""))


def reference(src, stdin=b""):
    """Reference BF interpreter (tape of 30000 bytes, wrapping cells)."""
    code = [c for c in src if c in b"+-<>.,[]"]
    jump, stack = {}, []
    for i, c in enumerate(code):
        if c == ord("["):
            stack.append(i)
        elif c == ord("]"):
            j = stack.pop()
            jump[i] = j
            jump[j] = i
    tape = bytearray(30000)
    p = ip = ii = 0
    out = bytearray()
    while ip < len(code):
        c = code[ip]
        if c == ord("+"):
            tape[p] = (tape[p] + 1) & 255
        elif c == ord("-"):
            tape[p] = (tape[p] - 1) & 255
        elif c == ord(">"):
            p += 1
        elif c == ord("<"):
            p -= 1
        elif c == ord("."):
            out.append(tape[p])
        elif c == ord(","):
            tape[p] = stdin[ii] if ii < len(stdin) else 0
            ii += 1
        elif c == ord("["):
            if tape[p] == 0:
                ip = jump[ip]
        elif c == ord("]"):
            if tape[p] != 0:
                ip = jump[ip]
        ip += 1
    # The stage-0 stub exits with the byte at the *current* cell, which is
    # exactly the observable result of a loop-free program.
    return bytes(out), tape[p]


CASES = [
    ("empty", b"", b""),
    ("print A", b"+" * 65 + b".", b""),
    ("print Hi", b"+" * 72 + b"." + b"+" * 33 + b".", b""),
    ("two cells", b"+" * 66 + b">" + b"+" * 67 + b"<.>.", b""),
    ("run of cells", b"+>++>+++>++++>+++++<<<<.>.>.>.>.", b""),
    ("comments ignored", b"a comment! " + b"+" * 90 + b".", b""),
    ("echo one byte", b",.", b"Q"),
    ("echo three", b",.,.,.", b"xyz"),
    ("EOF gives 0", b",.", b""),
    ("decrement wraps", b"-" * 3 + b".", b""),
]


def main():
    os.makedirs(OUT, exist_ok=True)

    print("=== stage 2: bfc.exe builds the Brainfuck-written compiler ===")
    r = subprocess.run([BFC, BFC0_BF, "-o", BFC0_EXE],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    check("bfc0.bf compiles with bfc.exe", r.returncode == 0,
          r.stderr.decode("utf-8", "replace"))
    check("bfc0.exe exists and is a PE",
          os.path.exists(BFC0_EXE) and open(BFC0_EXE, "rb").read(2) == b"MZ")
    pe = pecheck.parse(BFC0_EXE)
    check("bfc0.exe passes the independent PE checks", not pe["errors"],
          "; ".join(pe["errors"]))

    print("=== stage 3: bfc0.exe compiles Brainfuck into executables ===")
    for name, src, stdin in CASES:
        exe = os.path.join(OUT, "bfc0_%s.exe" % name.replace(" ", "_"))
        r = subprocess.run([BFC0_EXE], input=src, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        produced = r.stdout
        check("[%s] bfc0 emitted an executable" % name,
              r.returncode == 0 and produced[:2] == b"MZ" and len(produced) > 7000,
              "rc=%d len=%d" % (r.returncode, len(produced)))
        if produced[:2] != b"MZ":
            continue
        pe = pecheck.parse_bytes(produced)
        check("[%s] produced image is a valid PE32+" % name, not pe["errors"],
              "; ".join(pe["errors"]))
        want_out, want_rc = reference(src, stdin)
        with open(exe, "wb") as f:
            f.write(produced)
        got = subprocess.run([exe], input=stdin, stdout=subprocess.PIPE)
        check("[%s] runs, stdout matches the interpreter" % name,
              got.stdout == want_out,
              "got %r want %r" % (got.stdout[:24], want_out[:24]))
        check("[%s] exit code is the first tape cell" % name,
              got.returncode == want_rc,
              "got %d want %d" % (got.returncode, want_rc))

    print("=== bytes are identical to the Python model ===")
    for name, src, _stdin in CASES:
        r = subprocess.run([BFC0_EXE], input=src, stdout=subprocess.PIPE)
        check("[%s] output == model" % name, r.stdout == cg.build_exe(src),
              "%d vs %d bytes" % (len(r.stdout), len(cg.build_exe(src))))

    print("=== large programs (regression: code must not overlap the tape) ===")
    # 1341 bytes of machine code; an earlier memory map put the tape at 0x1500
    # so this program overwrote its own code and crashed.
    big = b"+" * 72 + b"." + b"+" * 101 + b"." + b"+" * 108 + b"." + b"+" * 108 + b"." + b"+" * 111 + b"."
    check("large case produces > 0x4C0 bytes of code",
          len(cg.compile_code(big)) > 0x4C0, len(cg.compile_code(big)))
    exe_big = os.path.join(OUT, "bfc0_big.exe")
    r = subprocess.run([BFC0_EXE], input=big, stdout=subprocess.PIPE)
    with open(exe_big, "wb") as f:
        f.write(r.stdout)
    check("large case: valid PE", not pecheck.parse_bytes(r.stdout)["errors"])
    ref_out, _ref_rc = reference(big)
    got_big = subprocess.run([exe_big], stdout=subprocess.PIPE)
    check("large case: runs without crashing", got_big.returncode < 0x80000000,
          hex(got_big.returncode))
    check("large case: stdout matches the interpreter", got_big.stdout == ref_out,
          "got %r want %r" % (got_big.stdout[:16], ref_out[:16]))

    failed = [n for n, c in results if not c]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    for n in failed:
        print("  FAILED: %s" % n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
