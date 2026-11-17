// ============================================================================
//  bfc.cpp --- Brainfuck -> Windows x86-64 (PE32+) 可执行文件编译器
//
//  编译流水线（全程无中间转译）：
//
//      .bf 源码
//        │  ① 词法过滤：只保留 8 个指令，记录源码位置用于报错
//        │  ② 语法检查：括号配对，[ 与 ] 之间做跳转回填
//        │  ③ 代码生成：手写 x86-64 指令编码，直接写入内存中的代码缓冲区
//        │  ④ 映像组装：DOS 头 / PE 签名 / COFF 头 / 可选头 / 节表 / 导入表
//        ▼
//      .exe
//
//  本程序不生成 C 或汇编源码，不调用汇编器、链接器，也不链接任何运行时库；
//  kernel32 的三个导入函数的导入表（ILT/IAT/名称表/描述符）全部由本文件
//  自己计算 RVA 并写进节里。生成的 exe 不依赖 MSVCRT / libstdc++。
//
//  用法：
//      bfc <input.bf> [-o output.exe] [-c cells] [--dump]
//
//  目标平台：Windows x86-64，控制台子系统（Subsystem = 3）。
// ============================================================================

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstddef>
#include <string>
#include <vector>
#include <algorithm>

typedef uint8_t  u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int32_t  i32;
typedef int64_t  i64;
typedef uint64_t u64;

// ---------------------------------------------------------------- 全局常量

static const u32 SECTION_ALIGN = 0x1000;   // 内存中的节对齐
static const u32 FILE_ALIGN    = 0x200;    // 文件中的节对齐
static const u32 DOS_HDR_SIZE  = 0x40;     // IMAGE_DOS_HEADER 固定 64 字节
static const u32 DOS_STUB_OFF  = 0x40;     // DOS 存根（不参与运行，只为兼容）
static const u32 PE_SIG_OFF    = 0x80;     // "PE\0\0" 的位置，即 e_lfanew 的值
static const u32 TEXT_RVA      = 0x1000;   // .text 的 RVA（头之后第一个节）
static const u64 IMAGE_BASE    = 0x140000000ULL;
static const u32 DEFAULT_CELLS = 30000;    // 磁带单元数
static const u32 OUTBUF_SIZE   = 4096;     // 输出缓冲区大小

// 导入表中的槽位顺序（ILT 与 IAT 必须一致）
static const int SLOT_GETSTDHANDLE = 0;
static const int SLOT_WRITEFILE    = 1;
static const int SLOT_READFILE     = 2;
static const int NUM_IMPORTS       = 3;
static const char* IMPORT_NAMES[NUM_IMPORTS] = { "GetStdHandle", "WriteFile", "ReadFile" };
static const char* IMPORT_DLL = "KERNEL32.dll";

static const u32 STD_INPUT_HANDLE  = (u32)-10;
static const u32 STD_OUTPUT_HANDLE = (u32)-11;

static inline u32 alignUp(u32 v, u32 a) { return (v + a - 1) / a * a; }

// ---------------------------------------------------------------- 错误处理

static std::string g_src;   // 原始源码，用于计算报错的行列号

static void die(const std::string& msg) {
    fprintf(stderr, "bfc: error: %s\n", msg.c_str());
    exit(1);
}

static void dieAt(size_t pos, const std::string& msg) {
    size_t line = 1, col = 1;
    for (size_t i = 0; i < pos && i < g_src.size(); i++) {
        if (g_src[i] == '\n') { line++; col = 1; } else { col++; }
    }
    fprintf(stderr, "bfc: error: %s (line %zu, col %zu)\n", msg.c_str(), line, col);
    exit(1);
}

