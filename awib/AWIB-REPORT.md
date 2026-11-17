# 参考项目 awib 分析 + 与本项目的实测对接报告

对象：[matslina/awib](https://github.com/matslina/awib) —— Mats Linander 写的、**完全用 Brainfuck 实现的 Brainfuck 编译器**（GPL-3.0）。
本地副本：`awib/awib-master/`（master 快照，含 `formats/awib-0.1..0.3`、`frontend/`、6 套 backend、`systest/`、Python 测试）。

本文件记录三件事：**(1) awib 是怎么做到自举的；(2) 它的原生后端怎么解决"循环回填"——正是本项目 bfc0 卡住的地方；(3) 用本项目的 `bfc.exe` 实际编译并验证 awib 的完整结果。**

---

## 1. awib 是什么

| 项目 | 内容 |
|---|---|
| 实现语言 | Brainfuck（源码 `awib.b`，由 skeleton + frontend + 各 backend 拼装） |
| 目标（7 个） | `386_linux`（i386 Linux ELF 可执行文件）、`lang_c`、`lang_go`、`lang_java`、`lang_ruby`、`lang_rust`、`lang_tcl`（默认 `lang_c`） |
| 前端优化 | 指令序列合并（`----`→SUB(4)）、互相抵消（`+++-->><`→`+>`）、`[-]`→SET(0)、"永不进入的循环"删除、复制/乘法循环→LMUL/RMUL |
| 目标选择 | 输入首行 `@TARGET` |
| 运行期要求 | 单元 8 位；`,` 在输入结束后写 0 / 写 -1 / **不改** 三种都接受；≥ 65535 单元 |
| 它生成的代码要求 | 单元 8 位回绕；`,` 遇 EOF **不改**；≥ 65535 单元 |

---

## 2. 自举原理：polyglot（同一个文件既是 BF 也是 C / bash / Tcl）

全部技巧的根基只有一条：**Brainfuck 解释器会忽略一切非指令字符**。于是"注释"里可以塞别的语言的代码。

`util/bfpp.py` 三步：

1. `preprocess()` —— 递归展开 `#include(FILENAME)`，然后 **只保留 8 个 BF 字符**：
   `data.append(''.join(c for c in line if c in BFIS))`（`bfpp.py:129`）。
   所以 `.b` 源文件里那些 C/bash/Tcl 代码，对 BF 视角而言什么都不是。
2. `format()`（`bfpp.py:132-152`）—— format 文件（`formats/awib-0.3`）只提供"版式"：空格照抄、换行照抄，**其它每一个字符位置依次被替换成一条 BF 指令**。
3. `attach_interpreters()`（`bfpp.py:77-109`，POLYGLOT 模板）—— 把整理好的 BF 代码包成：

```
#define EOF char* /* !\
cat >>/dev/null <<EOF
set K {
[]*/
EOF
K="\
<…整个 BF 程序，每行末尾是 " \" + 换行…>";
        # bash / C / Tcl 的解释器分别由 #if 0 / heredoc / 括号 掩蔽
```

结果就是一个 4 语言 polyglot：

* **BF 视角**：`K="` 之类的字符不是指令，被忽略；字符串里的 `+-<>[].,` 就是程序本体。
* **C 视角**：`K` 是一个（靠行末 `\` 续行拼起来的）超长字符串；之后跟着一段**用 C digraph（`<: :> <% %>`）+ 异或混淆常量**写成、刻意不含任何 BF 字符的 C 版 BF 解释器（`INT_C`，`bfpp.py:37-47`）。
* **bash 视角**：`K="..."` 是变量赋值，`INT_BASH` 是真解释器，其余用 heredoc/`#` 吃掉。
* **Tcl 视角**：`set K { ... }` 是括号词，`INT_TCL` 是解释器，`exit` 收尾。

所以自举链条是（README 原文命令）：

```
$ cp awib-0.2.b awib-0.2.c
$ gcc awib-0.2.c -o awib.tmp          # 用宿主 C 编译器做"种子"
$ ./awib.tmp < awib-0.2.b > awib-0.2.c # 种子编译器编译自己的源码
$ gcc -O2 awib-0.2.c -o awib          # 得到最终的 awib
```

**要点**：awib 从不假装"从零开始"——它用宿主编译器造第一颗种子，之后编译器自己编译自己。bash / tclsh 也能直接当宿主。

---

## 3. 编译器内部结构（`DEVEL`）

* **前端**（BF 程序）：读 BF 源码 → **优化** → 输出定长字节码 IR，整块留在磁带上。
  后置条件：`% T *0 b (code) 0 M m`（T=后端编号，b=正确性标志，M=最大循环深度）。
* **后端**（BF 程序）：从磁带上消费字节码，逐条展开成目标代码，**流式向前输出**。

字节码（9 种 op，2 或 4 字节）：

| op | 编码 | 语义 | op | 编码 | 语义 |
|---|---|---|---|---|---|
| ADD(x) | `01 x` | 当前单元 += x | SET(x) | `09 x` | 当前单元 = x |
| INPUT | `02 00` | 读一个字节 | LMUL(x,y) | `10 x 11 y` | 左 x 单元 += 当前×y |
| SUB(x) | `03 x` | 当前单元 -= x | RMUL(x,y) | `12 x 13 y` | 右 x 单元 += 当前×y |
| OUTPUT | `04 00` | 输出当前单元 | OPEN / CLOSE | `07 00` / `08 00` | 循环 |
| LEFT(x)/RIGHT(x) | `05 x` / `06 x` | 指针移动 | | | |

后端的翻译是"查表展开"，例如 i386（`386_linux/DEVEL` 表 1）：

| op | i386 机器码 |
|---|---|
| ADD(1) | `FE 01`（`inc byte [ecx]`） |
| RIGHT(1) | `41`；RIGHT(x), x>2 → `83 C1 x` |
| **OPEN** | `3A 39`（`cmp bh,[ecx]`）+ `0F 84 0000 xxxx`（`je rel32`） |
| **CLOSE** | `3A 39` + `0F 85 FFFF zzzz`（`jne rel32`） |
| SET(x) | `C6 01 x` |
| RMUL(x,y) | `B0 y` / `F6 21` / `00 41 ww` |

---

## 4. 关键：awib **不需要回填**，这正是 bfc0 卡住的地方

本项目 `bfsrc/bfc0.bf` 的障碍是：`[` / `]` 的前后跳转要**位置回填**，PE 头里的 `SizeOfImage` 等字段要先知道总长；在 BF 里既不能 seek 输出流，也不方便做多字节算术。

awib 的答案是**根本不做回填**，靠三件事：

1. **输入（字节码）整块留在磁带上** —— 后端可以任意重新扫描它，甚至来回走（走多远、走回来多少都是自己数的）。
2. **每条 op 的输出长度静态已知** —— 循环跳转强制用 32 位相对位移（`h.asm:4`：`2010-02-22 Load at standard address. 32 bit relative jumps for loops.`），所以 OPEN/CLOSE 固定 6 字节，ADD 固定 2 字节……
3. **需要提前知道的量，靠"预扫描求和"得到** ——
   * OPEN 的 Y = 从 OPEN 往后扫到匹配 CLOSE 为止，把各 op 的输出长度加起来（`386_linux/DEVEL:79-83`）；
   * ELF / program header 里的 `fsize`（`h.asm:48-49` 的 `dd fsize`）= 对整个字节码求和。

于是输出永远是一次性的**向前发射**：没有 linker、没有回填、不需要输出缓冲区、不需要临时文件。

**为什么它选 ELF 而不是 PE**：i386 静态 ELF（无 section header、无动态链接）需要"算"的字段极少，基本只有 `fsize`，BF 里一个 32 位加法就够；PE 虽然同样可以靠预扫描得到全部字段，但 `SizeOfHeaders` / `SizeOfCode` / `SizeOfImage` / 文件对齐与内存对齐等要在 BF 里做多字节算术，代价高得多。这也解释了 awib 的 386 后端为什么要 12610 字节 BF，而 `lang_c` 后端只要 3653 字节——**文本后端根本不用算偏移**，直接输出 `while(*p){` / `}`。

---

## 5. 用本项目 bfc.exe 编译 awib：实测结果

环境：Windows x86-64；`g++`/`gcc`（Strawberry Perl 自带 GNU 工具链）；Python 3.14；本项目 `bfc.exe`；`bash.exe`（WSL）。

### 5.1 构建
```
python util/bfpp.py --interpreter --format formats/awib-0.3 --width 79 awib-skeleton.b > awib.b
```
→ `awib.b` = **48535 字节 / 39273 条 BF 指令**。必须保证纯 LF：`bfpp.py` 在 Windows 上默认以文本模式写 stdout，会把 618 个 `\n` 变成 `\r\n`（多出 618 个 0x0D）——`\`+换行的续行会被 0x0D 破坏，所以要先 `sys.stdout.reconfigure(newline='\n')` 再运行它（BF 本身会忽略 0x0D，但文件应当与上游/Linux 产物完全一致）。

```
bfc.exe awib\awib.b -o awib\awib.exe -c 70000
```
→ 退出码 0，**耗时 0.02 秒**，输出 **276480 字节 PE32+**（bfc 自报 code 200906 B / data 74264 B）。0.06 秒编译一个 hello。

### 5.2 与独立参考实现交叉验证
用 awib 自带的 `util/bfint.c`（C 写的 BF 解释器，65535 单元）跑同一个 `awib.b`，与本项目的 `awib.exe` 比较：**逐字节相同**（唯一差异是 `bfint` 用 CRT `putchar`，在 Windows 上把 `\n` 写成 `0d 0a`；本项目的 `.` 直接写句柄，输出原始字节——这一点上 bfc 更符合 BF 语义）。

### 5.3 awib 自带 systest：**6 / 6 逐字节通过**
链路：`awib.exe` → 生成 C → `gcc` → 运行（输入用 `systest/*.in`）→ 与 `systest/*.out` 比较，stdout 用构造函数 `_setmode(_O_BINARY)` 关掉文本模式：

| 用例 | 期望输出 | 结果 |
|---|---|---|
| basicops | 9 B | PASS（9 B） |
| factor | 18 B | PASS（18 B） |
| endoffile | 2 B | PASS（2 B） |
| comments / empty / memoryhog | 0 B | PASS ×3 |

### 5.4 自举链（我们这条链里没有任何 C 写成的 BF 编译器）
```
bfc.exe (C++ 种子)  →  awib.exe（原生 Windows，由 BF 源码生成）
awib.exe < awib.b   →  awib_gen.c（106635 B）
gcc -O2 awib_gen.c  →  awib2.exe（154776 B，7.3 s）
```
* `awib.exe` 与 `awib2.exe` 对 `examples` 输入产生的 C **逐字节相同**（hello 802 B；factor 6928 B）。
* `bfc.exe → awib.exe` 这一步是"用 C++ 编译器编译出一个完全由 BF 写成的编译器"；`awib.exe → awib2.exe` 这一步是 awib 的自举。

### 5.5 跨平台
`awib.exe` 以 `@386_linux` 为目标 → 输出 **376 字节 i386 ELF**（魔数 `7f 45 4c 46`），在 WSL 里 `chmod +x` 后运行输出 `Hello World!`。
即：**Brainfuck 源码 →（本项目 bfc 编译的 awib）→ Linux 可执行文件 → 在 Linux 上跑通**。

### 5.6 环境参数实测
* README 要求 ≥ 65535 单元，我们用 `-c 70000`；作为对照又用默认 `-c 30000` 编了一份，在全部 6 个 systest 源 + hello 上输出**完全相同**（语料没有触及深磁带）。生产使用仍建议 `-c 70000`。
* Python 生成 `awib.b` 时必须写**纯 LF**：C/bash 的 polyglot 依赖行末 `\` 续行，CR 会破坏它。

---

## 6. 对本项目的三条结论

1. **bfc.cpp 的能力已经被第三方真实项目验证**：39273 条指令的 awib 一次编译通过、6/6 逐字节对上 awib 自己的测试，还能驱动 awib 自举并跨编译出可在 Linux 运行的 ELF。这是目前最有力的正确性证据。
2. **想做"纯 BF 写的原生代码生成器"，正确的路是 awib 这条路**：定长 op 编码 + 字节码驻留磁带 + 预扫描/重扫求偏移与总长，目标选**字段最少的静态 ELF**；不要在 BF 里做输出回填，也不要在 BF 里硬啃 PE 的多字节对齐字段。
3. **想彻底自举但不在 BF 里做机器码**：让 BF 写的编译器输出**文本**（C），由宿主 `gcc` 收尾——awib 的 `lang_c` 后端只用 3653 字节 BF，却拿到了完整的原生性能。本项目 `bfsrc/t2bf.bf`（353 字节）走的是同一思路的极简版。

---

## 7. 产物与复现

| 路径 | 说明 |
|---|---|
| `awib/awib.exe` | **可用的 Windows 原生 awib 交叉编译器**（276480 B，由 bfc.exe 从 BF 源码生成），0.06 s/次，7 个目标 |
| `awib/awib.b` | 拼装好的 awib BF 源码（48535 B / 39273 指令，纯 LF） |
| `awib/verify_awib.ps1` | 一键复现本报告全部结论（构建 → 编译 → systest → 自举 → ELF），16 项检查，全绿退出码 0 |
| `awib/awib-master/` | awib 源码快照（缺失时脚本自动从 codeload 重新下载） |
| `awib/work/` | 中间产物（hello/*.c、awib_gen.c、awib2.exe、各 systest 产物） |

一键复现（本机 shell 是 Windows PowerShell 5.1，`pwsh` 不在 PATH 上，用 `powershell` 调用）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\git\brainfuck-compiler\awib\verify_awib.ps1
```

实测输出：**16/16 通过**——`build awib.b`（48535 B，CR=0）、`bfc.exe compiles awib.b`（0.02 s，276480 B）、`hello world`、`matches reference bfint`、`systest 6/6`、`self-host stage1 == stage2`、`i386 ELF` + WSL 运行、5 个文本后端。
