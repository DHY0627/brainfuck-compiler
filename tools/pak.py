"""PAK format -- no external toolchain bootstrap.

A PAK executable is a normal PE32+ console program whose *content* is split into
two parts:

  * a fixed 0x800-byte prefix  = DOS/PE headers + import table + a hand-written
    loader stub (the only machine code that is a constant);
  * a variable-length record stream placed in the file *overlay*, after the
    prefix.  The stub reads its own file, relocates forward loop jumps, sets up
    the machine registers expected by the record payloads and jumps into the
    stream.

The record stream is produced one record at a time, sequentially, by a Brainfuck
program: every record has a fixed byte layout whose length is a compile-time
constant of the BF instruction it comes from.  No byte address, file offset or
jump displacement has to be known while emitting, which is what makes it
possible to write the emitter *in Brainfuck*: nothing is ever patched at
compile time, only at run time by the stub.

Record layout (all records):

    [len]  [type]  [payload ...]  EB 02

`len` is the total record length including itself; `EB 02` is `jmp +2`, which
skips the next record's 2-byte header so payloads execute back to back.  The
loop records are 13 bytes: `cmp byte [rbx],0` followed by a `je rel32` (open)
or `jne rel32` (close) whose displacement is left as zero and patched by the
stub.

Register contract inside the stream:
    rbx = tape pointer       r13 = stdout      r14 = stdin
    rbp = &written           r15 = &bytebuf    rsi = &readbuf
    rdi = &readcount

This module is the reference model: the C++ seed compiler and the Brainfuck
compiler must both emit exactly the bytes produced here.
"""

import struct

IMAGE_BASE = 0x140000000

FILE_ALIGN = 0x200
SECT_ALIGN = 0x1000

HEADERS_SIZE = 0x200

RDATA_RVA = 0x1000
RDATA_SIZE = 0x200
RDATA_RAW = 0x200

TEXT_RVA = 0x2000
TEXT_SIZE = 0x400
TEXT_VSIZE = 0x2FE000
TEXT_RAW = 0x400

REC_OFF = 0x800                       # file offset of the record stream

# scratch memory inside the writable .text virtual area
DEST_RVA = TEXT_RVA + 0x4000          # file image is read here (2 MiB max)
READ_LIMIT = 0x200000
TAPE_RVA = 0x280000
TAPE_CELLS = 30000
PATHBUF_RVA = 0x290000
WRITTEN_RVA = 0x290200
BYTEBUF_RVA = 0x290208
READBUF_RVA = 0x290210
READCNT_RVA = 0x290220
PATCHSTACK_RVA = 0x287600
PATCHSTACK_SIZE = 0x8000               # 4096 x 8-byte entries
SIZE_OF_IMAGE = 0x300000

# record type codes
T_ADD = 1
T_SUB = 2
T_RIGHT = 3
T_LEFT = 4
T_OUT = 5
T_IN = 6
T_OPEN = 7
T_CLOSE = 8
T_END = 9

IMPORTS = [
    b"GetModuleFileNameA",
    b"CreateFileA",
    b"ReadFile",
    b"GetStdHandle",
    b"WriteFile",
    b"ExitProcess",
]
DLL_NAME = b"KERNEL32.dll"

# import table layout inside .rdata (fixed so the addresses are constants)
_DESC_OFF = 0x00
_ILT_OFF = _DESC_OFF + 7 * 20          # + null descriptor
_IAT_OFF = _ILT_OFF + len(IMPORTS) * 16
_NAME_OFF = _IAT_OFF + len(IMPORTS) * 16


def _va(rva):
    return IMAGE_BASE + rva


def _align_up(v, a):
    return (v + a - 1) & ~(a - 1)


