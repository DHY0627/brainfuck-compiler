"""Large-program test for the PAK backend.

Compiles the real awib Brainfuck compiler (48535 bytes of BF source) with the
PAK model and checks that its output is byte-identical to the bfc.exe-built
awib.exe.  This exercises a ~390 KB record stream, the runtime relocation pass
and thousands of loops.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pak  # noqa: E402

OUT = os.path.join(ROOT, "out_pak")
AWIB_B = os.path.join(ROOT, "awib", "awib.b")
AWIB_EXE = os.path.join(ROOT, "awib", "awib.exe")
SYSTEST = os.path.join(ROOT, "awib", "awib-master", "systest")


def run(exe, data):
    t = time.time()
    p = subprocess.run([exe], input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    return p.returncode, p.stdout, p.stderr, time.time() - t


def main():
    if not (os.path.exists(AWIB_B) and os.path.exists(AWIB_EXE)):
        print("SKIP: awib artifacts missing")
        return 0
    os.makedirs(OUT, exist_ok=True)

    with open(AWIB_B, "rb") as f:
        awib_src = f.read()
    t = time.time()
    exe = pak.compile_source(awib_src)
    build_s = time.time() - t
    path = os.path.join(OUT, "awib_pak.exe")
    with open(path, "wb") as f:
        f.write(exe)
    records = len(exe) - len(pak.PREFIX) - len(pak.REC_END)
    print("awib_pak.exe: %d bytes total, %d bytes of records, model %.2fs"
          % (len(exe), records, build_s))

    cases = []
    for name in ("basicops", "factor", "memoryhog"):
        src = os.path.join(SYSTEST, name + ".b")
        if not os.path.exists(src):
            continue
        with open(src, "rb") as f:
            body = f.read()
        cases.append(("@lang_c " + name, b"@lang_c\n" + body))
        cases.append(("@386_linux " + name, b"@386_linux\n" + body))

    checks = 0
    failed = 0
    for label, data in cases:
        a_rc, a_out, a_err, a_t = run(AWIB_EXE, data)
        p_rc, p_out, p_err, p_t = run(path, data)
        checks += 1
        if a_rc != p_rc or a_out != p_out or p_err != b"":
            failed += 1
            print("FAIL %-22s rc %d/%d out %d/%d err=%r"
                  % (label, p_rc, a_rc, len(p_out), len(a_out), p_err[:120]))
        else:
            print("ok   %-22s out=%-7dB  native %.3fs  pak %.3fs"
                  % (label, len(p_out), a_t, p_t))
    print("\n%d checks, %d failed" % (checks, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
