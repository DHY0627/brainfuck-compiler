#!/usr/bin/env python3
"""Generate bootstrap/mkstage0.py.

Stage 0 turns a raw x86-64 *chunk stream* into a runnable PE32+ console exe.
Everything about the image is a constant except three instruction
displacements, which are constants too -- they do not depend on the chunk
stream at all, because the stub always copies a FIXED number of bytes.

Image contract:

    e_lfanew     0x80
    entry point  RVA 0x1000            (the stub)
    ImageBase    0x140000000           (no ASLR: DllCharacteristics = 0)
    .text        RVA 0x1000 raw 0x200  vsize 0x1000  rawsize 0x1000  RWX
                   0x1000  the stub
                   0x1040  where the chunk stream is copied to and executed
                   0x1500  the data tape used by compiled programs
    .rdata       RVA 0x2000 raw 0x1200 vsize 0x1000  rawsize 0x0A00
                   0x2000  import descriptor
                   0x2038  IAT  (kernel32!ExitProcess)
                   0x2100  the chunk stream as staged (source of the copy)

    entry stub (49 bytes, at RVA 0x1000):
        48 BF 40 10 00 00        mov   rdi, 0x1040        ; destination
        48 8D 35 <disp32>        lea   rsi, [rip+disp]    ; source (.rdata)
        B9 00 08 00 00           mov   ecx, 0x800         ; fixed byte count
        F3 A4                    rep   movsb
        48 BB <imm64>            mov   rbx, 0x140001500  ; data tape
        E8 <disp32>              call  0x1040            ; run the program
        48 83 EC 28              sub   rsp, 0x28         ; align + shadow
        0F B6 0B                 movzx ecx, byte [rbx]  ; exit code = tape[0]
        FF 15 <disp32>           call  qword [rip+disp]  ; ExitProcess(rc)
        CC                       int3

    The compiled program is expected to end with `ret` (0xC3); trailing zeros
    in the copied region are never executed.

Lesson learned the hard way: `lea esi, [rip+disp]` writes a 32-bit register,
and a32-bit write ZERO-EXTENDS into rsi, so the full 64-bit image address
(0x140002100) got truncated to 0x2100 and the copy read address 0x40002100.
The stub therefore uses the 64-bit forms (rdi/rsi/rbx).
"""
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

IMAGE_BASE = 0x140000000
TEXT_RAW = 0x200
RDATA_RAW = 0x1200
STUB_RVA = 0x1000
DST_RVA = 0x1040
# The data tape must sit ABOVE the copied code region (0x1040 + COPY_LEN), or
# a large compiled program overwrites its own tape. 0x1040 + 0x800 = 0x1840.
TAPE_RVA = 0x1900
TEXT_VSIZE = 0x9000              # the tape needs 30000 bytes (0x7530)
RDATA_RVA = 0xA000               # must not collide with .text's virtual range
SIZE_OF_IMAGE = RDATA_RVA + 0x1000


# ---- .rdata: import table + scratch slots ----------------------------------
# Four imports, in this order (the IAT slot order matters to the code we emit):
#   0 ExitProcess   1 GetStdHandle   2 WriteFile   3 ReadFile
IMPORTS = ["ExitProcess", "GetStdHandle", "WriteFile", "ReadFile"]
RDATA_RVA = 0xA000
DESC_RVA = RDATA_RVA + 0x00
ILT_RVA = RDATA_RVA + 0x28
IAT_RVA = RDATA_RVA + 0x50
HINT_RVA = RDATA_RVA + 0x78          # 4 x 16 bytes
DLL_RVA = RDATA_RVA + 0xB8
WRITTEN_RVA = RDATA_RVA + 0xC8       # DWORD: bytes written / read
IOBUF_RVA = RDATA_RVA + 0xCC         # 1 byte: ReadFile landing spot
SRC_RVA = RDATA_RVA + 0x100          # staged compiled code
SRC_RAW = RDATA_RAW + (SRC_RVA - RDATA_RVA)
COPY_LEN = 0x800
RDATA_RAW_SIZE = 0xA00

assert DST_RVA + COPY_LEN <= TAPE_RVA, "code region must not overlap the tape"
assert TAPE_RVA + 30000 <= RDATA_RVA, "tape must not overlap .rdata"

# absolute addresses the emitted code uses
IAT_VA = [IMAGE_BASE + IAT_RVA + 8 * i for i in range(len(IMPORTS))]
WRITTEN_VA = IMAGE_BASE + WRITTEN_RVA
IOBUF_VA = IMAGE_BASE + IOBUF_RVA


def hx(b):
    return b.hex()


def wrap(h, width=64):
    return "\n".join('    "%s"' % h[i:i + width] for i in range(0, len(h), width))