// ---------------------------------------------------------------- 数据段布局
//
//  .data 一节的布局（全部由本结构计算，代码里的 RIP 相对寻址直接引用这些 RVA）：
//
//      +0        tape      磁带，cells 字节，全 0
//      +cells    outbuf    输出缓冲区
//      +...      inp       输入 1 字节的落点
//      +...      written   ReadFile/WriteFile 的 lpNumberOfBytes* 输出
//      +...      iat       导入地址表（3 项，加载器写入真实地址，需可写）
//      +...      ilt       导入查找表（3 项，指向下面的 hint/name）
//      +...      hintName  3 个 IMAGE_IMPORT_BY_NAME（WORD hint + ASCIIZ 名称）
//      +...      dllName   "KERNEL32.dll"
//      +...      importDesc 导入描述符 + 一项全零结束描述符
//
struct DataLayout {
    u32 rva      = 0;
    u32 tape     = 0;
    u32 outbuf   = 0;
    u32 inp      = 0;
    u32 written  = 0;
    u32 iat      = 0;
    u32 ilt      = 0;
    u32 hintName[NUM_IMPORTS] = {0, 0, 0};
    u32 dllName  = 0;
    u32 importDesc = 0;
    u32 size     = 0;      // 节的虚拟大小

    u32 iatSlot(int i) const { return iat + 8u * (u32)i; }
};

static DataLayout layoutData(u32 dataRva, u32 cells) {
    DataLayout d;
    d.rva = dataRva;
    u32 off = 0;
    d.tape   = dataRva + off; off += cells;
    d.outbuf = dataRva + off; off += OUTBUF_SIZE;
    d.inp    = dataRva + off; off += 1;
    off = alignUp(off, 4); d.written = dataRva + off; off += 4;
    off = alignUp(off, 8); d.iat = dataRva + off; off += 8u * (NUM_IMPORTS + 1);
    d.ilt = dataRva + off; off += 8u * (NUM_IMPORTS + 1);
    for (int i = 0; i < NUM_IMPORTS; i++) {
        off = alignUp(off, 2);
        d.hintName[i] = dataRva + off;
        off += 2u + (u32)strlen(IMPORT_NAMES[i]) + 1u;
    }
    off = alignUp(off, 2);
    d.dllName = dataRva + off; off += (u32)strlen(IMPORT_DLL) + 1u;
    off = alignUp(off, 4);
    d.importDesc = dataRva + off; off += 20u * 2u;   // 描述符 + 结束描述符
    d.size = off;
    return d;
}

static std::vector<u8> buildData(const DataLayout& d) {
    std::vector<u8> b(d.size, 0);
    auto put32 = [&](u32 off, u32 v) { memcpy(&b[off], &v, 4); };
    auto put64 = [&](u32 off, u64 v) { memcpy(&b[off], &v, 8); };

    for (int i = 0; i < NUM_IMPORTS; i++) {
        // ILT 与 IAT 初始内容相同：指向 IMAGE_IMPORT_BY_NAME 的 RVA
        put64(d.iat - d.rva + 8u * i, d.hintName[i]);
        put64(d.ilt - d.rva + 8u * i, d.hintName[i]);
        u32 o = d.hintName[i] - d.rva;
        b[o] = 0; b[o + 1] = 0;                                  // Hint = 0
        memcpy(&b[o + 2], IMPORT_NAMES[i], strlen(IMPORT_NAMES[i]) + 1);
    }
    memcpy(&b[d.dllName - d.rva], IMPORT_DLL, strlen(IMPORT_DLL) + 1);

    u32 o = d.importDesc - d.rva;
    put32(o + 0,  d.ilt);        // OriginalFirstThunk
    put32(o + 4,  0);            // TimeDateStamp
    put32(o + 8,  0);            // ForwarderChain
    put32(o + 12, d.dllName);    // Name
    put32(o + 16, d.iat);        // FirstThunk
    // 紧随其后的 20 字节保持全 0 = 结束描述符
    return b;
}

// ---------------------------------------------------------------- 中间表示
//
// 连续的同一种指令会合并成一条 Run，便于生成 add/sub 立即数指令。

struct Run {
    char op;      // 8 个 BF 指令之一
    u32  count;   // 重复次数
    size_t pos;   // 在源码中的偏移（报错用）
};

