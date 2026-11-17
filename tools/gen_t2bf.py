#!/usr/bin/env python3
"""Emit bfsrc/t2bf.bf.

t2bf.bf is *hand-designed* Brainfuck: the design below is the program. This
script exists only so the '+' run lengths are counted exactly instead of being
typed by eye -- a single miscounted '+' silently produces a wrong character.

Tape layout used by t2bf.bf:

    cell 0 : c      current source byte
    cell 1 : 43     '+'
    cell 2 : 46     '.'
    cell 3 : 91     '['
    cell 4 : 45     '-'
    cell 5 : 93     ']'

Program logic:

    set cells 1..5 to the five constants, walk back to cell 0
    ,                    read first source byte
    [                    while c != 0
      >.<-               emit '+' c times (c counts down to 0)
      >>.>.>.>.          emit '.', '[', '-', ']'  from cells 2,3,4,5
      <<<<<              back to c
      ,                  read next source byte
    ]

which compiles the source text into a Brainfuck program of the shape

    +++++...+.  [-]+...+.  [-]   ...   (one block per source byte)
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (cell value, what it means) for cells 1..5, in the order they are set
CONSTANTS = [(43, "'+'"), (46, "'.'"), (91, "'['"), (45, "'-'"), (93, "']'")]


def build():
    """Return (setup, body). setup fills cells 1..5; body is the main loop."""
    setup = []
    for value, _name in CONSTANTS:
        setup.append(">")
        setup.append("+" * value)
    setup.append("<<<<<")      # back to cell 0
    body = [
        ",",          # read first source byte
        "[",          # outer loop: while c != 0
        "[",          #   inner loop: emit '+' c times
        ">.<-",
        "]",
        ">>.>.>.>.",  #   emit '.', '[', '-', ']' from cells 2,3,4,5
        "<<<<<",      #   back to cell 0
        ",",          #   read next source byte
        "]",          # end outer loop
    ]
    return "".join(setup), "".join(body)


def main():
    setup, body = build()
    src = setup + body
    out = os.path.join(ROOT, "bfsrc", "t2bf.bf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="\n") as f:
        f.write(src + "\n")
    print("wrote %s (%d bytes)" % (out, len(src)))
    print("  constants : %s" % ", ".join("%d=%s" % c for c in CONSTANTS))
    print("  body      : %s" % body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
