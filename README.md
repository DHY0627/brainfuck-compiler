# bfc —— Brainfuck 编译器（直接产出 Windows x86-64 .exe）

把 `.bf` 源码编译成**可直接双击运行的控制台 .exe**。全程不经过任何中间转译：
不生成 C/汇编源码，不调用汇编器、链接器，不链接 C 运行时，也不依赖任何第三方库。
**x86-64 机器码由 C++ 手写编码直接发射，PE32+ 映像（DOS 头 / COFF 头 / 可选头 /
节表 / 导入表）由本程序自己逐字节组装。**

```
.bf 源码
  │ ① 词法过滤     只保留 8 个指令，记录源码位置用于报错
  │ ② 语法检查     括号配对；配对失败时回填跳转目标
  │ ③ 机器码生成   手写 x86-64 指令编码 -> 内存中的 .text 缓冲区
  │ ④ 导入表/节表  常量池、ILT/IAT、hint-name、导入描述符
  ▼
.exe（PE32+，Subsystem = 3 控制台，仅导入 kernel32 的 3 个函数）
```

---

## 快速开始

```bat
build.bat                                  :: 用 g++ 编译出编译器本身
bfc.exe examples\hello.bf                  :: -> examples\hello.exe
hello.exe                                  :: Hello World!
```