// ---------------------------------------------------------------- 代码生成
//
//  生成的函数约定（这是进程入口，不需要对任何调用者负责）：
//
//      rbx  = 数据指针（指向当前单元）        —— 非易失寄存器，WinAPI 调用后仍有效
//      r12  = 输出缓冲区中待写出字节数        —— 同上
//      [rsp+0x00 .. rsp+0x20) = 影子空间；[rsp+0x20] = 第 5 个参数的槽位
//      RSP 始终保持 16 字节对齐（入口处 sub rsp,0x28 完成）
//
//  入口布局：
//
//      entry:   xor r12d,r12d / sub rsp,0x28 / lea rbx,[rip+tape]
//               jmp body            <- 跳过下面的 flush 例程
//      flush:   ...                 <- 位置固定，body 里的 call 直接相对寻址
//      body:    ... BF 主体 ...
//               call flush / xor ecx,ecx / add rsp,0x28 / ret
//
class CodeGen {
public:
    explicit CodeGen(const DataLayout& d) : d(d) {}

    std::vector<u8> compile(const std::vector<Run>& runs);

private:
    struct Loop {
        size_t jeDisp;   // '[' 产生的 je rel32 立即数位置
        size_t afterJe;  // je 之后的位置（']' 的 jne 目标）
        size_t srcPos;   // '[' 在源码中的位置
    };

    const DataLayout& d;
    std::vector<u8> code;
    size_t flushOff = 0;

    size_t here() const { return code.size(); }
    void   b(u8 v) { code.push_back(v); }
    void   w32(u32 v) { for (int i = 0; i < 4; i++) code.push_back((u8)(v >> (8 * i))); }

    u32 rvaAt(size_t off) const { return TEXT_RVA + (u32)off; }

    // 相对跳转：目标 = 源码里的某个偏移
    void fixRel32(size_t dispPos, size_t targetOff) {
        i32 rel = (i32)((i64)rvaAt(targetOff) - (i64)(rvaAt(dispPos) + 4));
        memcpy(&code[dispPos], &rel, 4);
    }
    // RIP 相对寻址：基址是"整条指令的末尾"，所以必须传指令起点，长度取当前已发字节数
    void ripDisp(size_t insnStart, size_t dispPos, u32 targetRva) {
        i32 rel = (i32)((i64)targetRva - (i64)(rvaAt(insnStart) + (u32)(code.size() - insnStart)));
        memcpy(&code[dispPos], &rel, 4);
    }

    // ---- 指令发射器（编码见文件末尾的对照表注释） ----
    void subRsp(u32 n) { b(0x48); b(0x83); b(0xEC); b((u8)n); }
    void addRsp(u32 n) { b(0x48); b(0x83); b(0xC4); b((u8)n); }
    void movEcxImm(u32 v) { b(0xB9); w32(v); }

    void leaRbxRip(u32 rva) { size_t s = here(); b(0x48); b(0x8D); b(0x1D); size_t p = here(); w32(0); ripDisp(s, p, rva); }
    void leaRcxRip(u32 rva) { size_t s = here(); b(0x48); b(0x8D); b(0x0D); size_t p = here(); w32(0); ripDisp(s, p, rva); }
    void leaRdxRip(u32 rva) { size_t s = here(); b(0x48); b(0x8D); b(0x15); size_t p = here(); w32(0); ripDisp(s, p, rva); }
    void leaR9Rip (u32 rva) { size_t s = here(); b(0x4C); b(0x8D); b(0x0D); size_t p = here(); w32(0); ripDisp(s, p, rva); }
    void callIat  (u32 rva) { size_t s = here(); b(0xFF); b(0x15);         size_t p = here(); w32(0); ripDisp(s, p, rva); }
    void callRel  (size_t targetOff) { b(0xE8); size_t p = here(); w32(0); fixRel32(p, targetOff); }