def _build_rdata():
    """Import directory + thunks + hint/name entries; returns (bytes, iat_vas)."""
    buf = bytearray(RDATA_SIZE)
    iat_vas = []

    name_off = _NAME_OFF
    name_rvas = []
    for n in IMPORTS:
        name_rvas.append(RDATA_RVA + name_off)
        hint = struct.pack("<H", 0) + n + b"\x00"
        if (name_off + len(hint)) % 2:
            hint += b"\x00"
        buf[name_off:name_off + len(hint)] = hint
        name_off += len(hint)
    if name_off % 2:
        name_off += 1
    dll_rva = RDATA_RVA + name_off
    buf[name_off:name_off + len(DLL_NAME) + 1] = DLL_NAME + b"\x00"

    for i in range(6):
        d = _DESC_OFF + i * 20
        ilt_rva = RDATA_RVA + _ILT_OFF + i * 16
        iat_rva = RDATA_RVA + _IAT_OFF + i * 16
        iat_vas.append(_va(iat_rva))
        struct.pack_into("<IIIII", buf, d, ilt_rva, 0, 0, dll_rva, iat_rva)
        thunk = struct.pack("<Q", name_rvas[i]) + struct.pack("<Q", 0)
        buf[_ILT_OFF + i * 16:_ILT_OFF + i * 16 + 16] = thunk
        buf[_IAT_OFF + i * 16:_IAT_OFF + i * 16 + 16] = thunk
    return bytes(buf), iat_vas


RDATA, IAT_VA = _build_rdata()
IAT_GMFN = IAT_VA[0]
IAT_CREATEFILE = IAT_VA[1]
IAT_READFILE = IAT_VA[2]
IAT_GSTDHANDLE = IAT_VA[3]
IAT_WRITEFILE = IAT_VA[4]
IAT_EXIT = IAT_VA[5]


class _Asm:
    """Tiny x86-64 assembler: absolute 64-bit immediates and rel32 branch fixups."""

    def __init__(self):
        self.b = bytearray()
        self.labels = {}
        self.fixups = []          # (offset, 'rel32', label) | (offset, 'rip32', va)

    def emit(self, *bs):
        for x in bs:
            if isinstance(x, int):
                self.b.append(x)
            else:
                self.b += x

    def label(self, name):
        assert name not in self.labels
        self.labels[name] = len(self.b)

    def rel32(self, opcode, label):
        self.emit(opcode)
        self.fixups.append((len(self.b), "rel32", label))
        self.emit(b"\x00\x00\x00\x00")

    def abs64(self, prefix, value):
        """prefix = opcode bytes of the `mov reg, imm64` / similar form."""
        self.emit(prefix, struct.pack("<Q", value))

    def rip32(self, opcode, rva):
        self.emit(opcode)
        self.fixups.append((len(self.b), "rip32", _va(rva)))
        self.emit(b"\x00\x00\x00\x00")

    def finish(self):
        for off, kind, target in self.fixups:
            if kind == "rel32":
                disp = self.labels[target] - (off + 4)
                self.b[off:off + 4] = struct.pack("<i", disp)
            else:
                next_va = _va(TEXT_RVA + off + 4)
                self.b[off:off + 4] = struct.pack("<i", target - next_va)
        return bytes(self.b)