> 不想自己编译？[Releases](https://github.com/DHY0627/brainfuck-compiler/releases)
> 里有打包好的 `bfc.exe` 和全部 Brainfuck 写的编译器。
> 一键构建全部产物（含自举冒烟测试）：`build_all.bat`。

命令行：

```
bfc <input.bf> [-o output.exe] [-c cells] [--dump] [-h]

  -o <file>   输出可执行文件（默认把输入名的扩展名换成 .exe）
  -c <n>      磁带单元数，每格 1 字节（默认 30000）
  --dump      打印 PE 布局与机器码十六进制
  -h          帮助
```

手动构建（MinGW-w64 g++，其它编译器同理）：

```bat
g++ -std=c++17 -O2 -Wall -Wextra -static -static-libgcc -static-libstdc++ -o bfc.exe bfc.cpp
```

---

## 一、生成的机器码长什么样

寄存器约定（生成的代码就是**进程入口**，不需要对任何调用者负责，但仍要满足
WinAPI 对"非易失寄存器"的要求）：

| 寄存器 | 用途 |
|---|---|
| `rbx` | 数据指针，指向当前单元 |
| `r12` | 输出缓冲区中已填充的字节数 |
| `rsp` | 入口处 `sub rsp, 0x28`：32 字节影子空间 + 8 字节保持 16 字节对齐；`[rsp+0x20]` 兼作第 5 个参数的槽位 |

Brainfuck → x86-64 指令对照（连续相同指令会先做游程合并）：

| BF | 生成指令 | 字节 |
|---|---|---|
| `+` | `inc byte [rbx]` | `FE 03` |
| `+++…`(n>1) | `add byte [rbx], n` | `80 03 nn` |
| `-` | `dec byte [rbx]` | `FE 0B` |
| `-`×(n>1) | `sub byte [rbx], n` | `80 2B nn` |
| `>` / `>`×n | `inc rbx` / `add rbx, n` | `48 FF C3` / `48 81 C3 nn…` |
| `<` / `<`×n | `dec rbx` / `sub rbx, n` | `48 FF CB` / `48 81 EB nn…` |
| `[` | `cmp byte [rbx], 0` + `je rel32`（目标暂时留空，遇到 `]` 回填） | `80 3B 00` `0F 84 …` |
| `]` | `cmp byte [rbx], 0` + `jne rel32`（回跳到 `[` 之后） | `80 3B 00` `0F 85 …` |
| `[-]` / `[+]` | `mov byte [rbx], 0`（窥孔优化，不再真的循环） | `C6 03 00` |
| `.` | 取当前单元 → 存入 4KB 输出缓冲区；缓冲区满则调用 flush | `0F B6 03` … |
| `,` | 先 flush，再 `ReadFile` 读 1 字节写入当前单元；EOF 写 0 | … |
| 程序结束 | `call flush` + `xor eax,eax` + `ret`（进程退出码 0） | |

地址全部用 **RIP 相对寻址**（`lea rbx, [rip+disp32]`、`call qword [rip+IAT]`），
所以映像放到任何加载基址都能跑；导入调用走 IAT，由加载器填充真实地址。

`.`/`,` 通过 `GetStdHandle` + `WriteFile`/`ReadFile` 实现。`.` 不是一字节一次系统调用：
先在 `.data` 里积攒到 4096 字节再一次性写出，`,前`会先冲洗缓冲区以保证交互顺序。

### 代码布局（为什么 flush 例程放在主体之前）

```
entry : xor r12d,r12d / sub rsp,0x28 / lea rbx,[rip+tape]
        jmp body          <- 跳过下面的例程
flush : ... WriteFile 冲洗 outbuf ... ret
body  : ... BF 主体 ...    call flush / xor eax,eax / add rsp,0x28 / ret
```

把 `flush` 放在 `body` 之前，`. ` 处的 `call flush` 目标在生成时就已经确定，
不必回填；只有入口那条 `jmp body` 需要在 `body` 起点确定后回填一次。

---

## 二、生成的 PE 映像

两节，节与文件偏移都按 0x1000 / 0x200 对齐：

| 节 | RVA | 权限 | 内容 |
|---|---|---|---|
| `.text` | 0x1000 | R-X（`0x60000020`） | 机器码 |
| `.data` | 0x2000 起 | RW-（`0xC0000040`） | 磁带、输出缓冲、IAT/ILT、hint-name、导入描述符 |

`.data` 内部布局（RVA 由 `layoutData()` 统一计算，代码里的 RIP 相对位移直接引用）：

```
+0x0000  tape        30000 字节，全 0（BF 要求初始为 0）
+0x7530  outbuf      4096  字节输出缓冲
+0x8530  inp         1     字节，ReadFile 落点
+0x8534  written     4     字节，lpNumberOfBytesRead/Written
+0x8538  iat         4×8   导入地址表（含 NULL 结束项，加载器写入真实地址）
+0x8558  ilt         4×8   导入查找表（内容同 IAT 初值）
+0x8578  hint/name   3 条  WORD hint=0 + ASCIIZ 名称
+0x85A0  "KERNEL32.dll"
+0x85B0  import desc 40    1 个描述符 + 1 个全零结束描述符
                     （节虚拟大小 0x85D8）
```

头部字段：`Machine=0x8664`、`Magic=0x20B`(PE32+)、`Subsystem=3`、
`ImageBase=0x140000000`、`SizeOfImage=0xB000`、`DllCharacteristics=0`、
`TimeDateStamp=0`（因此**构建可复现**，同一输入两次编译结果逐字节相同）。

导入表是唯一需要"手算"的部分：加载器会从 `DataDirectory[1]` 找到描述符，
顺着 ILT 读出每个名称，在 `KERNEL32.dll` 里解析后写回 IAT。ILT 与 IAT 都必须
以**全零项结尾**（N+1 项）——这正是第一版里被加载器拒绝的那个 bug。

---

## 三、验证

```bat
python tests\run_tests.py
```

测试脚本做四件事，并且**不信任编译器自己的说法**：

1. **端到端**：编译 → 运行，逐字节比对 stdout 与进程退出码（含 stdin 输入、
   EOF 行为、NUL 终止、UTF-8 BOM + CRLF + 中文注释、7000 字节输出强制多次刷新、
   `.text` 超过 4KB 触发第二个 RVA 页等边界情况）。
2. **独立 PE 校验**：`tests/pecheck.py` 用 `struct` 从头解析生成的 exe，检查
   DOS/PE 签名、可选头字段、节对齐、入口点是否落在可执行节、以及能否顺着导入表
   走到 `KERNEL32.dll` 的三个函数名。即使 exe 能跑，也要确认它是**真 PE**。
3. **差分测试**：内置一个 30 行的参考解释器作为基准答案，对 13 个手工程序 +
   随机生成程序逐个比对输出——编译器与解释器结果必须完全一致。
4. **"无中间步骤"的反证**：把 `PATH` 清空后再调用 `bfc.exe`。既然它在没有任何
   外部程序的搜索路径下仍能产出可运行的 exe，就不可能偷偷调用了 gcc/as/link。
   同时还检查了 `--dump` 之外不产生任何中间文件、以及重复构建逐字节一致。
5. **BF 写的编译器**：编译 `bfsrc/t2bf.bf` → `t2bf.exe`，比对它生成的 BF 与
   独立参考模型，再把生成的 BF 二次编译成 exe 运行，验证往返输出一致。
6. **自举链**：`bootstrap/selfhost.py` 让 `bfc.exe` 编译 `bfsrc/bfc1.bf`，
   再用 `bfc1.exe` 编译它自己、由 gcc 构建 `stage1.exe`、再编译一次自己，
   检查两次产出的 C **逐字节固定**（19 项检查）。
7. **不用 gcc 的自举链**：`bootstrap/selfhost_pak.py` 让 `bfc2.exe`（纯 BF
   写出完整 exe 字节流的编译器）编译它自己，检查两代 exe 逐字节固定，
   并拿它编译 48 KB 的 awib 源码、与原生 awib 对拍（29 项检查）。

实测：

```
tests\run_tests.py                       144 checks, 0 failed
bootstrap\run_bootstrap_tests.py          57 checks, 0 failed
bootstrap\selfhost.py                     19 checks, 0 failed
tools\verify_bfc2.py                      11 checks, 0 failed
tools\test_pak.py                         26 checks, 0 failed
tools\test_pak_large.py                    6 checks, 0 failed
bootstrap\selfhost_pak.py                 29 checks, 0 failed
awib\verify_awib.ps1                      16 checks, 0 failed
```

性能（`+`×100 四层嵌套，共 10^8 次单元自增）：

```
inner increments: 100000000,  wall: 0.679 s  (147.4 Mops/s)
```

这个数量级只可能来自原生机器码：解释器通常要慢一到两个数量级。

---

## 四、用 Brainfuck 写的编译器

`bfsrc/` 下有**四个用 Brainfuck 写的编译器**，都能被 `bfc.exe` 编译成原生 exe：

| 程序 | 大小 | 编译什么 | 输出 | 谁编译它 |
|---|---|---|---|---|
| `bfsrc/t2bf.bf` | 352 B | 自由文本 | 能打印该文本的 BF 程序 | `bfc.exe` |
| `bfsrc/bfc0.bf` | **30 KB** | **Brainfuck** | **完整可运行的 PE32+ .exe** | `bfc.exe` |
| `bfsrc/bfc1.bf` | **28 KB** | **Brainfuck** | **C 源码（再由 gcc 出原生 exe）** | `bfc.exe` → 自己 → 自己 |
| `bfsrc/bfc2.bf` | **66 KB** | **Brainfuck** | **完整可运行的 PE32+ .exe（纯 BF 写出，不用 gcc）** | `bfc.exe` → 自己 → 自己 |

`bfc0.bf` 是正题：它从 stdin 读一段 Brainfuck 程序，向 stdout 写一个**完整、
可直接运行的 Windows 控制台 exe**——不需要任何后续处理。

### 完整链条

```
                    bfc.exe  (C++，手写机器码 + 手工 PE)
                       │  编译
                       ▼
   bfsrc/bfc0.bf  ──────────>  bfsrc/bfc0.exe          ← 编译器本身，30KB BF → 297KB exe
   （30 KB 纯 Brainfuck）           │
                                    │  编译 stdin 上的 BF 程序
                                    ▼
                            prog.exe（完整 PE32+）      ← 直接双击可运行
```

一条命令跑通：

```bat
bfc.exe bfsrc\bfc0.bf -o bfsrc\bfc0.exe
bfsrc\bfc0.exe < examples\bfc0_hello.bf > hello.exe
hello.exe                         :: Hello
```

### bfc0 是怎么在 Brainfuck 里做到"生成机器码"的

**关键设计：让机器码里没有一个字节依赖位置。** 于是 BF 端不需要任何地址
运算、不需要回填、不需要重定位，只需要"按表输出常量字节"。

* 所有对外部数据的引用都用**绝对地址**（`mov rax, [imm64]` / `mov r9, imm64`），
  因为映像固定加载在 `0x140000000`（不开 ASLR）。若用 `lea rax,[rip+disp]`
  这类相对寻址，位移就会随代码位置变化，BF 端根本算不出来。
* 每条 BF 指令对应一段**固定字节序列**：

  | BF | 机器码 | 长度 |
  |---|---|---|
  | `+` | `FE 03`（`inc byte [rbx]`） | 2 |
  | `-` | `FE 0B` | 2 |
  | `>` | `48 FF C3`（`inc rbx`） | 3 |
  | `<` | `48 FF CB` | 3 |
  | `.` | `GetStdHandle` + `WriteFile` 序列 | 68 |
  | `,` | `GetStdHandle` + `ReadFile` + EOF 判断 | 106 |
  | 程序结束 | `C3`（`ret`，返回 stage-0 存根） | 1 |

* **常量池技巧**：把用到的每个字节值放进一个专用磁带单元。要输出字节 `v`，
  就把指针移到装着 `v` 的那个单元再 `.`。Brainfuck 的 `.` 输出的是当前单元的
  值，所以"打印任意常量字节"变成了一次移动加一个点。
* **6 路分派**：每个源字节依次与 6 个指令字符比较。比较用经典的
  "复制 + 相减 + 标志位" 惯用法；命中后把当前字符污染成 1，后续比较自然全部失败：

  ```
  [->+>+<<]      把 c 复制到 t 和 tmp
  >>[-<<+>>]     从 tmp 恢复 c
  <-----------   t -= K（K = 该指令的 ASCII 值）
  >+<            flag = 1
  [>[-]<[-]]     若 t != 0：清掉 flag 和 t   → flag = (c == K)
  >[             命中：
      <输出该指令的固定机器码序列>
      <<[-]+>>     把 c 污染成 1
      [-]          清 flag
  ]
  ```

* bfc0 自己**也把整个 PE 模板（头部、入口存根、导入表，共 4864 字节）
  当成常量逐字节输出**，然后接上编译出的代码，再补零——所以它的输出就是成品 exe。

### 生成的 exe 的内存布局

```
0x140001000  .text  RVA 0x1000  RWX
   0x1000    入口存根（53 字节）：拷贝代码 → 装入磁带指针 → call → ExitProcess
   0x1040    被拷贝过来的已编译代码（最多 0x800 字节）
   0x1900    数据磁带 30000 字节
0x14000A000  .rdata RVA 0xA000  RW
   0xA000    导入描述符 / ILT / IAT（ExitProcess, GetStdHandle, WriteFile, ReadFile）
   0xA0C8    written（写入字节数）、0xA0CC 输入缓冲
   0xA100    暂存的已编译代码（存根从这里拷贝）
```

退出码 = 程序结束时指针所指单元的值，所以无 I/O 的程序也能直接观察计算结果。

> 这里踩过一个真实的坑：磁带原本放在 `0x1500`，而代码区是
> `0x1040 + 0x800 = 0x1840`——**两者重叠**。小程序碰巧没事，代码一超过
> 0x4C0 字节就会覆盖自己的磁带而崩溃。现在磁带移到 `0x1900`，并在
> `gen_mkstage0.py` 里用断言把这个不变量钉住了。

### 支持范围

* **支持**：`+ - > < . ,`，任意长度（机器码上限 2048 字节，约 600–1000 条指令）。
* **不支持循环**：`[` 与 `]` 会被编译成 `ud2`（`0F 0B`），程序**立刻崩溃**而不是
  悄悄算出错误结果。这是刻意的：流式编译器无法回填前向跳转，而 stage-0 的
  0x800 字节代码区也放不下运行时链接器。
* 因此 **bfc0 还不算自举**：`bfc0.bf` 自己含循环，而它编译不了循环。
  要补上这一步，需要在 stage-0 存根里加一个扫描代码、配对括号、回填跳转槽的
  "运行时链接器"，并把每条指令的编码改成定长记录。这一步后来没有作为 bfc0
  的补丁实现——因为"输出直接到控制台"的新要求把回填整条路堵死了，
  真正的自举改由下面的 `bfc2` 完成（`bfc1` 是中间一步）。

### 验证

```bat
python bootstrap\run_bootstrap_tests.py     :: 57 项检查
python tools\verify_bfc0.py                 :: 用 Python 模拟 bfc0.bf，与模型逐字节比对
```

* **编译前先模拟**：`verify_bfc0.py` 直接解释 30 KB 的 `bfc0.bf`，把它的输出与
  `tools/bfcodegen.py` 里的参考模型逐字节比较。Brainfuck 不可读，这一步能把逻辑
  错误在交给 `bfc.exe` 之前就抓出来。
* **端到端**：`bfc.exe` 编译 `bfc0.bf` → `bfc0.exe`；`bfc0.exe` 编译 10 个测试
  程序 → 每个产物都过独立 PE 校验器、与参考解释器比 stdout、比退出码。
* **三方差分**：`bfc0.exe` 的输出、Python 模型、`bfc.exe` 的输出三者一致
  （见 `examples/bfc0_hello.exe` 与 `examples/bfc0_hello_bfc.exe`）。
* 大程序回归测试：1341 字节机器码的用例专门锁住上面那个磁带重叠 bug。

### bfc1：中间一步——输出 C 源码（需要 gcc）

`bfc0` 不能自举的根本原因不只是"不支持循环"：它**把整个产物缓冲在磁带里，
最后一次性输出**，这样才有机会回填 `[` / `]` 的跳转地址。当要求"边读边写、
直接输出到控制台"时，回填就不可能了——机器码里到处是只能事后才知道的地址
（跳转位移、PE 节偏移、导入表 RVA）。

所以 `bfc1` 换了目标语言：**输出 C 源码**。循环在 C 里天然配对：

```
[  ->  while(m[p]){
]  ->  }
```

于是 `[` 与 `]` 之间隔着多少字节、跳转到哪里，全部由 gcc 的 C 前端负责，
BF 端一个地址都不用算、不需要前瞻、不需要回填。整个编译器退化成一张
"逐字符查表、输出固定模板"的翻译器，磁带只需要 **12 个单元**（与源程序
大小无关）；机器码交给 gcc——这是自举链的标准构造，链里没有任何 C 写的
Brainfuck 编译器。完整推理见 [`docs/SELFHOST.md`](docs/SELFHOST.md)。

```
bfc.exe            (C++ 种子)
  │  编译 bfsrc/bfc1.bf（28647 字节纯 BF）
  ▼
bfc1.exe           (原生 BF → C 编译器，由 BF 源码生成)         0.030 s
  │  bfc1.exe < bfsrc/bfc1.bf
  ▼
stage1.c           (编译器编译自己，258976 字节 C)              0.333 s
  │  gcc -O1
  ▼
stage1.exe         (从自己产出的 C 构建出的原生编译器，76688 B)   1.294 s
  │  stage1.exe < bfsrc/bfc1.bf
  ▼
stage2.c           (新编译器再次编译自己)                        0.782 s
```

**固定点**：`stage2.c` 与 `stage1.c` **逐字节相同**；`stage1.exe` 与
`bfc1.exe` 编译任意程序得到的 C 也逐字节相同。这就是自举成立的证明。

```bat
python tools\verify_bfc1.py      :: 在 Python 里解释 bfc1.bf，9 个用例与模型逐字节一致
python bootstrap\selfhost.py     :: 完整自举链 + 固定点 + 4 个示例程序，19 项检查
```

> `bfc1` 的自举把机器码交给了 gcc，严格说"不算数"。真正满足"完全不用 gcc"
> 的是下面这个 `bfc2`；`bfc1` 的价值在于先用最简单的目标把
> "流式输出 + 固定点"这套骨架跑通——换回机器码目标时骨架原样复用。

### bfc2：不用 gcc 的自举——直接把 exe 字节写到控制台

`bfc2.bf` 输出的是**成品 exe 的完整字节流**，重定向即可运行，链里没有 gcc：

```bat
bfc.exe bfsrc\bfc2.bf -o bfsrc\bfc2_seed.exe    :: 唯一的 C++ 种子
bfsrc\bfc2_seed.exe < bfsrc\bfc2.bf > bfsrc\bfc2.exe
bfsrc\bfc2.exe < prog.bf > prog.exe               :: prog.exe 可直接双击运行
```

难点还是**地址**：`[` / `]` 的 `rel32`、PE 字段都只能事后才知道，而 stdout
不能回退。解法是把回填**推迟到 exe 启动时**：

* 文件 = **固定 2048 字节头部**（DOS/PE 头 + 导入表 + 一个 RWX 节）+
  **overlay 记录流**；`.text` 的 `VirtualSize` 远大于 `RawSize`，加载器会把
  多出来的部分填零，所以镜像结构完全确定、与源程序大小无关；
* 每条 BF 指令编译成一条**定长记录** `[长度][类型][payload][EB 02]`，
  `[` / `]` 的跳转位移先写 0；
* 头部里 412 字节的**自读存根**在进程启动时用 `GetModuleFileNameA` +
  `ReadFile` 把自己的文件读进内存，线性扫描记录把跳转位移补成正确值，
  然后跳到第一条记录。

于是 BF 端只需要"逐字符抄记录模板"，零算术、零前瞻、零回填。

```
bfc.exe             (C++ 种子)
  │  编译 bfsrc/bfc2.bf（67497 字节纯 BF）
  ▼
bfc2_seed.exe       (BF 编译器翻译成的原生程序)                    0.015 s
  │  bfc2_seed.exe < bfsrc/bfc2.bf
  ▼
bfc2.exe            (编译器编译自己 → 504775 字节 exe)            0.289 s
  │  bfc2.exe < bfsrc/bfc2.bf
  ▼
stage3.exe          (新编译器再次编译自己)                        1.490 s
```

**固定点**：`stage3.exe` 与 `bfc2.exe` **逐字节相同**；两者都与
`tools/pak.py` 的 Python 参考模型逐字节相同。`bfc2.exe` 还能把 48 KB 的
`awib.b` 编译成 384638 字节的 exe，运行结果与原生 `awib.exe` 逐字节相同。

```bat
python tools\verify_bfc2.py         :: 11 项：解释 bfc2.bf，与模型逐字节一致
python tools\test_pak.py            :: 26 项：示例程序行为 + PE 结构
python tools\test_pak_large.py      :: 6 项：编译 awib.b 并与原生 awib 对拍
python bootstrap\selfhost_pak.py    :: 29 项：完整自举链 + 固定点
```

限制：记录流上限 2 MiB（约 5.8 万条 `.`）、循环嵌套 4096 层、磁带 30000 单元；
运行期比 `bfc.exe` 产物慢 1.5–3 倍（每条指令多一个 `jmp`）。完整推理见
[`docs/SELFHOST.md`](docs/SELFHOST.md)。

### awib 交叉验证（第三方 BF 自举编译器）

[awib](https://github.com/matslina/awib)（matslina/awib）是用 Brainfuck 写的
多目标编译器（C / Go / Java / Ruby / Rust / Tcl / i386-ELF），带完整的
`systest` 用例集，是检验 `bfc.exe` 的现成标准件：

```powershell
bfc.exe awib\awib.b -o awib\awib.exe -c 70000     :: 0.02 s，276480 B PE32+
powershell -File awib\verify_awib.ps1             :: 一键复现全部 16 项检查
```

* 全量 48535 字节 / 39273 条指令的 awib 源码，`bfc.exe` **0.02 秒**编译成原生 exe。
* 与 awib 自带的 `util/bfint.c` 解释器输出**逐字节一致**；awib 自带 `systest`
  **6/6 逐字节通过**。
* **自举**：`awib.exe < awib.b > awib_gen.c` → `gcc` → `awib2.exe`，
  `awib2.exe` 编译同一输入产出与 `awib.exe` **逐字节相同**的 C；
  这条链里没有任何 C 写的 BF 编译器。
* 还能生成 i386 Linux ELF（376 B Hello World），在 WSL 下直接运行成功。

细节、命令与实测数字见 [`awib/AWIB-REPORT.md`](awib/AWIB-REPORT.md)。

---

## 五、注意事项与已知取舍

* **注释里不能出现那 8 个指令字符**。BF 没有注释语法，非指令字符会被忽略，但
  `+ - < > . , [ ]` 永远是指令。示例 `nested.bf` 的注释里就曾写过"再 +1"，
  那个 `+` 被如实编译成了一条 `inc byte [rbx]`——编译器没错，是注释有歧义。
* **没有指针越界检查**。这是经典 BF 的未定义行为；本编译器为了速度不做钳制，
  `tape` 之外的单元就是 `.data` 里的其它字段。需要安全性时可在 `>`/`<` 处
  插入边界比较与钳制。
* **单元是 8 位、回绕**（`0-1 → 255`），磁带默认 30000 字节，可用 `-c` 调整。
* **控制台代码页**：`.` 写的是原始字节。用 `WriteFile` 输出到控制台时，Windows
  会按当前输出代码页解释这些字节；要输出 UTF-8 文本先 `chcp 65001`，重定向到
  文件则始终是精确的原始字节。
* 只针对 **Windows x86-64**。改成 x86-32 或 ELF 需要替换指令编码与映像组装部分
  （前者一个文件、两个函数的事），词法与中间表示完全可复用。

---

## 六、文件清单

| 文件 | 说明 |
|---|---|
| `bfc.cpp` | 编译器全部源码（约 600 行，含中文注释与指令编码说明） |
| `build.bat` | 用 g++ 构建 `bfc.exe` |
| `bfc.exe` | 已构建好的编译器（静态链接，无 DLL 依赖） |
| `bfsrc/t2bf.bf` | **用 Brainfuck 写的编译器**（352 字节），文本 → BF |
| `bfsrc/t2bf.exe` | 由 `bfc.exe` 编译 `t2bf.bf` 得到的原生 exe |
| `examples/hello.bf` | 经典 Hello World |
| `examples/cat.bf` | `,[.,]` 回显；演示 `,` 与 EOF |
| `examples/nested.bf` | 嵌套循环 4×4×4 |
| `examples/zeroing.bf` | `[-]` / `[+]` 清零窥孔优化 |
| `tools/gen_t2bf.py` | 精确计数生成 `t2bf.bf` 的 `'+'` 游程 |
| `tools/t2bf_model.py` | `t2bf.bf` 的独立 Python 参考模型 |
| `tools/pipeline_demo.py` | 两级工具链端到端演示 |
| `tests/pecheck.py` | 独立 PE32+ 结构校验器 |
| `bootstrap/mkstage0.py` | stage-0 汇编器：机器码字节流 → 完整 exe（由 `tools/gen_mkstage0.py` 生成）|
| `bootstrap/run_bootstrap_tests.py` | 自举链条端到端测试（57 项检查）|
| `tools/gen_mkstage0.py` | 生成 stage-0：PE 头、入口存根、导入表全部当常量计算 |
| `tools/bfcodegen.py` | 代码生成契约：每条 BF 指令 → 固定 x86-64 字节序列，含参考模型 |
| `tools/gen_bfc0.py` | 生成 `bfsrc/bfc0.bf`（含常量池布局与 6 路分派）|
| `tools/verify_bfc0.py` | 用 Python 模拟 `bfc0.bf`，与模型逐字节比对 |
| `bfsrc/bfc0.bf` | **用 Brainfuck 写的 Brainfuck 编译器**（30 KB），输出完整 exe |
| `bfsrc/bfc0.exe` | `bfc.exe` 编译 `bfc0.bf` 得到的原生编译器 |
| `tools/bf2c.py` | bfc1 的参考模型：BF → C 模板表与 `translate()`（无缓冲输出）|
| `tools/gen_bfc1.py` | 生成 `bfsrc/bfc1.bf`（12 单元、8 路 one-hot 分派、逐字节抄写模板）|
| `tools/verify_bfc1.py` | 在 Python 里解释 `bfc1.bf`，与参考模型逐字节比对（9 个用例）|
| `bfsrc/bfc1.bf` | **真正自举的 Brainfuck 写的编译器**（28 KB），输出 C 源码 |
| `bfsrc/bfc1.exe` | `bfc.exe` 编译 `bfc1.bf` 得到的原生 BF → C 编译器 |
| `bootstrap/selfhost.py` | bfc1 自举链：固定点检查 + 4 个示例程序（19 项检查）|
| `tools/pak.py` | PAK 格式参考模型：PE 头 + 自读存根 + 定长记录流（`python tools\pak.py in.bf out.exe`）|
| `tools/gen_bfc2.py` | 生成 `bfsrc/bfc2.bf`（逐字符抄写记录模板，零算术）|
| `tools/verify_bfc2.py` | 在 Python 里解释 `bfc2.bf`，与 `pak.py` 模型逐字节比对（11 项）|
| `tools/bfrun.py` | 带游程压缩的 Python Brainfuck 解释器（用在 verify_bfc2）|
| `bfsrc/bfc2.bf` | **不用 gcc 的自举编译器**（66 KB 纯 BF），输出完整 PE32+ 字节流 |
| `bfsrc/bfc2_seed.exe` | `bfc.exe` 编译 `bfc2.bf` 得到的种子编译器 |
| `bfsrc/bfc2.exe` | 自举后的 BF → exe 编译器（504775 B，由 `bfc2_seed.exe` 产出）|
| `bootstrap/selfhost_pak.py` | bfc2 自举链：固定点 + 示例程序 + awib 对拍（29 项检查）|
| `tools/test_pak.py` | PAK 镜像行为测试：5 个示例 + PE 结构（26 项检查）|
| `tools/test_pak_large.py` | 大程序测试：编译 48 KB 的 awib.b 并与原生 awib 对拍（6 项检查）|
| `docs/SELFHOST.md` | 自举的设计推理：为什么必须 overlay + 运行期补丁，以及记录格式 |
| `examples/bfc0_hello.bf` | 由 bfc0 编译的演示程序（输出 Hello）|
| `tests/run_tests.py` | 端到端 + 差分 + 往返测试（144 项检查）|
| `tests/out/` | 测试产物（可随时删除，重跑测试会重建） |
| `awib/awib.b` | 第三方 awib 的 BF 源码（48535 B / 39273 指令，由 `verify_awib.ps1` 下载并生成，不随仓库分发） |
| `awib/awib.exe` | `bfc.exe` 编译 awib 得到的**原生 awib 交叉编译器**（同样由脚本生成） |
| `awib/verify_awib.ps1` | 一键复现 awib 全部结论（16 项检查） |
| `awib/AWIB-REPORT.md` | awib 逆向分析 + 实测报告 |
| `awib/awib-master/` | awib 源码快照（缺失时脚本自动重新下载） |