    // ---- BF 指令展开 ----
    void ptrAdd(u32 n) { if (n == 1) { b(0x48); b(0xFF); b(0xC3); } else { b(0x48); b(0x81); b(0xC3); w32(n); } }
    void ptrSub(u32 n) { if (n == 1) { b(0x48); b(0xFF); b(0xCB); } else { b(0x48); b(0x81); b(0xEB); w32(n); } }
    void cellAdd(u32 n) { while (n) { u32 k = n > 255 ? 255 : n; n -= k;
                            if (k == 1) { b(0xFE); b(0x03); } else { b(0x80); b(0x03); b((u8)k); } } }
    void cellSub(u32 n) { while (n) { u32 k = n > 255 ? 255 : n; n -= k;
                            if (k == 1) { b(0xFE); b(0x0B); } else { b(0x80); b(0x2B); b((u8)k); } } }
    void cellZero() { b(0xC6); b(0x03); b(0x00); }          // mov byte [rbx], 0
    void cmpCellZero() { b(0x80); b(0x3B); b(0x00); }       // cmp byte [rbx], 0

    void emitFlush();
    void emitOutput();
    void emitInput();
};

// flush 例程：把 outbuf 里积攒的 r12 个字节写到 stdout，并清零计数。
// 经由 call 进入，故进入时 RSP % 16 == 8，需要 sub rsp,0x28 恢复对齐。
void CodeGen::emitFlush() {
    subRsp(0x28);
    b(0x4D); b(0x85); b(0xE4);                       // test r12, r12
    b(0x0F); b(0x84); size_t pz = here(); w32(0);    // jz done
    movEcxImm(STD_OUTPUT_HANDLE);
    callIat(d.iatSlot(SLOT_GETSTDHANDLE));           // GetStdHandle(-11)
    b(0x48); b(0x89); b(0xC1);                       // mov rcx, rax
    leaRdxRip(d.outbuf);                             // lpBuffer
    b(0x4D); b(0x89); b(0xE0);                       // mov r8, r12   (nNumberOfBytesToWrite)
    leaR9Rip(d.written);                             // lpNumberOfBytesWritten
    b(0x48); b(0xC7); b(0x44); b(0x24); b(0x20); w32(0);   // mov qword [rsp+0x20], 0 (lpOverlapped)
    callIat(d.iatSlot(SLOT_WRITEFILE));              // WriteFile(...)
    b(0x45); b(0x31); b(0xE4);                       // xor r12d, r12d
    fixRel32(pz, here());                            // done:
    addRsp(0x28);
    b(0xC3);                                         // ret
}

// '.' —— 把当前单元追加进输出缓冲区，满了就冲洗
void CodeGen::emitOutput() {
    b(0x0F); b(0xB6); b(0x03);                       // movzx eax, byte [rbx]
    leaRcxRip(d.outbuf);                             // lea rcx, [rip+outbuf]
    b(0x42); b(0x88); b(0x04); b(0x21);              // mov [rcx+r12], al
    b(0x49); b(0xFF); b(0xC4);                       // inc r12
    b(0x49); b(0x81); b(0xFC); w32(OUTBUF_SIZE);     // cmp r12, 4096
    b(0x0F); b(0x85); size_t p = here(); w32(0);     // jne skip
    fixRel32(p, here() + 5);                         //   （跳过下面 5 字节的 call）
    callRel(flushOff);
}

