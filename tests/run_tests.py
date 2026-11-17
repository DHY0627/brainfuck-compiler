#!/usr/bin/env python3
"""End-to-end tests for bfc.

For every case:
  1. compile the .bf source with bfc.exe
  2. verify the emitted exe with the independent PE parser (tests/pecheck.py)
  3. run the exe, compare raw stdout bytes and the process exit code

Extra cases prove the "no intermediate step" claim: bfc is run with a stripped
environment (no PATH), so it cannot possibly be shelling out to a C compiler,
assembler or linker, and the resulting exe still has to work.

All messages are ASCII so they survive any Windows console code page.
"""
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BFC = os.path.join(ROOT, "bfc.exe")
OUT = os.path.join(HERE, "out")
EXAMPLES = os.path.join(ROOT, "examples")

sys.path.insert(0, HERE)
import pecheck  # noqa: E402

results = []


def run(cmd, stdin=None, env=None, cwd=None):
    p = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -- " + detail) if (detail and not cond) else ""))


def compile_bf(src_path, exe_path, extra=None, env=None):
    cmd = [BFC, src_path, "-o", exe_path] + (extra or [])
    return run(cmd, env=env)


def case(name, src_path, stdin_bytes, want_stdout, want_exit=0,
         extra=None, env=None, check_pe=True):
    exe = os.path.join(OUT, name + ".exe")
    print("- case: %s" % name)
    rc, out, err = compile_bf(src_path, exe, extra, env)
    check(name + ": compile rc==0", rc == 0, (err or out).decode("utf-8", "replace"))
    if rc != 0:
        return
    check(name + ": exe created", os.path.exists(exe))

    if check_pe:
        r = pecheck.parse(exe)
        check(name + ": PE valid", not r["errors"], "; ".join(r["errors"]))
        names = [n for _dll, ns in r.get("imports", []) for n in ns]
        check(name + ": imports resolved",
              r.get("imports") and r["imports"][0][0].lower() == "kernel32.dll"
              and set(names) == {"GetStdHandle", "WriteFile", "ReadFile"},
              str(r.get("imports")))
        check(name + ": entry in .text",
              r["sections"][0]["rva"] <= r["entry"] <
              r["sections"][0]["rva"] + r["sections"][0]["vsize"])

    rc, out, err = run([exe], stdin=stdin_bytes)
    check(name + ": stdout matches (%d bytes)" % len(want_stdout),
          out == want_stdout,
          "got %r... want %r..." % (out[:40], want_stdout[:40]))
    check(name + ": exit code == %d" % want_exit, rc == want_exit, "got %d" % rc)


