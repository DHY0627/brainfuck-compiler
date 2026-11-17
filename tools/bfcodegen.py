#!/usr/bin/env python3
"""The code-generation contract for the Brainfuck-written compiler (bfc0).

bfc0 compiles a Brainfuck program into x86-64 machine code that runs inside the
stage-0 image. The contract:

    rbx  = data tape pointer   (the stage-0 stub loads it with 0x140001500)
    rsp  = 16-byte aligned on entry to the compiled code
    tape = 30000 bytes inside .text, zero-filled by the loader

Every instruction sequence below is made of CONSTANTS ONLY. That is what makes
the compiler expressible in Brainfuck: there is no address arithmetic, no
backpatching and no relocation -- absolute addressing is used for everything
outside the code, and the image is loaded at a fixed base (no ASLR).

    +  inc byte [rbx]                  FE 03
    -  dec byte [rbx]                  FE 0B
    >  inc rbx                         48 FF C3
    <  dec rbx                         48 FF CB
    .  WriteFile(stdout, rbx, 1)       (68 bytes)
    ,  ReadFile(stdin, iobuf, 1)       (96 bytes), EOF stores 0
    end  ret                           C3

Loops are NOT part of this contract: `[` and `]` compile to `ud2` (0F 0B) so
that a program using them fails loudly instead of computing a wrong answer.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bootstrap"))
import mkstage0 as st  # noqa: E402

IAT_EXIT = st.IAT_VA[0]
IAT_GETSTD = st.IAT_VA[1]
IAT_WRITE = st.IAT_VA[2]
IAT_READ = st.IAT_VA[3]
WRITTEN = st.WRITTEN_VA
IOBUF = st.IOBUF_VA


def b(*parts):
    """Flatten ints and bytes into a byte string."""
    out = bytearray()
    for p in parts:
        if isinstance(p, int):
            out.append(p)
        else:
            out += p
    return bytes(out)


def imm64(v):
    return struct.pack("<Q", v)


def imm32(v):
    return struct.pack("<I", v)


# --- sub rsp,0x28 ; <body> ; add rsp,0x28 ----------------------------------
def framed(body):
    return b(0x48, 0x83, 0xEC, 0x28, body, 0x48, 0x83, 0xC4, 0x28)


SEQ_OUT = framed(b(
    0xB9, imm32(0xFFFFFFF5),          # mov ecx, -11      STD_OUTPUT_HANDLE
    0x48, 0xA1, imm64(IAT_GETSTD),    # mov rax, [GetStdHandle]
    0xFF, 0xD0,                       # call rax
    0x48, 0x89, 0xC1,                 # mov rcx, rax
    0x48, 0x89, 0xDA,                 # mov rdx, rbx      lpBuffer = &cell
    0x41, 0xB8, imm32(1),             # mov r8d, 1
    0x49, 0xB9, imm64(WRITTEN),       # mov r9, written
    0x48, 0xC7, 0x44, 0x24, 0x20, imm32(0),   # mov qword [rsp+0x20], 0
    0x48, 0xA1, imm64(IAT_WRITE),     # mov rax, [WriteFile]
    0xFF, 0xD0,                       # call rax
))

SEQ_IN = framed(b(
    0xB9, imm32(0xFFFFFFF6),          # mov ecx, -10      STD_INPUT_HANDLE
    0x48, 0xA1, imm64(IAT_GETSTD),
    0xFF, 0xD0,
    0x48, 0x89, 0xC1,                 # mov rcx, rax
    0x48, 0xBA, imm64(IOBUF),         # mov rdx, iobuf
    0x41, 0xB8, imm32(1),             # mov r8d, 1
    0x49, 0xB9, imm64(WRITTEN),       # mov r9, written
    0x48, 0xC7, 0x44, 0x24, 0x20, imm32(0),
    0x48, 0xA1, imm64(IAT_READ),      # mov rax, [ReadFile]
    0xFF, 0xD0,
    0x31, 0xC0,                       # xor eax, eax      default 0 (EOF)
    0x48, 0xBA, imm64(WRITTEN),       # mov rdx, written
    0x83, 0x3A, 0x01,                 # cmp dword [rdx], 1
    0x75, 0x0C,                       # jne +12  -> store
    0x48, 0xBA, imm64(IOBUF),         # mov rdx, iobuf
    0x8A, 0x02,                       # mov al, [rdx]
    0x88, 0x03,                       # store: mov [rbx], al
))

SEQ_UD2 = b(0x0F, 0x0B)               # ud2: unsupported instruction
SEQ_RET = b(0xC3)                     # ret: back to the stage-0 stub

OPS = {
    ord('+'): b(0xFE, 0x03),
    ord('-'): b(0xFE, 0x0B),
    ord('>'): b(0x48, 0xFF, 0xC3),
    ord('<'): b(0x48, 0xFF, 0xCB),
    ord('.'): SEQ_OUT,
    ord(','): SEQ_IN,
    ord('['): SEQ_UD2,
    ord(']'): SEQ_UD2,
}


def compile_code(source):
    """Reference model: BF source -> x86-64 chunk stream."""
    out = bytearray()
    for ch in source:
        seq = OPS.get(ord(ch) if isinstance(ch, str) else ch)
        if seq is not None:
            out += seq
    out += SEQ_RET
    return bytes(out)


def build_exe(source):
    """Reference model: BF source -> complete PE32+ image.

    Matches the layout bfc0.bf emits: the constant image prefix, then the
    compiled code, then zero padding to cover .rdata's raw data. The result is
    a superset of the minimum image size, which is valid: the loader only
    needs every section's raw range to be present in the file.
    """
    code = compile_code(source)
    prefix = st.build(b"\xc3")[:st.SRC_RAW]
    return prefix + code + b"\x00" * (st.FILE_SIZE - st.SRC_RAW)


if __name__ == "__main__":
    for k in "+-<>.,[]":
        print("  %s  %3d bytes  %s" % (k, len(OPS[ord(k)]), OPS[ord(k)].hex(' ')))
    print("  ret %3d bytes" % len(SEQ_RET))
    print("total per-op bytes: %d" % (sum(len(v) for v in OPS.values()) + 1))