def build_stub():
    a = _Asm()
    a.emit(b"\x48\x83\xE4\xF0")                     # and rsp, -16
    a.emit(b"\x48\x81\xEC\x80\x00\x00\x00")         # sub rsp, 0x80

    # GetModuleFileNameA(NULL, pathbuf, 260)
    a.emit(b"\x31\xC9")                             # xor ecx, ecx
    a.rip32(b"\x48\x8D\x15", PATHBUF_RVA)           # lea rdx, [rip+pathbuf]
    a.emit(b"\x41\xB8\x04\x01\x00\x00")             # mov r8d, 260
    a.abs64(b"\x48\xB8", IAT_GMFN)                  # mov rax, &IAT[GetModuleFileNameA]
    a.emit(b"\xFF\x10")                             # call [rax]

    # CreateFileA(pathbuf, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, 0)
    a.rip32(b"\x48\x8D\x0D", PATHBUF_RVA)           # lea rcx, [rip+pathbuf]
    a.emit(b"\xBA\x00\x00\x00\x80")                 # mov edx, 0x80000000
    a.emit(b"\x41\xB8\x01\x00\x00\x00")             # mov r8d, 1
    a.emit(b"\x45\x31\xC9")                         # xor r9d, r9d
    a.emit(b"\x48\xC7\x44\x24\x20\x03\x00\x00\x00")  # mov qword [rsp+0x20], 3
    a.emit(b"\x48\xC7\x44\x24\x28\x00\x00\x00\x00")  # mov qword [rsp+0x28], 0
    a.emit(b"\x48\xC7\x44\x24\x30\x00\x00\x00\x00")  # mov qword [rsp+0x30], 0
    a.abs64(b"\x48\xB8", IAT_CREATEFILE)
    a.emit(b"\xFF\x10")                             # call [rax]
    a.emit(b"\x48\x83\xF8\xFF")                     # cmp rax, -1
    a.rel32(b"\x0F\x84", "fail")                    # je fail
    a.emit(b"\x49\x89\xC7")                         # mov r15, rax (file handle)

    # ReadFile(h, DEST, 2 MiB, &nread, NULL)
    a.emit(b"\x4C\x89\xF9")                         # mov rcx, r15
    a.abs64(b"\x48\xBA", _va(DEST_RVA))             # mov rdx, DEST
    a.emit(b"\x41\xB8\x00\x00\x20\x00")             # mov r8d, 0x100000
    a.emit(b"\x4C\x8D\x4C\x24\x40")                 # lea r9, [rsp+0x40]
    a.emit(b"\x48\xC7\x44\x24\x20\x00\x00\x00\x00")  # mov qword [rsp+0x20], 0
    a.abs64(b"\x48\xB8", IAT_READFILE)
    a.emit(b"\xFF\x10")                             # call [rax]

    # --- the read must contain the whole prefix at least
    a.emit(b"\x48\x8B\x44\x24\x40")                 # mov rax, [rsp+0x40] (nread)
    a.emit(b"\x48\x3D" + struct.pack("<I", REC_OFF + 2))   # cmp rax, REC_OFF+2
    a.rel32(b"\x0F\x82", "fail_file")               # jb fail_file

    # --- file must be <= READ_LIMIT: probe for one more byte (record stream
    #     beyond the buffer would otherwise be silently truncated)
    a.emit(b"\x4C\x89\xF9")                         # mov rcx, r15
    a.abs64(b"\x48\xBA", _va(BYTEBUF_RVA))          # mov rdx, &probe byte
    a.emit(b"\x41\xB8\x01\x00\x00\x00")             # mov r8d, 1
    a.emit(b"\x4C\x8D\x4C\x24\x48")                 # lea r9, [rsp+0x48]
    a.emit(b"\x48\xC7\x44\x24\x20\x00\x00\x00\x00")  # mov qword [rsp+0x20], 0
    a.abs64(b"\x48\xB8", IAT_READFILE)
    a.emit(b"\xFF\x10")                             # call [rax]
    a.emit(b"\x83\x7C\x24\x48\x00")                 # cmp dword [rsp+0x48], 0
    a.rel32(b"\x0F\x85", "fail_big")                # jne fail_big

    # rdi = end of record stream
    a.emit(b"\x48\x8B\x7C\x24\x40")                 # mov rdi, [rsp+0x40] (bytes read)
    a.abs64(b"\x49\xB9", _va(DEST_RVA))             # mov r9, DEST
    a.emit(b"\x4C\x01\xCF")                         # add rdi, r9
    a.abs64(b"\x48\xBE", _va(DEST_RVA + REC_OFF))   # mov rsi, first record
    a.abs64(b"\x49\xBB", _va(PATCHSTACK_RVA))       # mov r11, patch stack

    # ---- relocation pass over the record stream
    a.label("patch_loop")
    a.emit(b"\x48\x39\xFE")                         # cmp rsi, rdi
    a.rel32(b"\x0F\x83", "patch_done")              # jae patch_done
    a.emit(b"\x0F\xB6\x46\x01")                     # movzx eax, byte [rsi+1]
    a.emit(b"\x3C\x07")                             # cmp al, T_OPEN
    a.rel32(b"\x0F\x84", "do_open")                 # je do_open
    a.emit(b"\x3C\x08")                             # cmp al, T_CLOSE
    a.rel32(b"\x0F\x84", "do_close")                # je do_close
    a.label("advance")
    a.emit(b"\x0F\xB6\x06")                         # movzx eax, byte [rsi]
    a.emit(b"\x48\x01\xC6")                         # add rsi, rax
    a.rel32(b"\xE9", "patch_loop")                  # jmp patch_loop

    a.label("do_open")
    a.abs64(b"\x48\xB8", _va(PATCHSTACK_RVA + PATCHSTACK_SIZE))  # mov rax, stack end
    a.emit(b"\x49\x39\xC3")                         # cmp r11, rax
    a.rel32(b"\x0F\x83", "fail_deep")               # jae fail_deep (nesting too deep)
    a.emit(b"\x49\x89\x33")                         # mov [r11], rsi
    a.emit(b"\x49\x83\xC3\x08")                     # add r11, 8
    a.rel32(b"\xE9", "advance")

    a.label("do_close")
    a.abs64(b"\x48\xB8", _va(PATCHSTACK_RVA))       # mov rax, stack base
    a.emit(b"\x49\x39\xC3")                         # cmp r11, rax
    a.rel32(b"\x0F\x86", "fail_close")              # jbe fail_close (unmatched ']')
    a.emit(b"\x49\x83\xEB\x08")                     # sub r11, 8
    a.emit(b"\x4D\x8B\x13")                         # mov r10, [r11]
    a.emit(b"\x0F\xB6\x06")                         # movzx eax, byte [rsi]
    a.emit(b"\x48\x83\xC0\x02")                     # add rax, 2
    a.emit(b"\x48\x01\xF0")                         # add rax, rsi
    a.emit(b"\x49\x8D\x52\x0B")                     # lea rdx, [r10+11]
    a.emit(b"\x48\x29\xD0")                         # sub rax, rdx
    a.emit(b"\x41\x89\x42\x07")                     # mov [r10+7], eax   (open je)
    a.emit(b"\x49\x8D\x42\x02")                     # lea rax, [r10+2]
    a.emit(b"\x48\x8D\x56\x0B")                     # lea rdx, [rsi+11]
    a.emit(b"\x48\x29\xD0")                         # sub rax, rdx
    a.emit(b"\x89\x46\x07")                         # mov [rsi+7], eax   (close jne)
    a.rel32(b"\xE9", "advance")

    # ---- get std handles and enter the stream
    a.label("patch_done")
    a.abs64(b"\x48\xB8", _va(PATCHSTACK_RVA))       # mov rax, stack base
    a.emit(b"\x49\x39\xC3")                         # cmp r11, rax
    a.rel32(b"\x0F\x85", "fail_open")               # jne fail_open (unmatched '[')
    a.emit(b"\xB9\xF5\xFF\xFF\xFF")                 # mov ecx, -11 (stdout)
    a.abs64(b"\x48\xB8", IAT_GSTDHANDLE)
    a.emit(b"\xFF\x10")                             # call [rax]
    a.emit(b"\x49\x89\xC5")                         # mov r13, rax
    a.emit(b"\xB9\xF6\xFF\xFF\xFF")                 # mov ecx, -10 (stdin)
    a.abs64(b"\x48\xB8", IAT_GSTDHANDLE)
    a.emit(b"\xFF\x10")                             # call [rax]
    a.emit(b"\x49\x89\xC6")                         # mov r14, rax
    a.abs64(b"\x48\xBB", _va(TAPE_RVA))             # mov rbx, tape
    a.abs64(b"\x48\xBD", _va(WRITTEN_RVA))          # mov rbp, &written
    a.abs64(b"\x49\xBF", _va(BYTEBUF_RVA))          # mov r15, &bytebuf
    a.abs64(b"\x48\xBE", _va(READBUF_RVA))          # mov rsi, &readbuf
    a.abs64(b"\x48\xBF", _va(READCNT_RVA))          # mov rdi, &readcount
    a.abs64(b"\x48\xB8", _va(DEST_RVA + REC_OFF + 2))
    a.emit(b"\xFF\xE0")                             # jmp rax

    def _fail_error(label, code):
        a.label(label)
        a.emit(b"\xB9" + struct.pack("<I", code))   # mov ecx, <code>
        a.abs64(b"\x48\xB8", IAT_EXIT)
        a.emit(b"\xFF\x10")                         # call [rax]

    # Exit codes of a PAK executable that cannot run its payload:
    #   1 = cannot open/read own file
    #   2 = record stream larger than the 2 MiB read buffer (would be truncated)
    #   3 = loop nesting deeper than 4096
    #   4 = unmatched ']'
    #   5 = unmatched '['
    #   6 = file shorter than the record-stream offset (no payload)
    _fail_error("fail", 1)
    _fail_error("fail_big", 2)
    _fail_error("fail_deep", 3)
    _fail_error("fail_close", 4)
    _fail_error("fail_open", 5)
    _fail_error("fail_file", 6)
    return a.finish()