def write(path, data):
    """Write bytes verbatim; str is always encoded as UTF-8 (never the console
    code page, which is what the default text mode would use on Windows)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    return path


# ---------------------------------------------------------------- reference BF
#
# A dead-simple interpreter used as ground truth for differential testing.
# Returns (stdout, ok); ok=False marks undefined behaviour (pointer escaped the
# tape) or a program that needs more steps than the budget, in which case the
# comparison is skipped rather than reported as a failure.

def reference(src, stdin=b"", cells=30000, max_steps=2000000):
    code = [c for c in src if c in "+-<>.,[]"]
    jump = {}
    stack = []
    for i, c in enumerate(code):
        if c == "[":
            stack.append(i)
        elif c == "]":
            if not stack:
                return b"", False
            j = stack.pop()
            jump[i] = j
            jump[j] = i
    if stack:
        return b"", False
    tape = [0] * cells
    p = ip = 0
    out = bytearray()
    steps = 0
    while ip < len(code):
        steps += 1
        if steps > max_steps:
            return bytes(out), False
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
            tape[p] = stdin[0] if stdin else 0
            stdin = stdin[1:]
        elif c == "[":
            if tape[p] == 0:
                ip = jump[ip]
        elif c == "]":
            if tape[p] != 0:
                ip = jump[ip]
        if p < 0 or p >= cells:
            return bytes(out), False
        ip += 1
    return bytes(out), True


CURATED = [
    "++++++++[>++++++++<-]>+.",
    "++++++++++[>+++++++>++++++++++>+++>+<<<<-]>++.>+.+++++++..+++.>++.<<"
    "+++++++++++++++.>.+++.------.--------.>+.>.",
    "++++[>++++[>++++<-]<-]>>+.",
    "++++++++++[-]++++++++++[>+++++++++<-]>.",
    "+++++[+]++++++++++[>+++++++++<-]>.",
    ",.,.,.,.,.",
    ",[.,]",
    "+++++[->+++++<]>[->+++++<]>.",
    ">>>+<<<[->>>+<<<]>.",
    "++++++++[->++++++++<]>[->+++++++++++++<]>-.+.",
    "+++[>+++[>+++[>+++<-]<-]<-]>>>.",
    "-[--->+<]>.",
    "++++++++++[>+>+++>+++++++<<<-]>>>.",
]


def gen_random(rng, n):
    """Random program with balanced brackets AND balanced pointer moves.

    '<' is only emitted when some earlier '>' is still unmatched, so the data
    pointer can never go below its start; with n <= 80 the pointer also cannot
    reach the end of the 30000-cell tape. Everything the generator produces is
    therefore defined behaviour, which makes skips purely about non-termination.
    """
    out = []
    depth = 0      # unmatched '['
    ptr = 0        # unmatched '>'
    for _ in range(n):
        choices = "+-.,"
        if depth == 0:
            # Pointer moves only outside loops: a loop body that runs many
            # times would otherwise walk the pointer off the tape dynamically
            # even though the source looks balanced.
            choices += ">"
            if ptr > 0:
                choices += "<"
        if depth < 3:
            choices += "["
        if depth > 0:
            choices += "]"
        c = rng.choice(choices)
        out.append(c)
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == ">":
            ptr += 1
        elif c == "<":
            ptr -= 1
        elif c == "+" and depth > 0 and rng.random() < 0.35:
            out.append("-")   # makes loop termination much likelier
    out.extend("]" * depth)
    out.extend("<" * ptr)
    return "".join(out)


def differential(src, tag, stdin=b"", max_steps=300000):
    want, ok = reference(src, stdin, max_steps=max_steps)
    if not ok:
        return None
    path = write(os.path.join(OUT, "diff_%s.bf" % tag), src)
    exe = os.path.join(OUT, "diff_%s.exe" % tag)
    rc, out, err = compile_bf(path, exe)
    if rc != 0:
        check("diff %s: compile" % tag, False, err.decode("utf-8", "replace"))
        return False
    got = run([exe], stdin=stdin)[1]
    check("diff %s: matches interpreter" % tag, got == want,
          "src=%r got=%r want=%r" % (src, got[:40], want[:40]))
    return got == want


def main():
    if not os.path.exists(BFC):
        print("bfc.exe not found; build it first")
        return 2
    os.makedirs(OUT, exist_ok=True)

    print("=== basic programs ===")
    case("hello", os.path.join(EXAMPLES, "hello.bf"), b"", b"Hello World!\n")
    case("nested", os.path.join(EXAMPLES, "nested.bf"), b"", b"A")
    case("zeroing", os.path.join(EXAMPLES, "zeroing.bf"), b"", b"Z")

    print("=== source robustness (UTF-8 BOM + CRLF + comments) ===")
    bom = write(os.path.join(OUT, "bom.bf"),
                "\ufeff+++[>+++<-]>." "\r\n" "  注释里有汉字，也有中文字符串  \r\n").encode("utf-8")
    case("bom_crlf", bom, b"", bytes([9]))

    print("=== input ===")
    payload = b"Hello, Brainfuck!\n"
    case("cat", os.path.join(EXAMPLES, "cat.bf"), payload, payload)
    case("cat_eof", os.path.join(EXAMPLES, "cat.bf"), b"", b"")          # EOF -> 0 -> loop ends
    case("cat_nul", os.path.join(EXAMPLES, "cat.bf"), b"AB\x00CD", b"AB")  # 0 terminates cat

    print("=== output buffering (>4096 bytes forces flushes) ===")
    many = os.path.join(OUT, "many.bf")
    write(many, "+" * 70 + ">" + "+" * 100 + ">" + "+" * 65 + "<<" +
          "[" + ">[-]" + "+" * 100 + "[>.<-]" + "<-]")
    case("many", many, b"", b"A" * 7000)

    print("=== large machine code (forces second RVA page) ===")
    big = os.path.join(OUT, "bigcode.bf")
    src = "+-" * 4000 + "+" * 65 + "."
    write(big, src)
    exe = os.path.join(OUT, "bigcode.exe")
    rc, out, err = compile_bf(big, exe)
    check("bigcode: compile rc==0", rc == 0, (err or out).decode("utf-8", "replace"))
    r = pecheck.parse(exe)
    check("bigcode: PE valid", not r["errors"], "; ".join(r["errors"]))
    check("bigcode: .text spans >4KB",
          r["sections"][0]["vsize"] > 0x1000, str(r["sections"][0]["vsize"]))
    check("bigcode: .data RVA pushed past .text",
          r["sections"][1]["rva"] >= r["sections"][0]["rva"] + 0x2000,
          "data rva 0x%X" % r["sections"][1]["rva"])
    rc, out, err = run([exe])
    check("bigcode: stdout == 'A'", out == b"A", repr(out))
    check("bigcode: exit code == 0", rc == 0, "got %d" % rc)

    print("=== no toolchain involved (stripped environment) ===")
    env = {"PATH": "", "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
    case("notool", os.path.join(EXAMPLES, "hello.bf"), b"", b"Hello World!\n",
         env=env)

    print("=== diagnostics ===")
    bad1 = write(os.path.join(OUT, "bad_open.bf"), "+++[>+++<-")
    rc, out, err = compile_bf(bad1, os.path.join(OUT, "bad_open.exe"))
    check("unmatched '[' rejected", rc != 0 and b"unmatched" in err, err.decode("utf-8", "replace"))
    bad2 = write(os.path.join(OUT, "bad_close.bf"), "+++]")
    rc, out, err = compile_bf(bad2, os.path.join(OUT, "bad_close.exe"))
    check("unmatched ']' rejected", rc != 0 and b"unmatched" in err, err.decode("utf-8", "replace"))
    rc, out, err = compile_bf(os.path.join(OUT, "no_such_file.bf"),
                              os.path.join(OUT, "x.exe"))
    check("missing input rejected", rc != 0, str(rc))

    print("=== determinism ===")
    a = os.path.join(OUT, "det_a.exe")
    b = os.path.join(OUT, "det_b.exe")
    compile_bf(os.path.join(EXAMPLES, "hello.bf"), a)
    compile_bf(os.path.join(EXAMPLES, "hello.bf"), b)
    check("byte-identical rebuild", open(a, "rb").read() == open(b, "rb").read())

    print("=== differential vs reference interpreter ===")
    compared = 0
    for i, src in enumerate(CURATED):
        if differential(src, "c%02d" % i, b"brainfuck") is not None:
            compared += 1
    rng = random.Random(20240607)
    skipped = 0
    attempts = 0
    target = 50
    while compared < target and attempts < 400:
        src = gen_random(rng, rng.randint(10, 80))
        attempts += 1
        if differential(src, "r%03d" % attempts, b"AB", max_steps=50000) is None:
            skipped += 1
        else:
            compared += 1
    check("differential coverage: %d programs matched (%d attempts, %d skipped as "
          "non-terminating/UB)" % (compared, attempts, skipped),
          compared >= target)

    print("=== a compiler written in Brainfuck (bfsrc/t2bf.bf) ===")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from t2bf_model import compile_text

    t2bf_src = os.path.join(ROOT, "bfsrc", "t2bf.bf")
    t2bf_exe = os.path.join(OUT, "t2bf.exe")
    check("t2bf.bf exists", os.path.exists(t2bf_src))
    rc, out, err = compile_bf(t2bf_src, t2bf_exe)
    check("t2bf.bf compiles with bfc.exe", rc == 0,
          (err or out).decode("utf-8", "replace"))
    r = pecheck.parse(t2bf_exe)
    check("t2bf.exe is a valid PE", not r["errors"], "; ".join(r["errors"]))

    def t2bf(data, tag, stage2=False):
        """Run the Brainfuck-written compiler; optionally compile+run its output."""
        got = run([t2bf_exe], stdin=data)[1]
        want = compile_text(data).encode("ascii")
        check("t2bf[%s]: emitted BF matches reference model" % tag, got == want,
              "got %d bytes, want %d bytes" % (len(got), len(want)))
        if not stage2 or got != want:
            return
        bf_path = write(os.path.join(OUT, "t2_%s.bf" % tag), got)
        exe = os.path.join(OUT, "t2_%s.exe" % tag)
        rc, out, err = compile_bf(bf_path, exe)
        check("t2bf[%s]: emitted BF compiles with bfc.exe" % tag, rc == 0,
              (err or out).decode("utf-8", "replace"))
        rc, out, err = run([exe])
        check("t2bf[%s]: twice-compiled exe reproduces the source text" % tag,
              out == data and rc == 0,
              "got %r want %r (rc=%d)" % (out[:40], data[:40], rc))

    t2bf(b"", "empty", stage2=True)
    t2bf(b"Hi!", "hi", stage2=True)
    t2bf(b"Hello, Brainfuck!\n", "hello", stage2=True)
    t2bf("你好，世界 —— Brainfuck 写的编译器".encode("utf-8"), "utf8", stage2=True)
    t2bf(bytes(range(1, 256)), "allbytes")
    t2bf(b"The quick brown fox jumps over the lazy dog. " * 50, "long", stage2=True)
    # a 0 byte ends the source stream, exactly like EOF does
    t2bf(b"AB\x00CD", "nul")

    failed = [n for n, c, _ in results if not c]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    for n in failed:
        print("  FAILED: %s" % n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
