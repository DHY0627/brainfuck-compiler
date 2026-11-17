#!/usr/bin/env python3
"""End-to-end demo of the two-stage toolchain.

    stage 0 : bfc.exe                     (C++ compiler, emits PE32+ directly)
    stage 1 : bfc.exe bfsrc/t2bf.bf       -> bfsrc/t2bf.exe
              t2bf.bf is a COMPILER WRITTEN IN BRAINFUCK. bfc.exe turns it
              into a native x86-64 executable.
    stage 2 : t2bf.exe < text > out.bf    (the Brainfuck compiler produces
                                           Brainfuck)
    stage 3 : bfc.exe out.bf              -> out.exe
    stage 4 : out.exe                     prints the original text

Nothing in the chain uses an assembler, a linker, a C compiler or any
intermediate translation step: bfsrc/t2bf.bf is Brainfuck source, and both
executables are emitted as machine code by bfc.exe.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BFC = os.path.join(ROOT, "bfc.exe")
T2BF_BF = os.path.join(ROOT, "bfsrc", "t2bf.bf")
T2BF_EXE = os.path.join(ROOT, "bfsrc", "t2bf.exe")
OUT = os.path.join(ROOT, "tests", "out")

sys.path.insert(0, HERE)
from t2bf_model import compile_text  # noqa: E402


def step(msg):
    print("\n--- %s" % msg)


def main():
    os.makedirs(OUT, exist_ok=True)
    text = sys.argv[1].encode("utf-8") if len(sys.argv) > 1 else \
        "Brainfuck 编译器，用 Brainfuck 编写。".encode("utf-8")

    step("0. the C++ compiler")
    print("    %s" % BFC)

    step("1. compile the Brainfuck-written compiler into a native exe")
    print("    bfc.exe bfsrc\\t2bf.bf -o bfsrc\\t2bf.exe")
    subprocess.run([BFC, T2BF_BF, "-o", T2BF_EXE], check=True)
    print("    t2bf.bf size: %d bytes of pure Brainfuck"
          % len(open(T2BF_BF, "rb").read().strip()))

    step("2. the Brainfuck compiler compiles text -> Brainfuck")
    print("    input text: %r" % text.decode("utf-8", "replace"))
    gen = subprocess.run([T2BF_EXE], input=text, stdout=subprocess.PIPE,
                         check=True).stdout
    gen_bf = os.path.join(OUT, "demo_gen.bf")
    with open(gen_bf, "wb") as f:
        f.write(gen)
    print("    t2bf.exe emitted %d bytes of Brainfuck (head: %r)"
          % (len(gen), gen[:24]))
    print("    matches the independent Python model: %s"
          % (gen == compile_text(text).encode("ascii")))

    step("3. compile the generated Brainfuck")
    gen_exe = os.path.join(OUT, "demo_gen.exe")
    subprocess.run([BFC, gen_bf, "-o", gen_exe], check=True)

    step("4. run it")
    got = subprocess.run([gen_exe], stdout=subprocess.PIPE, check=True).stdout
    print("    output: %r" % got.decode("utf-8", "replace"))
    print("    round trip ok: %s" % (got == text))
    return 0 if got == text else 1


if __name__ == "__main__":
    raise SystemExit(main())
