"""Reference Brainfuck interpreter used by the bfc2 verifiers.

Compressed run-length representation (like bfc's own optimizer) so the unrolled
constants in generated programs stay cheap to interpret.
"""


class BFRun:
    def __init__(self, source, tape_cells=30000):
        code = []
        for ch in source:
            c = chr(ch) if isinstance(ch, int) else ch
            if c in "+-<>":
                if code and code[-1][0] == c:
                    code[-1][1] += 1
                else:
                    code.append([c, 1])
            elif c in ".,[]":
                code.append([c, 0])
        self.code = code
        self.tape_cells = tape_cells
        # matching bracket indices in the compressed stream
        self.match = [-1] * len(code)
        stack = []
        for i, (op, _) in enumerate(code):
            if op == "[":
                stack.append(i)
            elif op == "]":
                j = stack.pop()
                self.match[i] = j
                self.match[j] = i

    def run(self, stdin, tape_cells=None):
        cells = tape_cells or self.tape_cells
        tape = bytearray(cells)
        p = 0
        sp = self.code
        match = self.match
        out = bytearray()
        inp = memoryview(stdin)
        i = 0
        stdin_pos = 0
        n = len(sp)
        while i < n:
            op, cnt = sp[i]
            if op == "+":
                tape[p] = (tape[p] + cnt) & 0xFF
            elif op == "-":
                tape[p] = (tape[p] - cnt) & 0xFF
            elif op == ">":
                p += cnt
                if p >= cells:
                    raise IndexError("tape overflow at +%d cells" % p)
            elif op == "<":
                p -= cnt
                if p < 0:
                    raise IndexError("tape underflow at %d" % p)
            elif op == ".":
                out.append(tape[p])
            elif op == ",":
                if stdin_pos < len(inp):
                    tape[p] = inp[stdin_pos]
                    stdin_pos += 1
                else:
                    tape[p] = 0
            elif op == "[":
                if tape[p] == 0:
                    i = match[i]
            elif op == "]":
                if tape[p] != 0:
                    i = match[i]
            i += 1
        return bytes(out)