// ',' —— 读 1 字节写入当前单元；EOF 则写 0
void CodeGen::emitInput() {
    callRel(flushOff);                               // 先把缓冲输出刷掉，保证交互顺序
    movEcxImm(STD_INPUT_HANDLE);
    callIat(d.iatSlot(SLOT_GETSTDHANDLE));           // GetStdHandle(-10)
    b(0x48); b(0x89); b(0xC1);                       // mov rcx, rax
    leaRdxRip(d.inp);                                // lpBuffer
    b(0x41); b(0xB8); w32(1);                        // mov r8d, 1
    leaR9Rip(d.written);                             // lpNumberOfBytesRead
    b(0x48); b(0xC7); b(0x44); b(0x24); b(0x20); w32(0);   // mov qword [rsp+0x20], 0
    callIat(d.iatSlot(SLOT_READFILE));               // ReadFile(...)
    b(0x31); b(0xC0);                                // xor eax, eax        ; 默认结果 0
    { size_t s = here(); b(0x83); b(0x3D); size_t p = here(); w32(0); b(0x01);
      ripDisp(s, p, d.written); }                    // cmp dword [rip+written], 1
    b(0x0F); b(0x85); size_t pj = here(); w32(0);    // jne store
    { size_t s = here(); b(0x8A); b(0x05); size_t p = here(); w32(0);
      ripDisp(s, p, d.inp); }                        // mov al, [rip+inp]
    fixRel32(pj, here());                            // store:
    b(0x88); b(0x03);                                // mov [rbx], al
}

std::vector<u8> CodeGen::compile(const std::vector<Run>& runs) {
    code.clear();

    // ---- 进程入口 ----
    b(0x45); b(0x31); b(0xE4);          // xor r12d, r12d   ; 输出计数 = 0
    subRsp(0x28);                        // 影子空间 + 16 字节对齐
    leaRbxRip(d.tape);                   // rbx = 磁带首地址（RIP 相对，与加载基址无关）
    b(0xE9); size_t jmpDisp = here(); w32(0);   // jmp body（跳过 flush 例程）

    // ---- flush 例程放在 body 之前，这样 body 里的 call 目标已知，无需回填 ----
    flushOff = here();
    emitFlush();

    size_t bodyOff = here();
    fixRel32(jmpDisp, bodyOff);

    // ---- BF 主体 ----
    std::vector<Loop> loops;
    for (size_t i = 0; i < runs.size(); i++) {
        const Run& r = runs[i];
        switch (r.op) {
        case '>': ptrAdd(r.count); break;
        case '<': ptrSub(r.count); break;
        case '+': cellAdd(r.count); break;
        case '-': cellSub(r.count); break;
        case '.': emitOutput(); break;
        case ',': emitInput();  break;

        case '[':
            // 窥孔优化：[-] 与 [+] 等价于把当前单元清零
            if (i + 2 < runs.size() && runs[i + 2].op == ']' &&
                (runs[i + 1].op == '+' || runs[i + 1].op == '-')) {
                cellZero();
                i += 2;
                break;
            }
            cmpCellZero();
            b(0x0F); b(0x84); { size_t p = here(); w32(0);
                loops.push_back({ p, here(), r.pos }); }
            break;

        case ']': {
            if (loops.empty()) dieAt(r.pos, "unmatched ']'");
            Loop L = loops.back(); loops.pop_back();
            cmpCellZero();
            b(0x0F); b(0x85); size_t p = here(); w32(0);
            fixRel32(p, L.afterJe);      // jne 回跳到 '[' 之后
            fixRel32(L.jeDisp, here());  // je  前跳到 ']' 之后
            break;
        }
        default: break;
        }
    }
    if (!loops.empty()) dieAt(loops.back().srcPos, "unmatched '[' (missing ']')");

    // ---- 收尾：冲洗缓冲区、返回 0（进程退出码 = eax） ----
    callRel(flushOff);
    b(0x31); b(0xC0);                    // xor eax, eax
    addRsp(0x28);
    b(0xC3);                             // ret

    return code;
}

// ---------------------------------------------------------------- PE 映像组装

struct Section {
    std::string name;
    u32 vsize = 0;
    u32 rva = 0;
    u32 rawSize = 0;
    u32 rawPtr = 0;
    u32 chars = 0;
    std::vector<u8> raw;
};

static void put16(std::vector<u8>& b, size_t off, u16 v) { memcpy(&b[off], &v, 2); }
static void put32(std::vector<u8>& b, size_t off, u32 v) { memcpy(&b[off], &v, 4); }
static void put64(std::vector<u8>& b, size_t off, u64 v) { memcpy(&b[off], &v, 8); }