STUB = build_stub()


def build_headers():
    """0x200-byte DOS + PE headers for the PAK layout."""
    h = bytearray(HEADERS_SIZE)
    h[0:2] = b"MZ"
    struct.pack_into("<I", h, 0x3C, 0x80)           # e_lfanew
    h[0x80:0x84] = b"PE\x00\x00"

    size_code = TEXT_SIZE
    size_init = RDATA_SIZE
    struct.pack_into("<HHIIIHH", h, 0x84,
                     0x8664, 2, 0, 0, 0, 0xF0, 0x0022)

    opt = 0x98
    struct.pack_into("<H", h, opt, 0x20B)           # PE32+
    struct.pack_into("<I", h, opt + 4, size_code)              # SizeOfCode
    struct.pack_into("<I", h, opt + 8, size_init)              # SizeOfInitializedData
    struct.pack_into("<I", h, opt + 12, 0)                     # SizeOfUninitializedData
    struct.pack_into("<I", h, opt + 16, TEXT_RVA)              # AddressOfEntryPoint
    struct.pack_into("<I", h, opt + 20, TEXT_RVA)              # BaseOfCode
    struct.pack_into("<Q", h, opt + 24, IMAGE_BASE)            # ImageBase
    struct.pack_into("<II", h, opt + 32, SECT_ALIGN, FILE_ALIGN)
    struct.pack_into("<HHHHHH", h, opt + 40, 6, 0, 0, 0, 6, 0)
    struct.pack_into("<I", h, opt + 52, 0)                     # Win32VersionValue
    struct.pack_into("<I", h, opt + 56, SIZE_OF_IMAGE)
    struct.pack_into("<I", h, opt + 60, HEADERS_SIZE)
    struct.pack_into("<I", h, opt + 64, 0)                     # CheckSum
    struct.pack_into("<HH", h, opt + 68, 3, 0)                 # Subsystem=CUI, DllChars=0
    struct.pack_into("<QQQQ", h, opt + 72,
                     0x100000, 0x1000, 0x100000, 0x1000)
    struct.pack_into("<II", h, opt + 104, 0, 16)               # LoaderFlags, NrvaAndSizes
    # data directory 1 = import table, 12 = import address table
    struct.pack_into("<II", h, opt + 112 + 8, RDATA_RVA, RDATA_SIZE)
    struct.pack_into("<II", h, opt + 112 + 12 * 8,
                     RDATA_RVA + _IAT_OFF, len(IMPORTS) * 16)

    # section table
    sec = opt + 0xF0
    struct.pack_into("<8sIIIIIIHHI", h, sec,
                     b".rdata", RDATA_SIZE, RDATA_RVA, RDATA_SIZE,
                     RDATA_RAW, 0, 0, 0, 0, 0x40000040)
    struct.pack_into("<8sIIIIIIHHI", h, sec + 40,
                     b".text", TEXT_VSIZE, TEXT_RVA, TEXT_SIZE,
                     TEXT_RAW, 0, 0, 0, 0, 0xE0000020)
    return bytes(h)


