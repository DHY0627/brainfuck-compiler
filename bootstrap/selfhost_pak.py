"""Bootstrap chain for the gcc-free PAK compiler (bfsrc/bfc2.bf).

    bfc.exe (native seed, written in C++)
        -> bfsrc/bfc2_seed.exe        the BF compiler translated to native code
        -> bfsrc/bfc2.exe             stage2: BF compiler *emitting PE bytes*
        -> stage3.exe                 stage2 compiling itself

stage2 and stage3 must be byte-identical (bootstrap fixed point).  Every
executable from stage2 onwards is produced by a Brainfuck program writing the
complete .exe byte stream to stdout -- no assembler, linker, C compiler or any
other external tool is involved.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import pak  # noqa: E402

BFC = os.path.join(ROOT, "bfc.exe")
BFC2_BF = os.path.join(ROOT, "bfsrc", "bfc2.bf")
BFC2_SEED = os.path.join(ROOT, "bfsrc", "bfc2_seed.exe")
BFC2 = os.path.join(ROOT, "bfsrc", "bfc2.exe")
OUT = os.path.join(ROOT, "bootstrap", "out-pak")
EXAMPLES = os.path.join(ROOT, "examples")
AWIB_B = os.path.join(ROOT, "awib", "awib.b")
AWIB_EXE = os.path.join(ROOT, "awib", "awib.exe")

checks = 0
failed = 0


def check(cond, label):
    global checks, failed
    checks += 1
    if not cond:
        failed += 1
        print("FAIL:", label)


def run(exe, stdin_bytes):
    t = time.time()
    p = subprocess.run([exe], input=stdin_bytes, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    return p.returncode, p.stdout, p.stderr, time.time() - t


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(BFC):
        print("FAIL: bfc.exe missing")
        return 1
    with open(BFC2_BF, "rb") as f:
        bfc2_src = f.read()

    # ---- 1. native seed translates the BF compiler -----------------------
    t = time.time()
    p = subprocess.run([BFC, BFC2_BF, "-o", BFC2_SEED], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    dt = time.time() - t
    check(p.returncode == 0, "seed compiled bfc2.bf (rc=%d)" % p.returncode)
    check(os.path.exists(BFC2_SEED), "bfc2_seed.exe exists")
    print("seed:   bfc.exe -> bfc2_seed.exe in %.3fs (%d bytes)"
          % (dt, os.path.getsize(BFC2_SEED) if os.path.exists(BFC2_SEED) else 0))

    # ---- 2. stage2: the BF compiler emits its own PE image ---------------
    rc, stage2, err, dt = run(BFC2_SEED, bfc2_src)
    check(rc == 0, "stage2 run rc=%d" % rc)
    check(err == b"", "stage2 stderr empty")
    model = pak.compile_source(bfc2_src)
    check(stage2 == model, "stage2 == python model (%d bytes)" % len(stage2))
    check(stage2[:2] == b"MZ", "stage2 is a PE image")
    write(BFC2, stage2)
    print("stage2: bfc2_seed.exe < bfc2.bf -> bfc2.exe in %.3fs (%d bytes)"
          % (dt, len(stage2)))

    # ---- 3. the PAK compiler compiles itself -----------------------------
    rc, stage3, err, dt = run(BFC2, bfc2_src)
    check(rc == 0, "stage3 run rc=%d" % rc)
    check(err == b"", "stage3 stderr empty")
    check(stage3 == stage2, "fixed point: stage3 == stage2")
    write(os.path.join(OUT, "stage3.exe"), stage3)
    print("stage3: bfc2.exe < bfc2.bf -> %d bytes in %.3fs, identical=%s"
          % (len(stage3), dt, stage3 == stage2))

    # ---- 4. programs compiled by the BF compiler behave like bfc's -------
    for name in ("hello", "nested", "cat", "zeroing"):
        src = os.path.join(EXAMPLES, name + ".bf")
        if not os.path.exists(src):
            continue
        with open(src, "rb") as f:
            source = f.read()
        stdin_bytes = b"Hello, cat!\r\n\x00tail" if name == "cat" else b""
        rc, exe, err, dt = run(BFC2, source)
        check(rc == 0 and err == b"", "%s: bfc2 produced exe" % name)
        check(exe == pak.compile_source(source), "%s: bytes match model" % name)
        path = os.path.join(OUT, name + ".pak.exe")
        write(path, exe)
        n_rc, n_out, _, _ = run(os.path.join(EXAMPLES, name + ".exe"),
                                stdin_bytes)
        p_rc, p_out, _, _ = run(path, stdin_bytes)
        check(n_out == p_out, "%s: stdout matches bfc-built exe" % name)
        if name != "bfc0_hello":
            check(n_rc == p_rc, "%s: exit code matches" % name)
        print("  program %-8s %6d bytes  out=%dB  %.2fs" % (name, len(exe),
                                                            len(p_out), dt))

    # ---- 5. awib compiled by the BF-written compiler ---------------------
    if os.path.exists(AWIB_B) and os.path.exists(AWIB_EXE):
        with open(AWIB_B, "rb") as f:
            awib_src = f.read()
        rc, awib_pak, err, dt = run(BFC2, awib_src)
        check(rc == 0 and err == b"", "awib: bfc2 produced exe")
        check(awib_pak == pak.compile_source(awib_src),
              "awib: bytes match model (%d)" % len(awib_pak))
        path = os.path.join(OUT, "awib.pak.exe")
        write(path, awib_pak)
        print("awib:   bfc2.exe < awib.b -> %d bytes in %.2fs" % (len(awib_pak), dt))
        body = b"@lang_c\n" + open(os.path.join(
            ROOT, "awib", "awib-master", "systest", "factor.b"), "rb").read()
        a_rc, a_out, _, a_t = run(AWIB_EXE, body)
        b_rc, b_out, _, b_t = run(path, body)
        check(a_out == b_out, "awib(factor): output matches native awib")
        check(a_rc == b_rc, "awib(factor): exit code matches")
        print("  awib_pak.exe on factor: %d bytes, native %.3fs, pak %.3fs"
              % (len(b_out), a_t, b_t))

    print("\n%d checks, %d failed" % (checks, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