# ---- DOS header -------------------------------------------------------------
dos = bytearray(64)
struct.pack_into("<H", dos, 0x00, 0x5A4D)
struct.pack_into("<H", dos, 0x02, 0x0090)
struct.pack_into("<H", dos, 0x04, 3)
struct.pack_into("<H", dos, 0x06, 4)
struct.pack_into("<H", dos, 0x08, 0xFFFF)
struct.pack_into("<H", dos, 0x0C, 0xB8)
struct.pack_into("<H", dos, 0x14, 0x40)
struct.pack_into("<I", dos, 0x3C, 0x80)
assert len(dos) == 64

# ---- DOS stub ---------------------------------------------------------------
msg = b"This program cannot be run in DOS mode.\r\r\n$"
dosstub = (bytes([0x0E, 0x1F, 0xBA, 0x0E, 0x00, 0xB4, 0x09, 0xCD, 0x21,
                  0xB8, 0x01, 0x4C, 0xCD, 0x21]) + msg)
dosstub += b"\x00" * (64 - len(dosstub))
assert len(dosstub) == 64

# ---- PE signature + COFF header --------------------------------------------
coff = b"PE\x00\x00" + struct.pack("<HHIIIHH", 0x8664, 2, 0, 0, 0, 240, 0x0022)
assert len(coff) == 24

# ---- optional header --------------------------------------------------------
opt = bytearray(240)
struct.pack_into("<H", opt, 0, 0x020B)
opt[2] = 1
struct.pack_into("<I", opt, 4, 0x1000)          # SizeOfCode
struct.pack_into("<I", opt, 8, RDATA_RAW_SIZE)  # SizeOfInitializedData
struct.pack_into("<I", opt, 16, STUB_RVA)       # AddressOfEntryPoint
struct.pack_into("<I", opt, 20, 0x1000)         # BaseOfCode
struct.pack_into("<Q", opt, 24, IMAGE_BASE)
struct.pack_into("<I", opt, 32, 0x1000)
struct.pack_into("<I", opt, 36, 0x200)
struct.pack_into("<H", opt, 40, 6)
struct.pack_into("<H", opt, 48, 6)
struct.pack_into("<I", opt, 56, SIZE_OF_IMAGE)  # SizeOfImage
struct.pack_into("<I", opt, 60, 0x200)          # SizeOfHeaders
struct.pack_into("<H", opt, 68, 3)              # Subsystem = console
struct.pack_into("<I", opt, 72, 0x100000)
struct.pack_into("<I", opt, 80, 0x1000)
struct.pack_into("<I", opt, 88, 0x100000)
struct.pack_into("<I", opt, 96, 0x1000)
struct.pack_into("<I", opt, 112 + 8 * 1, DESC_RVA)
struct.pack_into("<I", opt, 112 + 8 * 1 + 4, 40)
struct.pack_into("<I", opt, 112 + 8 * 12, IAT_RVA)
struct.pack_into("<I", opt, 112 + 8 * 12 + 4, 8 * (len(IMPORTS) + 1))
struct.pack_into("<I", opt, 108, 16)
assert len(opt) == 240

# ---- .rdata bytes -----------------------------------------------------------
rdata = bytearray(0x1000)
struct.pack_into("<IIIII", rdata, 0, ILT_RVA, 0, 0, DLL_RVA, IAT_RVA)
for i, name in enumerate(IMPORTS):
    entry = HINT_RVA + 16 * i
    struct.pack_into("<Q", rdata, (ILT_RVA - RDATA_RVA) + 8 * i, entry)
    struct.pack_into("<Q", rdata, (IAT_RVA - RDATA_RVA) + 8 * i, entry)
    struct.pack_into("<H", rdata, (entry - RDATA_RVA), 0)
    nm = name.encode() + b"\x00"
    rdata[(entry - RDATA_RVA) + 2:(entry - RDATA_RVA) + 2 + len(nm)] = nm
rdata[DLL_RVA - RDATA_RVA:DLL_RVA - RDATA_RVA + 13] = b"KERNEL32.dll\x00"
assert HINT_RVA + 16 * len(IMPORTS) <= DLL_RVA
assert DLL_RVA + 13 <= WRITTEN_RVA

# ---- section table ---------------------------------------------------------
secs = (b".text\x00\x00\x00"
        + struct.pack("<IIIIIIII", TEXT_VSIZE, STUB_RVA, 0x1000, TEXT_RAW,
                      0, 0, 0, 0xE0000020)
        + b".rdata\x00\x00"
        + struct.pack("<IIIIIIII", 0x1000, RDATA_RVA, RDATA_RAW_SIZE, RDATA_RAW,
                      0, 0, 0, 0xC0000040))
assert len(secs) == 80

