#!/usr/bin/env python3
"""Reference model of what bfsrc/t2bf.bf must do.

Independent of the Brainfuck source: if the compiled t2bf.exe and this model
disagree on any input, one of them is wrong.
"""


def compile_text(data, stop_at_nul=True):
    """Tiny -> Brainfuck.

    Every source byte b becomes:
        '+' * b  +  '.'  +  '[-]'
    i.e. set the current cell to b, print it, clear it. A 0 byte ends the
    source stream (that is also what EOF produces), so it is never emitted.
    """
    out = []
    for b in data:
        if stop_at_nul and b == 0:
            break
        out.append("+" * b)
        out.append(".")
        out.append("[-]")
    return "".join(out)


if __name__ == "__main__":
    import sys
    data = sys.stdin.buffer.read()
    sys.stdout.write(compile_text(data))