HEADERS = build_headers()


def header_prefix():
    """The fixed 0x800 bytes every PAK executable starts with."""
    out = bytearray(REC_OFF)
    out[0:HEADERS_SIZE] = HEADERS
    out[RDATA_RAW:RDATA_RAW + RDATA_SIZE] = RDATA
    assert len(STUB) <= TEXT_SIZE, "stub too large: %d" % len(STUB)
    out[TEXT_RAW:TEXT_RAW + len(STUB)] = STUB
    return bytes(out)


PREFIX = header_prefix()


def rec(rtype, payload):
    body = bytes([rtype]) + payload + b"\xEB\x02"
    return bytes([len(body) + 1]) + body


REC_ADD = rec(T_ADD, b"\xFE\x03")
REC_SUB = rec(T_SUB, b"\xFE\x0B")
REC_RIGHT = rec(T_RIGHT, b"\x48\xFF\xC3")
REC_LEFT = rec(T_LEFT, b"\x48\xFF\xCB")
REC_OUT = rec(T_OUT,
              b"\x8A\x03"                             # mov al, [rbx]
              b"\x41\x88\x07"                         # mov [r15], al
              b"\x4C\x89\xE9"                         # mov rcx, r13
              b"\x4C\x89\xFA"                         # mov rdx, r15
              b"\x41\xB8\x01\x00\x00\x00"             # mov r8d, 1
              b"\x49\x89\xE9"                         # mov r9, rbp
              b"\x48\xB8" + struct.pack("<Q", IAT_WRITEFILE)
              + b"\xFF\x10")                          # call [rax]
