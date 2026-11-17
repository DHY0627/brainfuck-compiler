#!/usr/bin/env python3
"""Reference model: Brainfuck -> C (single self-contained C file).

This is the SPECIFICATION of the exact bytes that `bfc1.bf` must emit.
`tools/gen_bfc1.py` embeds these very strings into the Brainfuck program, and
`tools/verify_bfc1.py` / `bootstrap/selfhost.py` compare the real compiler
against `translate()` byte for byte.

Why the target is C and not machine code (this is the whole self-hosting trick):

  * the compiler writes its output straight to the console -- a stream cannot
    be rewound, so forward jump addresses can never be patched afterwards;
  * BF loops are *structured*, so `[` and `]` map to `while (...){` and `}`
    with no address arithmetic at all;
  * therefore the BF program never needs a lookahead pass, never needs to
    buffer generated code, and never needs multi-byte offsets: it can emit
    output one input byte at a time, in a fixed number of tape cells.
  * an external C compiler (gcc) supplies the machine code and the addresses.

Every BF instruction maps to a fixed piece of text, so the translation is
deterministic -- which is what makes the bootstrap fixed point exact.
"""

import sys

TAPE = 70000

PRELUDE = (
    "#include <stdio.h>\n"
    "#ifdef _WIN32\n"
    "#include <io.h>\n"
    "#include <fcntl.h>\n"
    "#endif\n"
    f"static unsigned char m[{TAPE}];\n"
    "int main(void){int p=0;\n"
    "#ifdef _WIN32\n"
    "_setmode(_fileno(stdout),O_BINARY);\n"
    "#endif\n"
    "setvbuf(stdout,0,_IONBF,0);\n"
)

# one fixed fragment per BF instruction, in the canonical order
KEYS = "+-><.,[]"

TEMPLATES = {
    "+": " m[p]++;\n",
    "-": " m[p]--;\n",
    ">": " p++;\n",
    "<": " p--;\n",
    ".": " putchar(m[p]);\n",
    # bfc.exe writes 0 on EOF for ','; the generated C does the same
    ",": " {int ch=getchar();m[p]=ch<0?0:ch;}\n",
    "[": " while(m[p]){\n",
    "]": " }\n",
}

EPILOGUE = " return 0;}\n"


def translate(data: bytes) -> bytes:
    """Exact byte sequence bfc1 must write for BF source `data`."""
    out = bytearray(PRELUDE.encode("ascii"))
    table = {ord(k): v.encode("ascii") for k, v in TEMPLATES.items()}
    for b in data:
        frag = table.get(b)
        if frag is not None:
            out += frag
    out += EPILOGUE.encode("ascii")
    return bytes(out)


if __name__ == "__main__":
    src = open(sys.argv[1], "rb").read() if len(sys.argv) > 1 \
        else sys.stdin.buffer.read()
    sys.stdout.buffer.write(translate(src))
