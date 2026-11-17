#!/usr/bin/env python3
"""Standalone PE32+ structure verifier (no third-party deps).

Parses an exe emitted by bfc with struct and checks:
  * DOS header / PE signature / COFF header / PE32+ optional header fields
  * section table: RVA, raw pointer, alignment consistency
  * import table: walk DataDirectory[1] to KERNEL32.dll and every imported name

So even if the exe happens to run, we confirm it is a real PE image rather
than something the loader accepted by accident.
"""
import struct
import sys


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def cstr(b, o):
    e = b.index(b"\0", o)
    return b[o:e].decode("ascii", "replace")


def parse(path):
    return parse_bytes(open(path, "rb").read(), path)


def parse_bytes(b, path="<memory>"):
    r = {"file": path, "size": len(b), "errors": []}

    def bad(msg):
        r["errors"].append(msg)

    if b[:2] != b"MZ":
        bad("DOS magic != MZ")
        return r
    r["e_lfanew"] = u32(b, 0x3C)
    if b[r["e_lfanew"]:r["e_lfanew"] + 4] != b"PE\0\0":
        bad("PE signature missing")
        return r

    coff = r["e_lfanew"] + 4
    r["machine"] = u16(b, coff)
    r["n_sections"] = u16(b, coff + 2)
    r["opt_size"] = u16(b, coff + 16)
    r["characteristics"] = u16(b, coff + 18)
    if r["machine"] != 0x8664:
        bad("Machine != 0x8664 (AMD64)")

    opt = coff + 20
    r["magic"] = u16(b, opt)
    if r["magic"] != 0x20B:
        bad("Optional magic != 0x20B (PE32+)")
    r["entry"] = u32(b, opt + 16)
    r["base_of_code"] = u32(b, opt + 20)
    r["image_base"] = u64(b, opt + 24)
    r["section_align"] = u32(b, opt + 32)
    r["file_align"] = u32(b, opt + 36)
    r["size_of_image"] = u32(b, opt + 56)
    r["size_of_headers"] = u32(b, opt + 60)
    r["subsystem"] = u16(b, opt + 68)
    r["dll_characteristics"] = u16(b, opt + 70)
    r["n_rva"] = u32(b, opt + 108)
    if r["subsystem"] != 3:
        bad("Subsystem != 3 (console)")
    if r["n_rva"] != 16:
        bad("NumberOfRvaAndSizes != 16")

    dirs = {}
    for i in range(r["n_rva"]):
        dirs[i] = (u32(b, opt + 112 + 8 * i), u32(b, opt + 112 + 8 * i + 4))
    r["dir_import"] = dirs.get(1)
    r["dir_iat"] = dirs.get(12)

    sect = opt + r["opt_size"]
    secs = []
    for i in range(r["n_sections"]):
        o = sect + 40 * i
        s = {
            "name": cstr(b, o),
            "vsize": u32(b, o + 8),
            "rva": u32(b, o + 12),
            "raw_size": u32(b, o + 16),
            "raw_ptr": u32(b, o + 20),
            "chars": u32(b, o + 36),
        }
        secs.append(s)
        if s["raw_ptr"] % r["file_align"]:
            bad("%s: PointerToRawData not FileAlignment-aligned" % s["name"])
        if s["rva"] % r["section_align"]:
            bad("%s: VirtualAddress not SectionAlignment-aligned" % s["name"])
        if s["raw_size"] and s["raw_ptr"] + s["raw_size"] > len(b):
            bad("%s: raw data runs past EOF" % s["name"])
    r["sections"] = secs

    hit = [s for s in secs if s["rva"] <= r["entry"] < s["rva"] + s["vsize"]]
    if not hit:
        bad("AddressOfEntryPoint is not inside any section")
    elif not (hit[0]["chars"] & 0x20000000):
        bad("entry section lacks MEM_EXECUTE")
    if r["size_of_headers"] > secs[0]["raw_ptr"]:
        bad("SizeOfHeaders exceeds first section raw pointer")
    end = max(s["rva"] + s["vsize"] for s in secs)
    if r["size_of_image"] < end:
        bad("SizeOfImage smaller than end of last section")
    if r["size_of_image"] % r["section_align"]:
        bad("SizeOfImage not SectionAlignment-aligned")

    def rva2off(rva):
        for s in secs:
            if s["rva"] <= rva < s["rva"] + max(s["vsize"], s["raw_size"]):
                return s["raw_ptr"] + (rva - s["rva"])
        return None

    imports = []
    if not r["dir_import"] or r["dir_import"][0] == 0:
        bad("no import directory")
    else:
        off = rva2off(r["dir_import"][0])
        while True:
            oft, _ts, _fc, name_rva, ft = struct.unpack_from("<IIIII", b, off)
            if oft == 0 and name_rva == 0 and ft == 0:
                break
            dll = cstr(b, rva2off(name_rva))
            to = rva2off(oft or ft)
            names = []
            while True:
                ent = u64(b, to)
                if ent == 0:
                    break
                if ent & 0x8000000000000000:
                    names.append("ordinal#%d" % (ent & 0xFFFF))
                else:
                    o = rva2off(ent & 0xFFFFFFFF)
                    if o is None:
                        bad("import thunk RVA 0x%X outside image" % (ent & 0xFFFFFFFF))
                        break
                    names.append(cstr(b, o + 2))
                to += 8
            imports.append((dll, names))
            off += 20
    r["imports"] = imports

    iat_rva, iat_size = r["dir_iat"] if r["dir_iat"] else (0, 0)
    if iat_rva == 0:
        bad("no IAT directory")
    else:
        io = rva2off(iat_rva)
        r["iat"] = [u64(b, io + 8 * i) for i in range(iat_size // 8)]

    return r


def main():
    ok = True
    for path in sys.argv[1:]:
        r = parse(path)
        print("=== %s (%d bytes) ===" % (r["file"], r["size"]))
        if "machine" in r:
            print("  machine=0x%04X magic=0x%03X subsystem=%d sections=%d "
                  "entry=0x%X image_base=0x%X"
                  % (r["machine"], r["magic"], r["subsystem"], r["n_sections"],
                     r["entry"], r["image_base"]))
            print("  align: section=0x%X file=0x%X size_of_image=0x%X headers=0x%X"
                  % (r["section_align"], r["file_align"], r["size_of_image"],
                     r["size_of_headers"]))
            for s in r["sections"]:
                print("  %-8s rva=0x%04X vsize=%-6d raw@0x%04X rawsize=%-6d chars=0x%08X"
                      % (s["name"], s["rva"], s["vsize"], s["raw_ptr"],
                         s["raw_size"], s["chars"]))
            for dll, names in r["imports"]:
                print("  import: %s -> %s" % (dll, ", ".join(names)))
            if "iat" in r:
                print("  iat: %s" % ["0x%X" % v for v in r["iat"]])
        for e in r["errors"]:
            ok = False
            print("  !! %s" % e)
        if not r["errors"]:
            print("  OK: structure verified")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