# ---- the entry stub --------------------------------------------------------
# NOTE: `mov rdi, imm32` would sign-extend, i.e. load 0x0000000000001040
# instead of the image address 0x0000000140001040. Every 64-bit constant must
# use the movabs form (REX.W + B8+r + imm64).
stub = bytearray()
stub += b"\x48\xBF" + struct.pack("<Q", IMAGE_BASE + DST_RVA)      # 0x00 mov rdi
stub += b"\x48\x8D\x35" + struct.pack("<i", SRC_RVA - (STUB_RVA + 0x0A + 7))
stub += b"\xB9" + struct.pack("<I", COPY_LEN)                      # 0x11 mov ecx
stub += b"\xF3\xA4"                                                # 0x16 rep movsb
stub += b"\x48\xBB" + struct.pack("<Q", IMAGE_BASE + TAPE_RVA)     # 0x18 mov rbx
stub += b"\xE8" + struct.pack("<i", DST_RVA - (STUB_RVA + 0x22 + 5))
stub += b"\x48\x83\xEC\x28"                                        # 0x27 sub rsp
stub += b"\x0F\xB6\x0B"                                            # 0x2B movzx
stub += b"\xFF\x15" + struct.pack("<i", IAT_RVA - (STUB_RVA + 0x2E + 6))
stub += b"\xCC"                                                    # 0x34 int3
assert len(stub) == 53, len(stub)
assert DST_RVA > STUB_RVA + len(stub)

TEMPLATE = '''#!/usr/bin/env python3
"""Stage-0 assembler: x86-64 chunk stream -> PE32+ console exe.

GENERATED by tools/gen_mkstage0.py -- edit the generator, not this file.

The image is entirely constant; the chunk stream only supplies the bytes that
get copied to RVA 0x1040 and executed. See the generator for the layout.
"""
import struct
import sys

DOS = bytes.fromhex(
{dos}
)
DOSSTUB = bytes.fromhex(
{dosstub}
)
COFF = bytes.fromhex(
{coff}
)
OPT = bytes.fromhex(
{opt}
)
SECTIONS = bytes.fromhex(
{secs}
)
RDATA = bytes.fromhex(
{rdata}
)
STUB = bytes.fromhex(
{stub}
)

IMAGE_BASE = {image_base:#x}
TEXT_RAW = {text_raw:#x}
RDATA_RAW = {rdata_raw:#x}
STUB_RVA = {stub_rva:#x}
DST_RVA = {dst_rva:#x}
TAPE_RVA = {tape_rva:#x}
SRC_RVA = {src_rva:#x}
SRC_RAW = {src_raw:#x}
COPY_LEN = {copy_len:#x}
RDATA_RAW_SIZE = {rdata_raw_size:#x}
FILE_SIZE = {file_size:#x}

# absolute addresses the emitted x86-64 code refers to
IAT_VA = {iat_va}
WRITTEN_VA = {written_va:#x}
IOBUF_VA = {iobuf_va:#x}

assert len(DOS) == 64
assert len(DOSSTUB) == 64
assert len(COFF) == 24
assert len(OPT) == 240
assert len(SECTIONS) == 80
assert len(RDATA) == 0x1000
assert len(STUB) == 53


def build(program):
    """program: the x86-64 chunk stream (at most COPY_LEN bytes)."""
    n = len(program)
    if n > COPY_LEN:
        raise ValueError("chunk stream too large: %d > %d" % (n, COPY_LEN))
    out = bytearray(FILE_SIZE)
    out[0x00:0x40] = DOS
    out[0x40:0x80] = DOSSTUB
    out[0x80:0x98] = COFF
    out[0x98:0x188] = OPT
    out[0x188:0x1D8] = SECTIONS
    out[TEXT_RAW:TEXT_RAW + len(STUB)] = STUB
    out[RDATA_RAW:RDATA_RAW + len(RDATA)] = RDATA
    out[SRC_RAW:SRC_RAW + n] = program
    return bytes(out)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    program = open(src, "rb").read()
    img = build(program)
    with open(dst, "wb") as f:
        f.write(img)
    print("mkstage0: %s (%d bytes of code) -> %s (%d bytes)"
          % (src, len(program), dst, len(img)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

out = TEMPLATE.format(
    dos=wrap(hx(dos)), dosstub=wrap(hx(dosstub)), coff=wrap(hx(coff)),
    opt=wrap(hx(opt)), secs=wrap(hx(secs)), rdata=wrap(hx(rdata)),
    stub=wrap(hx(stub)),
    image_base=IMAGE_BASE, text_raw=TEXT_RAW, rdata_raw=RDATA_RAW,
    stub_rva=STUB_RVA, dst_rva=DST_RVA, tape_rva=TAPE_RVA,
    src_raw=SRC_RAW, src_rva=SRC_RVA, copy_len=COPY_LEN,
    rdata_raw_size=RDATA_RAW_SIZE,
    iat_va=IAT_VA, written_va=WRITTEN_VA, iobuf_va=IOBUF_VA,
    file_size=RDATA_RAW + RDATA_RAW_SIZE)

path = os.path.join(ROOT, "bootstrap", "mkstage0.py")
with open(path, "w", newline="\n") as f:
    f.write(out)
print("wrote %s (%d bytes)" % (path, len(out)))
print("stub = %d bytes, copy_len = 0x%X, src raw 0x%X, file 0x%X"
      % (len(stub), COPY_LEN, SRC_RAW, RDATA_RAW + RDATA_RAW_SIZE))
