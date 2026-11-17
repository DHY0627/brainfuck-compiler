#!/usr/bin/env python3
"""Self-hosting chain for bfc1 (Brainfuck -> C written in Brainfuck).

    1. seed      bfc.exe (C++ compiler, emits native exe)  compiles bfc1.bf
                 -> bfc1.exe  (a native BF->C compiler, made from Brainfuck)
    2. bfc1.exe < bfc1.bf  -> stage1.c        (the compiler compiles itself)
    3. gcc stage1.c        -> stage1.exe      (native compiler built from C)
    4. stage1.exe < bfc1.bf -> stage2.c       (the rebuilt compiler compiles
                                               itself again)
    5. stage2.c must be byte-identical to stage1.c   <- the bootstrap fixed point
    6. stage1.exe must compile ordinary programs whose C output is correct

Nothing in the chain is a C-written Brainfuck compiler: step 1 is our own
bfc.exe, which is a seed (every bootstrap has one), and steps 2-5 only ever
run BF-written code plus gcc's C compiler.

All output is ASCII so it survives any console code page.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)
import bf2c  # noqa: E402

BFC = os.path.join(ROOT, "bfc.exe")
BFC1_BF = os.path.join(ROOT, "bfsrc", "bfc1.bf")
BFC1_EXE = os.path.join(ROOT, "bfsrc", "bfc1.exe")
EXAMPLES = os.path.join(ROOT, "examples")
OUT = os.path.join(HERE, "out-selfhost")

GCC = shutil.which("gcc")

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -- " + detail) if (detail and not cond) else ""))


def run(cmd, stdin=None, cwd=None):
    return subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=cwd)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def cc(src, exe, opt="-O1"):
    p = run([GCC, opt, "-o", exe, src])
    if p.returncode != 0:
        print(p.stderr.decode("utf-8", "replace")[-2000:])
    return p.returncode == 0


def main():
    if not GCC:
        print("FATAL: gcc not found on PATH")
        return 2
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    src = read(BFC1_BF)
    print("bfc1.bf: %d bytes" % len(src))

    # ---- 1. seed compile -------------------------------------------------
    print("- stage 0: bfc.exe compiles bfc1.bf")
    p = run([BFC, BFC1_BF, "-o", BFC1_EXE])
    check("bfc.exe compiles bfc1.bf", p.returncode == 0 and os.path.exists(BFC1_EXE),
          (p.stderr or p.stdout).decode("utf-8", "replace"))
    if not os.path.exists(BFC1_EXE):
        return 1
    check("bfc1.exe size", os.path.getsize(BFC1_EXE) > 10000,
          str(os.path.getsize(BFC1_EXE)))

    # ---- 2. the compiler compiles itself --------------------------------
    print("- stage 1: bfc1.exe compiles bfc1.bf -> stage1.c")
    stage1_c = os.path.join(OUT, "stage1.c")
    p = run([BFC1_EXE], stdin=src)
    write(stage1_c, p.stdout)
    check("bfc1.exe exit code", p.returncode == 0, "rc=%d" % p.returncode)
    want = bf2c.translate(src)
    check("stage1.c == reference model", p.stdout == want,
          "%d bytes, want %d" % (len(p.stdout), len(want)))

    # ---- 3. gcc turns that C into a native compiler ---------------------
    print("- stage 2: gcc stage1.c -> stage1.exe")
    stage1_exe = os.path.join(OUT, "stage1.exe")
    check("gcc builds stage1.exe", cc(stage1_c, stage1_exe),
          "")
    if not os.path.exists(stage1_exe):
        return 1

    # ---- 4/5. the rebuilt compiler compiles itself: fixed point ---------
    print("- stage 3: stage1.exe compiles bfc1.bf -> stage2.c")
    stage2_c = os.path.join(OUT, "stage2.c")
    p = run([stage1_exe], stdin=src)
    write(stage2_c, p.stdout)
    check("stage1.exe exit code", p.returncode == 0, "rc=%d" % p.returncode)
    check("FIXED POINT: stage2.c == stage1.c", p.stdout == read(stage1_c),
          "%d vs %d bytes" % (len(p.stdout), os.path.getsize(stage1_c)))

    # ---- 6. ordinary programs through the rebuilt compiler ---------------
    print("- stage 4: rebuilt compiler compiles example programs")
    for name in ("hello", "nested", "cat", "zeroing"):
        bf = os.path.join(EXAMPLES, name + ".bf")
        cfile = os.path.join(OUT, name + ".c")
        exe = os.path.join(OUT, name + ".exe")
        data = read(bf)
        p1 = run([BFC1_EXE], stdin=data)
        p2 = run([stage1_exe], stdin=data)
        check("%s: two compilers emit identical C" % name, p1.stdout == p2.stdout,
              "%d vs %d" % (len(p1.stdout), len(p2.stdout)))
        check("%s: matches reference model" % name, p2.stdout == bf2c.translate(data), "")
        write(cfile, p2.stdout)
        if cc(cfile, exe):
            stdin = b"abc\n" if name == "cat" else b""
            r = run([exe], stdin=stdin)
            # reference output comes from bfc.exe's own exe for the same source
            ref = run([os.path.join(EXAMPLES, name + ".exe")], stdin=stdin)
            check("%s: runs and matches bfc's output" % name, r.stdout == ref.stdout,
                  "got %r want %r" % (r.stdout[:80], ref.stdout[:80]))
        else:
            check("%s: gcc builds exe" % name, False, "")

    # ---- summary --------------------------------------------------------
    failed = [r for r in results if not r[1]]
    print()
    print("%d checks, %d failed" % (len(results), len(failed)))
    if failed:
        for name, _ok, detail in failed:
            print("  FAILED: %s %s" % (name, detail))
        return 1
    print("SELF-HOSTING OK: bfc1.exe was built from Brainfuck by bfc.exe;")
    print("               gcc built stage1.exe from the C that bfc1.exe emitted;")
    print("               stage1.exe reproduces byte-identical C for bfc1.bf.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
