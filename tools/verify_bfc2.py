"""Verify bfsrc/bfc2.bf against the Python reference model (tools/pak.py).

Runs the Brainfuck compiler in an independent interpreter and compares the
produced executable bytes with `pak.compile_source` for a range of inputs.
"""

import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pak      # noqa: E402
import bfrun    # noqa: E402

BFC2 = os.path.join(ROOT, "bfsrc", "bfc2.bf")
EXAMPLES = os.path.join(ROOT, "examples")
SYSTEST = os.path.join(ROOT, "awib", "awib-master", "systest")
AWIB_B = os.path.join(ROOT, "awib", "awib.b")


def cases():
    out = [("empty", b"")]
    out.append(("all-ops", b"++--><.,[]"))
    out.append(("junk", b"x y z\n\t 0 1 2 3"))
    out.append(("only-junk", b"no instructions here"))
    for name in ("hello", "nested", "cat", "zeroing", "bfc0_hello"):
        path = os.path.join(EXAMPLES, name + ".bf")
        if os.path.exists(path):
            with open(path, "rb") as f:
                out.append((name, f.read()))
    out.append(("all-swapped", bytes(range(1, 256))))
    rnd = random.Random(20261117)
    out.append(("random-ascii",
                bytes(rnd.randrange(1, 128) for _ in range(300))))
    return out


def main(argv):
    if not os.path.exists(BFC2):
        print("FAIL: %s missing (run tools/gen_bfc2.py)" % BFC2)
        return 1
    with open(BFC2, "rb") as f:
        prog = f.read()

    t = time.time()
    machine = bfrun.BFRun(prog)
    print("bfc2.bf: %d bytes -> %d compressed ops (%.1fs)"
          % (len(prog), len(machine.code), time.time() - t))

    todo = cases()
    if "--large" in argv:
        awib = os.path.join(SYSTEST, "factor.b")
        if os.path.exists(awib):
            with open(awib, "rb") as f:
                todo.append(("systest-factor", f.read()))
        if os.path.exists(AWIB_B) and "--awib" in argv:
            with open(AWIB_B, "rb") as f:
                todo.append(("awib", f.read()))

    checks = 0
    failed = 0
    for name, source in todo:
        t = time.time()
        got = machine.run(source)
        dt = time.time() - t
        want = pak.compile_source(source)
        checks += 1
        if got != want:
            failed += 1
            # first difference is the useful part of the report
            pos = next((i for i in range(min(len(got), len(want)))
                        if got[i] != want[i]), min(len(got), len(want)))
            print("FAIL %-16s got %d bytes, want %d, first diff at %d"
                  % (name, len(got), len(want), pos))
        else:
            print("ok   %-16s %7d bytes in %6.2fs" % (name, len(got), dt))
    print("\n%d checks, %d failed" % (checks, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