static std::vector<u8> buildImage(std::vector<Section>& secs, u32 entryRva,
                                  u32 importRva, u32 importSize,
                                  u32 iatRva, u32 iatSize) {
    const u32 optSize = 240;                       // PE32+ 可选头固定 240 字节
    const u32 nsec = (u32)secs.size();

    // ① 计算头部大小与各节 RVA / 文件偏移
    u32 hdrSize = DOS_HDR_SIZE + (PE_SIG_OFF - DOS_STUB_OFF) /*stub*/ + 4 /*签名*/ + 20 /*COFF*/
                + optSize + 40 * nsec;
    u32 hdrAligned = alignUp(hdrSize, FILE_ALIGN);
    u32 rva = alignUp(hdrAligned, SECTION_ALIGN);
    for (auto& s : secs) { s.rva = rva; rva += alignUp(s.vsize, SECTION_ALIGN); }
    u32 sizeOfImage = rva;

    u32 raw = hdrAligned;
    for (auto& s : secs) { s.rawPtr = raw; s.rawSize = alignUp(s.vsize, FILE_ALIGN); raw += s.rawSize; }

    std::vector<u8> img(raw, 0);

    // ② DOS 头（e_lfanew 指向 PE 签名；DOS 头 0x40 + stub 0x40 => PE 签名在 0x80）
    img[0] = 'M'; img[1] = 'Z';
    put32(img, 0x3C, PE_SIG_OFF);
    static const u8 stubCode[14] = {0x0E,0x1F,0xBA,0x0E,0x00,0xB4,0x09,0xCD,0x21,0xB8,0x01,0x4C,0xCD,0x21};
    memcpy(&img[DOS_STUB_OFF], stubCode, sizeof(stubCode));
    const char* stubMsg = "This program cannot be run in DOS mode.\r\r\n$";
    memcpy(&img[DOS_STUB_OFF + sizeof(stubCode)], stubMsg, strlen(stubMsg));

    // ③ PE 签名 + COFF 文件头
    size_t p = PE_SIG_OFF;
    put32(img, p, 0x00004550); p += 4;             // "PE\0\0"
    put16(img, p, 0x8664); p += 2;                 // Machine = AMD64
    put16(img, p, (u16)nsec); p += 2;
    put32(img, p, 0); p += 4;                      // TimeDateStamp = 0（可复现构建）
    put32(img, p, 0); p += 4;                      // PointerToSymbolTable
    put32(img, p, 0); p += 4;                      // NumberOfSymbols
    put16(img, p, (u16)optSize); p += 2;
    put16(img, p, 0x0022); p += 2;                 // EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE
    size_t opt = p;

    // ④ 可选头（PE32+）
    put16(img, opt + 0, 0x020B);                   // Magic = PE32+
    img[opt + 2] = 1;                              // MajorLinkerVersion（"bfc 1.0"）
    img[opt + 3] = 0;                              // MinorLinkerVersion
    put32(img, opt + 4,  secs[0].rawSize);         // SizeOfCode
    put32(img, opt + 8,  nsec > 1 ? secs[1].rawSize : 0);   // SizeOfInitializedData
    put32(img, opt + 12, 0);                       // SizeOfUninitializedData
    put32(img, opt + 16, entryRva);                // AddressOfEntryPoint
    put32(img, opt + 20, secs[0].rva);             // BaseOfCode
    put64(img, opt + 24, IMAGE_BASE);
    put32(img, opt + 32, SECTION_ALIGN);
    put32(img, opt + 36, FILE_ALIGN);
    put16(img, opt + 40, 6);  put16(img, opt + 42, 0);   // 最低 OS 版本 6.0
    put16(img, opt + 44, 0);  put16(img, opt + 46, 0);   // 映像版本
    put16(img, opt + 48, 6);  put16(img, opt + 50, 0);   // 最低子系统版本 6.0
    put32(img, opt + 52, 0);                       // Win32VersionValue
    put32(img, opt + 56, sizeOfImage);
    put32(img, opt + 60, hdrAligned);              // SizeOfHeaders
    put32(img, opt + 64, 0);                       // CheckSum
    put16(img, opt + 68, 3);                       // Subsystem = 3 (CONSOLE)
    put16(img, opt + 70, 0);                       // DllCharacteristics
    put64(img, opt + 72, 0x100000);                // SizeOfStackReserve
    put64(img, opt + 80, 0x1000);                  // SizeOfStackCommit
    put64(img, opt + 88, 0x100000);                // SizeOfHeapReserve
    put64(img, opt + 96, 0x1000);                  // SizeOfHeapCommit
    put32(img, opt + 104, 0);                      // LoaderFlags
    put32(img, opt + 108, 16);                     // NumberOfRvaAndSizes
    size_t dir = opt + 112;
    put32(img, dir + 8 * 1,  importRva);  put32(img, dir + 8 * 1 + 4,  importSize);
    put32(img, dir + 8 * 12, iatRva);     put32(img, dir + 8 * 12 + 4, iatSize);

    // ⑤ 节表
    size_t sh = opt + optSize;
    for (auto& s : secs) {
        memcpy(&img[sh], s.name.c_str(), std::min<size_t>(8, s.name.size()));
        put32(img, sh + 8,  s.vsize);
        put32(img, sh + 12, s.rva);
        put32(img, sh + 16, s.rawSize);
        put32(img, sh + 20, s.rawPtr);
        put32(img, sh + 36, s.chars);
        sh += 40;
    }

    // ⑥ 节内容
    for (auto& s : secs) {
        if (!s.raw.empty()) memcpy(&img[s.rawPtr], s.raw.data(), s.raw.size());
    }

    return img;
}