REC_IN = rec(T_IN,
             b"\x4C\x89\xF1"                          # mov rcx, r14
             b"\x48\x89\xF2"                          # mov rdx, rsi
             b"\x41\xB8\x01\x00\x00\x00"              # mov r8d, 1
             b"\x49\x89\xF9"                          # mov r9, rdi
             b"\x48\xB8" + struct.pack("<Q", IAT_READFILE)
             + b"\xFF\x10"                            # call [rax]
             + b"\x31\xC0"                            # xor eax, eax
             + b"\x83\x3F\x00"                        # cmp dword [rdi], 0
             + b"\x74\x02"                            # je +2
             + b"\x8A\x06"                            # mov al, [rsi]
             + b"\x88\x03")                           # mov [rbx], al
REC_OPEN = rec(T_OPEN, b"\x80\x3B\x00" + b"\x0F\x84\x00\x00\x00\x00")
REC_CLOSE = rec(T_CLOSE, b"\x80\x3B\x00" + b"\x0F\x85\x00\x00\x00\x00")
REC_END = rec(T_END,
              b"\x31\xC9"                              # xor ecx, ecx (exit code 0)
              b"\x48\xB8" + struct.pack("<Q", IAT_EXIT)
              + b"\xFF\x10")                           # call [rax]

RECORDS = {
    ord("+"): REC_ADD,
    ord("-"): REC_SUB,
    ord(">"): REC_RIGHT,
    ord("<"): REC_LEFT,
    ord("."): REC_OUT,
    ord(","): REC_IN,
    ord("["): REC_OPEN,
    ord("]"): REC_CLOSE,
}
REC_GARBAGE = b""            # non-instruction bytes contribute nothing


def pack(record_stream):
    """prefix + records -> complete executable bytes."""
    return PREFIX + record_stream + REC_END


def compile_source(source, keys=b"+-><.,[]"):
    """Reference model: BF source bytes -> PAK executable bytes."""
    out = bytearray()
    for ch in source:
        if ch in keys:
            out += RECORDS[ch]
    return pack(bytes(out))


def main(argv):
    import sys
    if len(argv) != 3:
        print("usage: pak.py INPUT.bf OUTPUT.exe")
        return 2
    with open(argv[1], "rb") as f:
        src = f.read()
    exe = compile_source(src)
    with open(argv[2], "wb") as f:
        f.write(exe)
    print("wrote %s (%d bytes, prefix %d)" % (argv[2], len(exe), len(PREFIX)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
