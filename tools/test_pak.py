"""End-to-end tests for the PAK backend (tools/pak.py).

Every program is compiled three ways and compared byte for byte:

  1. tools/pak.py       -- reference model (PE + records)
  2. the PAK executable the model produced, executed
  3. the bfc.exe-built native executable, executed

stdout bytes, exit codes and (for `,`) stdin handling must all agree.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pak  # noqa: E402

OUT = os.path.join(ROOT, "out_pak")
BFC = os.path.join(ROOT, "bfc.exe")
EXAMPLES = os.path.join(ROOT, "examples")

CASES = [
    ("hello", b"", True),
    ("zeroing", b"", True),
    ("nested", b"", True),
    ("cat", b"Hello, cat!\r\n\x00tail", True),
    # examples/bfc0_hello.exe was produced by the BF-written stage-0 compiler,
    # whose stub exits with the tape cell value; compare stdout only.
    ("bfc0_hello", b"", False),
]


def build(name, source):
    exe = pak.compile_source(source)
    path = os.path.join(OUT, name + ".pak.exe")
    with open(path, "wb") as f:
        f.write(exe)
    return exe, path


def run(path, stdin_bytes, timeout=30):
    p = subprocess.run([path], input=stdin_bytes,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(BFC):
        print("FAIL: bfc.exe missing")
        return 1
    checks = 0
    failed = 0

    def check(cond, label):
        nonlocal checks, failed
        checks += 1
        if not cond:
            failed += 1
            print("FAIL:", label)

    for name, stdin_bytes, compare_exit in CASES:
        src_path = os.path.join(EXAMPLES, name + ".bf")
        with open(src_path, "rb") as f:
            source = f.read()
        exe, path = build(name, source)

        # the model output must itself be a well-formed file and stable
        check(exe[:2] == b"MZ", "%s: MZ" % name)
        check(exe.find(b"PE\x00\x00") == 0x80, "%s: PE offset" % name)

        # reference bfc executable
        native = os.path.join(EXAMPLES, name + ".exe")
        if not os.path.exists(native):
            rc = subprocess.run([BFC, src_path, "-o", native]).returncode
            check(rc == 0, "%s: bfc built" % name)
        b_rc, b_out, _ = run(native, stdin_bytes)
        p_rc, p_out, p_err = run(path, stdin_bytes)
        if compare_exit:
            check(b_rc == p_rc, "%s: exit code %d != %d" % (name, p_rc, b_rc))
        check(b_out == p_out, "%s: stdout differs (%d != %d bytes)"
              % (name, len(p_out), len(b_out)))
        check(p_err == b"", "%s: stderr %r" % (name, p_err[:100]))
        print("ok %-12s exit=%d out=%dB" % (name, p_rc, len(p_out)))

    # garbage bytes between instructions must be ignored
    exe_a = pak.compile_source(b"+++ comment +++")
    exe_b = pak.compile_source(b"++++++")
    check(exe_a == exe_b, "non-instruction bytes are ignored")
    # empty program is a valid executable
    exe = pak.compile_source(b"")
    path = os.path.join(OUT, "empty.pak.exe")
    with open(path, "wb") as f:
        f.write(exe)
    rc, out, err = run(path, b"")
    check(rc == 0 and out == b"" and err == b"", "empty program")

    # --- payloads the launcher must reject instead of running garbage -------
    # The stub validates the record stream before entering it, so a broken or
    # oversized payload exits with a specific code instead of hanging.
    def rejects(name, source, want_code):
        exe = pak.compile_source(source)
        path = os.path.join(OUT, name + ".pak.exe")
        with open(path, "wb") as f:
            f.write(exe)
        rc, out, err = run(path, b"", timeout=20)
        check(rc == want_code, "%s: exit code %d != %d" % (name, rc, want_code))
        check(out == b"", "%s: no output before failing" % name)
        print("ok %-14s exit=%d (rejected)" % (name, rc))

    rejects("unmatched_close", b"+++]", 4)
    rejects("unmatched_open", b"+++[", 5)
    rejects("too_deep", b"[" * 5000 + b"]" * 5000, 3)
    rejects("too_big", b"+" * 400000, 2)

    print("\n%d checks, %d failed" % (checks, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