// ---------------------------------------------------------------- 主流程

static void usage() {
    printf(
        "bfc - Brainfuck -> Windows x86-64 (PE32+) compiler, direct machine-code emit\n"
        "\n"
        "usage: bfc <input.bf> [-o output.exe] [-c cells] [--dump]\n"
        "\n"
        "  -o <file>   output executable (default: input name with .exe)\n"
        "  -c <n>      tape cells, 1 byte each (default: %u)\n"
        "  --dump      print PE layout / machine-code hex\n"
        "  -h          show this help\n",
        DEFAULT_CELLS);
}

static std::string defaultOut(const std::string& in) {
    std::string s = in;
    size_t dot = s.find_last_of('.');
    size_t slash = s.find_last_of("/\\");
    if (dot != std::string::npos && (slash == std::string::npos || dot > slash)) s.erase(dot);
    return s + ".exe";
}

int main(int argc, char** argv) {
    std::string inPath, outPath;
    u32 cells = DEFAULT_CELLS;
    bool dump = false;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "-h" || a == "--help") { usage(); return 0; }
        else if (a == "-o") { if (++i >= argc) die("missing argument after -o"); outPath = argv[i]; }
        else if (a == "-c" || a == "--cells") {
            if (++i >= argc) die("missing argument after -c");
            cells = (u32)strtoul(argv[i], nullptr, 10);
        }
        else if (a == "--dump") dump = true;
        else if (!a.empty() && a[0] == '-') die("unknown option: " + a);
        else if (inPath.empty()) inPath = a;
        else if (outPath.empty()) outPath = a;
        else die("too many input files");
    }
    if (inPath.empty()) { usage(); return 1; }
    if (outPath.empty()) outPath = defaultOut(inPath);
    if (cells == 0) die("cells must be > 0");

    // ---- 读入源码 ----
    FILE* f = fopen(inPath.c_str(), "rb");
    if (!f) die("cannot open input file: " + inPath);
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n < 0) { fclose(f); die("cannot read input file"); }
    g_src.assign((size_t)n, '\0');
    if (n > 0 && fread(&g_src[0], 1, (size_t)n, f) != (size_t)n) { fclose(f); die("short read"); }
    fclose(f);

    // ---- ① 词法过滤 + ② 指令合并 ----
    std::vector<Run> runs;
    for (size_t i = 0; i < g_src.size(); i++) {
        char c = g_src[i];
        switch (c) {
        case '>': case '<': case '+': case '-':
            if (!runs.empty() && runs.back().op == c) runs.back().count++;
            else runs.push_back({ c, 1, i });
            break;
        case '.': case ',': case '[': case ']':
            runs.push_back({ c, 1, i });
            break;
        default:
            break;   // 其它字符一律视为注释
        }
    }

    // ---- ③ 代码生成（两遍：第一遍拿代码长度，定出 .data 的 RVA，再生成最终代码） ----
    DataLayout probe = layoutData(0, cells);
    CodeGen g1(probe);
    std::vector<u8> codeProbe = g1.compile(runs);

    u32 dataRva = TEXT_RVA + alignUp((u32)codeProbe.size(), SECTION_ALIGN);
    DataLayout data = layoutData(dataRva, cells);

    CodeGen gen(data);
    std::vector<u8> code = gen.compile(runs);
    if (code.size() != codeProbe.size()) die("internal error: unstable code size");

    // ---- ④ 组装映像 ----
    std::vector<u8> dataBytes = buildData(data);

    Section text;
    text.name = ".text";
    text.vsize = (u32)code.size();
    text.chars = 0x60000020;          // CNT_CODE | MEM_EXECUTE | MEM_READ
    text.raw = code;

    Section dseg;
    dseg.name = ".data";
    dseg.vsize = data.size;
    dseg.chars = 0xC0000040;          // CNT_INITIALIZED_DATA | MEM_READ | MEM_WRITE
    dseg.raw = dataBytes;

    std::vector<Section> secs = { text, dseg };
    std::vector<u8> image = buildImage(secs, TEXT_RVA, data.importDesc,
                                       20 * 2, data.iat, 8 * (NUM_IMPORTS + 1));

    // ---- 写出 ----
    FILE* o = fopen(outPath.c_str(), "wb");
    if (!o) die("cannot create output file: " + outPath);
    if (fwrite(image.data(), 1, image.size(), o) != image.size()) { fclose(o); die("write failed"); }
    fclose(o);

    if (dump) {
        printf("--- layout -------------------------------------------------\n");
        printf("  file            : %s  (%zu bytes)\n", outPath.c_str(), image.size());
        printf("  .text           : rva 0x%04X  vsize %u  raw %u  @0x%04X\n",
               secs[0].rva, secs[0].vsize, secs[0].rawSize, secs[0].rawPtr);
        printf("  .data           : rva 0x%04X  vsize %u  raw %u  @0x%04X\n",
               secs[1].rva, secs[1].vsize, secs[1].rawSize, secs[1].rawPtr);
        printf("  entry rva       : 0x%04X\n", TEXT_RVA);
        printf("  tape            : rva 0x%04X  (%u cells)\n", data.tape, cells);
        printf("  outbuf          : rva 0x%04X  (%u bytes)\n", data.outbuf, OUTBUF_SIZE);
        printf("  iat             : rva 0x%04X  ilt rva 0x%04X\n", data.iat, data.ilt);
        printf("  import desc     : rva 0x%04X  dll %s\n", data.importDesc, IMPORT_DLL);
        printf("--- machine code (first 256 bytes) -------------------------\n");
        for (size_t i = 0; i < code.size() && i < 256; i++) {
            if (i % 16 == 0) printf("  %04zX: ", i);
            printf("%02X ", code[i]);
            if (i % 16 == 15) printf("\n");
        }
        if (code.size() % 16) printf("\n");
        if (code.size() > 256) printf("  ... (%zu bytes total)\n", code.size());
    }

    printf("bfc: %s -> %s  [direct x86-64 -> PE32+, no C/asm/link step]  "
           "code %zu B, data %u B, exe %zu B\n",
           inPath.c_str(), outPath.c_str(), code.size(), data.size, image.size());
    return 0;
}
