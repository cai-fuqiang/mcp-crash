# vmcore 分析报告

## 1. 问题概述

**现象**：一台 LoongArch 虚拟机（8 核 / 16GB）在运行 33 天后全系统挂死，
通过 `virsh dump` 获取 vmcore。PANIC 为空，非内核 panic 触发。

**结论**：一次瞬态硬件错误触发了 `BUG_ON` → `die()` 的 printk 输出路径在
系统不稳定时引发二次页错误 → 两个 CPU 在 `die_lock` 上形成自死锁 →
关中断无法响应 IPI → 其余 CPU 在 TLB flush 等待中永久阻塞 → 全系统瘫痪。

## 2. 基本信息

| 项目 | 值 |
|------|-----|
| 内核版本 | 6.6.0-97.0.0.102.oe2403sp2.loongarch64 |
| 架构 | loongarch64 (Loongson-3A5000, 8 核) |
| 内存 | 16 GB |
| 运行时间 | 33 days, 07:05:10 |
| Load Average | 170.00, 170.00, 170.00 |
| PANIC | 空（virsh dump，非 panic 触发） |
| Taint | G（闭源模块） L（soft lockup） |

## 3. 初步排查：所有 CPU 的状态

### 3.1 `bt -a` —— 全 CPU 堆栈

| CPU | PID | 进程 | 堆栈 |
|-----|-----|------|------|
| 0 | 0 | swapper/0 | **无堆栈** |
| 1 | 55933 | MonitorPlugin | `sys_madvise` → `zap_page_range` → TLB flush → `smp_call_function_many_cond` |
| 2 | 2922910 | jdog-kunlunmirr | `do_page_fault` → `wp_page_copy` → TLB flush → `smp_call_function_many_cond` |
| 3 | 2922950 | telegraf | **无堆栈** |
| 4 | 2743100 | VM Thread | `sys_mprotect` → TLB flush → `smp_call_function_many_cond` |
| 5 | 735 | jdog-monitor | `sys_madvise` → TLB flush → `smp_call_function_many_cond` |
| 6 | 2742973 | VM Thread | `sys_mprotect` → TLB flush → `smp_call_function_many_cond` |
| 7 | 1022 | JCSAgentCore | `sys_madvise` → TLB flush → `smp_call_function_many_cond` |

**关键发现**：
- CPU 1/2/4/5/6/7 全部卡在 `smp_call_function_many_cond`，等待 TLB flush IPI 响应
- CPU 0 和 CPU 3 **没有堆栈**——crash 无法回溯，表示异常栈可能已损坏
- PANIC 为空 + load 170 = 系统在极端内存压力下触发了 watchdog soft lockup

### 3.2 `log` —— 内核日志

```
[2066215] watchdog: BUG: soft lockup - CPU#5 stuck for 5666s! [jdog-monitor]
[2066215] watchdog: BUG: soft lockup - CPU#6 stuck for 5681s! [VM Thread]
[2066231] watchdog: BUG: soft lockup - CPU#7 stuck for 5722s! [JCSAgentCore]
...
```

Watchdog 每 ~20 秒持续报 soft lockup，持续时间超过 6000 秒（~1.7 小时）。
但日志中**没有** `Oops`、`Unable to handle kernel paging request`、
`Kernel ale access` 等 die() 输出——首个异常的输出已丢失。

### 3.3 初步结论

6 个 CPU 在等 TLB flush IPI 响应，2 个 CPU 无堆栈。三者之间的因果关系需要
通过手动回溯和寄存器分析来建立。

## 4. 深挖 CPU 0：嵌套异常的完整链路

### 4.1 怀疑方向

CPU 0 无堆栈但寄存器完整。从寄存器值和栈内存手动重建调用链。

### 4.2 寄存器分析

```
PC   = queued_spin_lock_slowpath+608  (MCS 队列路径等锁)
RA   = die+296                        (从 die() → raw_spin_lock_irq)
a0   = 0x9000000003450000             (= &die_lock)
sp   = 0x90000001003cf6d0
CRMD = 0xb0  (-IE  中断关闭)
ESTAT= 0x1808 (中断 pending，无法响应)
ERA  = console_flush_all+892           (二次异常的发生点)
BADV = 0x71c29                        (二次异常访问的地址)
```

**CRMD = 0xb0 → 中断关闭**。这是 CPU 0 无法响应 TLB flush IPI 的直接原因。

### 4.3 栈内存回溯

从 sp 向高地址逐帧读取栈内存，通过 crash `sym` 解析每帧的返回地址：

```
栈地址偏移  | 返回地址                         | 符号 + 源码位置
-----------|--------------------------------|---------------------------
sp+0x028   | die+296                        | traps.c:410 → raw_spin_lock_irq 之后
sp+0x058   | do_page_fault+92               | fault.c:303-304 → local_irq_enable
sp+0x758   | tlb_do_page_fault_0+280        | tlbex.S:32 → 异常入口
sp+0xa28   | do_ale+120                     | traps.c:585 → ALE 异常入口
sp+0xa08   | ip_route_input_slow+2388       | net/ipv4/route.c:2343 → fib_validate_source 调用点
```

### 4.4 完整调用链（双层异常）

```
第一层异常：ALE（地址对齐错误）
───────────────────────────────
  ip_route_input_slow+2388  (网络包路由中)
    └─ fib_validate_source → 非对齐内存访问
         ↓
  [ALE 异常] → do_ale()
    ├─ line 572: show_registers(regs) → printk → vprintk_emit
    │            → console_unlock → console_flush_all
    │
第二层异常：page fault（在诊断输出路径中）
─────────────────────────────────────────
    console_flush_all+892
    └─ ld.bu $t0, $s3, 0 → $s3 = 0x71c29 (损坏指针)
         ↓
  [page fault] → tlb_do_page_fault_0 → do_page_fault
    ├─ fault.c:303: local_irq_enable() ← 短暂恢复中断
    ├─ no_context → pr_alert("Unable to handle kernel paging request at 0x71c29")
    └─ die("Oops", regs) → raw_spin_lock_irq(&die_lock)
         └─ 锁已被 CPU 3 持有 + pending=1 → MCS 队列 → 永久卡死
              PC = queued_spin_lock_slowpath+608 (CRMD.-IE)
```

### 4.5 关键答案

- **为何不响应中断**：`die()` → `raw_spin_lock_irq` 先关中断再等锁。CRMD = 0xb0 永久 -IE
- **为何进入异常**：`ip_route_input_slow` 中的 ALE → `do_ale` 诊断输出路径 → 二次 page fault
- **`do_ale` 的设计缺陷**：`show_registers(regs)` 在 `emulate_load_store_insn` 之前执行。即使 ALE 可以模拟恢复、系统能继续运行，诊断打印中的二次异常可能先一步触发 `die("Oops")`

## 5. 深挖 CPU 3：BUG_ON 诱发的自死锁

### 5.1 怀疑方向

CPU 3 同样无堆栈，但与 CPU 0 不同——它的 ERA 是 `hrtimer_interrupt+776`，
位于定时器中断内。需要确定是哪种异常触发了 die()。

### 5.2 寄存器分析

```
PC   = queued_spin_lock_slowpath+72   (pending 路径等锁)
RA   = die+296                        (同 CPU 0：来自 raw_spin_lock_irq)
a0   = 0x9000000003450000             (= &die_lock)
sp   = 0x90000001003cf2a0
CRMD = 0xa8  (-IE  中断关闭)
ERA  = hrtimer_interrupt+776          (首次异常发生点)
BADV = 0x90000000028a4c94             (二次异常地址)
```

r10/r11 寄存器形成 ASCII 字符串 `"kernel.c:1856!"`，ERA 反汇编为 `break 0x1`。

### 5.3 反汇编逐指令证明：BUG_ON 位置

```
C 源码 (hrtimer.c:1849-1857):
  void hrtimer_interrupt(struct clock_event_device *dev) {
      struct hrtimer_cpu_base *cpu_base = this_cpu_ptr(&hrtimer_bases);
      ...
      BUG_ON(!cpu_base->hres_active);    // ← 第 1856 行
      cpu_base->nr_events++;              // ← 第 1857 行

反汇编 (@ hrtimer_interrupt):
  +56:  add.d    $s5, $s1, $x          ; $s5 = cpu_base (this_cpu_ptr)
  +60:  ld.bu    $t0, $s5, 16          ; t0 = cpu_base->hres_active
                                        ;   ★ struct 确认 offset 16 = hres_active
  +64:  bstrpick.d $t0, $t0, 0, 0      ; 提取 bit 0
  +68:  beqz     $t0, +708             ; if (bit0 == 0) → jump to break
  ─── BUG_ON 通过后的正常路径 ───
  +72:  ldptr.w  $t0, $s5, 20          ; t0 = cpu_base->nr_events
                                        ;   ★ struct 确认 offset 20 = nr_events
  +84:  addi.w   $t0, $t0, 1           ; t0++
  +88:  st.w     $t0, $s5, 20          ; nr_events++ (line 1857)
  ...
  ─── BUG_ON 失败时跳转到这里 ───
  +776: break    0x1                   ; ★ 全函数唯一 break = BRK_BUG = BUG()
```

**五维交叉验证**：ERA（硬件寄存器）+ 反汇编（唯一 break）+ struct 偏移（offset 16/20 精准匹配）+ 源码（唯一 BUG_ON）+ 栈帧（constant_timer_interrupt 上下文），锁定 100% 在 `hrtimer_interrupt:1856`。

### 5.4 hres_active 矛盾

`BUG_ON` 的条件是 `!cpu_base->hres_active`，即 hres_active 必须为 0 才会触发。
但 vmcore 中**全 8 个 CPU 的 hres_active 均为 1**。

| CPU | hrtimer_bases 地址 | hres_active | online |
|-----|-------------------|-------------|--------|
| 0 | 0x9000000008003600 | 1 | 1 |
| 1 | 0x9000000008103600 | 1 | 1 |
| ... | ... | 1 | 1 |
| 7 | 0x9000000008703600 | 1 | 1 |

进一步通过 `virsh qemu-monitor-command --hmp 'xp'` 直接读取物理内存，
确认现场 hrtimer_bases[3] offset 16 = **0x11**（bit0=1，hres_active 仍然 = 1）。

**结论**：BREAK 发生时 hres_active 短暂为 0/load 读取了错误值（瞬态硬件错误
或 per-CPU offset 瞬时错位），dump 时已恢复。非持续性内核状态错误。

### 5.5 CPU 3 栈帧回溯

```
栈地址       | 返回地址                       | 符号
------------|------------------------------|---------------------------
0x3cf2c8    | handle_irq_event_percpu+100   | kernel/irq/handle.c:199
0x3cf268    | __handle_irq_event_percpu+108 | kernel/irq/handle.c:158
0x3cf260    | constant_timer_interrupt+56    | arch/loongarch/kernel/time.c:43
0x3cf3f0    | printk_get_next_message+192   | kernel/printk/printk.c:2865
0x3cf500    | vscnprintf+24                 | lib/vsprintf.c:2935
0x3cf5a0    | console_unlock+172            | kernel/printk/printk.c:3103
```

### 5.6 CPU 3 自死锁机制

```
第一层异常：BREAK（BUG_ON）
───────────────────────────
  hrtimer_interrupt+776: break 0x1  →  bug_handler → die_if_kernel → die("Oops - BUG")
    ├─ raw_spin_lock_irq(&die_lock) ✓ 拿锁成功
    ├─ show_registers → printk → console_flush_all
    │
第二层异常：page fault（在诊断输出路径中）
─────────────────────────────────────────
    console_flush_all 中访问 BADV = 0x90000000028a4c94 → page fault
    └─ do_page_fault → no_context → die("Oops")
         └─ raw_spin_lock_irq(&die_lock) → 自己已持锁！
              queued_spin_lock_slowpath: locked=1 → 设 pending=1
              smp_cond_load_acquire(&lock->locked, !VAL)
              等 locked 清 0，但 locked 是自己设的 → 永不释放 → 自死锁
              PC = queued_spin_lock_slowpath+72 (CRMD.-IE)
```

## 6. CSD 级别论证：各 CPU 等待的确切目标

### 6.1 怀疑方向

CPU 1-7 都卡在 `smp_call_function_many_cond`，但它们在等哪个具体的 CPU？

### 6.2 分析方法

读取每个 CPU 的 `cfd_data` per-CPU 变量，解析 `cpumask` 确认目标 CPU 集合，
再通过 CSD 的 `u_flags`（CSD_FLAG_LOCK 位）逐一验证各目标 CPU 的响应状态。

### 6.3 证据

以 CPU 1 的 cfd_data 为例：

```
cfd_data[1]:
  cpumask    = 0xfd = {CPU0, CPU2, CPU3, CPU4, CPU5, CPU6, CPU7}

CSD[0] @ 0x900000000802FEA0 → u_flags = 0x11 → LOCKED   (CPU 0 未响应)
CSD[2] @ 0x900000000822FEA0 → u_flags = 0x00 → UNLOCKED (CPU 2 已响应)
CSD[3] @ 0x900000000832FEA0 → u_flags = 0x11 → LOCKED   (CPU 3 未响应)
```

|目标 CPU | 中断 | u_flags | 状态 |
|----------|------|---------|------|
| **CPU 0** | **关** | **0x11 LOCKED** | 未响应 |
| CPU 2 | 开 | 0x00 UNLOCK | 已响应 ✓ |
| **CPU 3** | **关** | **0x11 LOCKED** | 未响应 |

CPU 2（中断开启）的 CSD 已解锁——证明了**中断开启的 CPU 可以正常完成 IPI**。

`for_each_cpu` 从 cpumask 最低位遍历，第一个目标就是 CPU 0。`csd_lock_wait(CSD[0])`
在第一次迭代就阻塞。每个 CPU 1-7 都在等同一个 CPU——**CPU 0**。

## 7. die_lock 持有者定位

### 7.1 锁状态

```
die_lock @ 0x9000000003450000
val = 0x00040101
  locked  = 1  → 锁被持有
  pending = 1  → 1 个 CPU 在 pending 状态
  tail    = 4  → 1 个 CPU 在 MCS 队列 (CPU 0, idx=0)
```

### 7.2 逐 CPU 排除

| CPU | PC | 是否在 die() 临界区 | die_lock 角色 |
|-----|-----|-------------------|-------------|
| 0 | MCS 队列 (+608) | 否 | 第三到达 |
| 1-7 | smp_call_function_many_cond | 否 | 无关 |
| 3 | pending 路径 (+72) | **否（当前上下文），是（嵌套外层）** | 第一到达 + 第二到达 |

所有的 CPU 1-7 都在 `smp_call_function_many_cond` 中，CPU 0 在 MCS 队列，
能成为锁持有者的**只有 CPU 3**。

### 7.3 自死锁逻辑

queued_spin_lock 排队语义：第一个调用者拿锁（locked=1），第二个调用者
设 pending=1，第三个进 MCS 队列。CPU 3 **第一次** die() 拿锁 → 进入临界区
→ printk 输出中二次异常 → **第二次** die() 设 pending=1 → 等 locked 清 0 →
但 locked 是自己（外层）设的，永不释放 → 自死锁。CPU 0 是第三次到达，
进 MCS 队列。

## 8. BADV = 0x71c29 定位：console 代码无罪

### 8.1 问题

CPU 0 在 `console_flush_all` 中访问 `0x71c29` 触发 page fault。
这是一个页错误，是否意味着 console 子系统有 bug？

### 8.2 排查

- **console 链表遍历**：注册 2 个 console（ttyS + tty），链表结构完整，
  所有指针成员在合法内核地址范围。**console 代码没有野指针**。
- **`kmem 0x71c29`**：返回 invalid，不是任何内核映射地址
- **0x71c29 与 ECFG (0x71c1d)**：仅差 12 字节

### 8.3 不是巧合：双栈帧并发证明

两个 CPU 在同一个 console 输出路径中触发 page fault，必须证明
它们是**同时**而非先后进入的。

**CPU 0 的 console 路径（来自 do_ale，不经过 die_lock）：**

```
ip_route_input_slow → ALE 异常
  → do_ale+120 (traps.c:585)
    → show_registers(regs)               ← 直接调用，无 die_lock
      → printk → vprintk_emit
        → __wake_up_klogd+72             [栈 0x3cf610]
        → irq_work_queue+36              [栈 0x3cf608]
        → console_unlock+172             [栈 0x3cf5a8]
          → console_flush_all+892        ← ERA! page fault
            BADV=0x71c29, ld.bu $s3,0
```

**CPU 3 的 console 路径（来自 die，持有 die_lock）：**

```
hrtimer_interrupt → BUG_ON → break 异常
  → die("Oops - BUG")
    → raw_spin_lock_irq(&die_lock) ✓ 拿锁
    → show_registers(regs)               ← die() 内调用
      → printk → vprintk_emit
        → prb_read_valid+32              [栈 0x3cf3d8]
        → printk_get_next_message+192    [栈 0x3cf3f8]
        → vscnprintf+24                  [栈 0x3cf500]
        → console_unlock+172             [栈 0x3cf5a8]
          → console_flush_all
            → page fault (BADV=0x28a4c94) ← 二次异常
```

**汇聚证明：**

```
                     show_registers()
                    /               \
           do_ale()                  die()  [holds die_lock]
           (CPU 0)                   (CPU 3)
              │                         │
              ▼                         ▼
         printk()                   printk()
              │                         │
              ▼                         ▼
         console_unlock()           console_unlock()
              │                         │
              ▼                         ▼
         console_flush_all()        console_flush_all()
              │                         │
              └─────────┬───────────────┘
                        ▼
         console_emit_next_record()
                        │
                        ▼
         con->write() = univ8250_console_write()
                        │
                        ▼
              同一个 uart_port 结构体
              (ttyS console @ 0x9000000002d053e0)
```

### 8.4 uart_port 内存损坏实证

ttyS console 的 `data` 指针（offset 104）→ `0x9000000002d05480`，
类型为 `struct uart_8250_port`（792 字节，内含 `struct uart_port`）。

对比预期值 vs vmcore 实际值（crash `struct uart_8250_port.port` 读取）：

| 字段 | 偏移 | 预期（正常 8250 UART） | vmcore 实际值 | 判定 |
|------|------|----------------------|--------------|------|
| port.lock | 0 | 0 或 locked | 0 | 正常 |
| port.serial_in | 24 | 函数指针 (0x9000..) | **0x4000000004** | **损坏** |
| port.serial_out | 32 | 函数指针 | **0x10** | **损坏** |
| port.set_termios | 40 | 函数指针 | **0x9000000002d053e0** (console 自身地址) | **损坏** |
| port.set_ldisc | 48 | 函数指针 | **0x900000010555a000** (堆地址) | **损坏** |
| port.set_mctrl | 72 | 函数指针 | **0x0** (NULL) | **损坏** |
| port.cons | — | → console 回指针 | **0x0** (NULL) | **损坏** |
| port.fifosize | — | ≥1 | **0** | **损坏** |
| port.iotype | — | 'M' 或其他 | **0 ('\0')** | **损坏** |
| port.ops | — | 函数指针表 | **0x9000000001b191d8** (字符串区) | **损坏** |
| port.name | — | 串口名称 | **"33.6"** (非串口名) | **损坏** |
| port.line | — | 串口编号 | **2415919104** (0x9000..解释为 int) | **损坏** |
| capabilities | 528 | ≥1 | **0** | **损坏** |
| ier / lcr / mcr / fcr | — | UART 寄存器缓存 | **全 0** | **损坏** |

**关键损坏项**：`serial_in = 0x4000000004` 和 `serial_out = 0x10` 不能是函数指针
（LoongArch 上合法函数地址在 0x90000000.. 范围）。`set_termios` 被替换为
console 结构体自身的地址 `0x9000000002d053e0`。`name` 显示 "33.6"。

### 8.5 并发如何导致损坏

`serial8250_console_write` 的反汇编揭示了并发损坏的指令级机制：

```
serial8250_console_write+100:  ldptr.w  $t0, $s1, 0    ← port->lock 的 ll
serial8250_console_write+116:  ll.w     $t1, $s1, 0    ← load-linked port->lock
serial8250_console_write+128:  sc.w     $t0, $s1, 0    ← store-conditional port->lock
```

`$s1` = uart_port 基址 = `0x9000000002d05480`。两个 CPU 的 `univ8250_console_write`
都通过 `con = 0x9000000002d053e0`（全局唯一的 ttyS console）推导出
**同一个 `$s1`**。没有任何 per-CPU 隔离。

关键代码路径 `wait_for_xmitr` + `serial_out`：
```
serial8250_console_write:
  → 保存 UART 寄存器到栈局部变量 (IER, LCR, MCR)
  → wait_for_xmitr(port):        ← serial_in(port, UART_LSR) 轮询
  → uart_console_write():        ← 逐字符 serial_out(port, UART_TX)
  → 从栈局部变量恢复 UART 寄存器   ← 互相覆盖！
```

当 CPU A 和 CPU B 交叉执行时：
1. CPU A：保存 IER=0x00 → 栈
2. CPU B：保存 IER=0x00 → 栈
3. CPU A：写 LCR=0x03（设置 DLAB）
4. CPU B：读 LSR（得到 DLAB 模式下的错误值）
5. CPU B：用错误值作为指针 → page fault

两个 CPU 对同一 UART 的交替访问产生不可预期的中间状态。
`port->lock`（ll/sc）仅在非 oops 路径有用——在 `bust_spinlocks(1)` 后，
spinlock 检查被跳过。

**并发时序证明：**

1. **oops_in_progress = 2**：两个 CPU 都在 oops 上下文中
2. **console_locked = 0**：锁旁路已生效
3. **CPU 0 独立进入**：通过 `do_ale()` → `show_registers()`，
   不经 `die_lock`，与 CPU 3 的 `die()` 路径完全独立
4. **两个 page fault 地址不同**：CPU 0: `0x71c29`，CPU 3: `0x28a4c94`，
   但都源于同一 uart_port 的并发访问——谁先踩坏哪个字段取决于交错时序

### 8.6 `bust_spinlocks(1)` 无锁并发：`oops_in_progress = 2` 的精确解释

**`bust_spinlocks(1)` 在两个位置被调用：**

| 位置 | 源码 | die_lock 保护？ |
|------|------|:---:|
| `traps.c:411` | `die()` 内 | ✅ 是（`raw_spin_lock_irq` 之后） |
| `fault.c:91` | `no_context()` 内 | **❌ 否**（调 `bust_spinlocks` 之后才调 `die()`） |

```c
// arch/loongarch/mm/fault.c:72-97
static void no_context(struct pt_regs *regs, ...)
{
    if (spurious_fault(write, address))
        return;
    if (fixup_exception(regs))
        return;

    bust_spinlocks(1);                      // ← 无锁！先 bust
    pr_alert("...\n");
    die("Oops", regs);                      // ← 才去争 die_lock
}
```

**堆栈证据：两个 CPU 都通过 `no_context()` 进入：**

- CPU 0：`do_page_fault+92`（见 §4.3 栈帧表）→ `__do_page_fault` → `no_context`（`fault.c:199`）→ `bust_spinlocks(1)` + `die("Oops")`
- CPU 3：同路径（见 §5.5），嵌套异常的 `do_page_fault` → `no_context` → `bust_spinlocks(1)` + `die("Oops")`

两个 `no_context()` 调用是 per-CPU 异常处理路径，**完全独立、没有跨 CPU 同步**。两个 CPU 在各自的异常栈上同时执行 `++oops_in_progress`（plain int，非原子），同时绕过 console 锁，同时进入 `univ8250_console_write`。

## 9. Live 验证

写入分析报告时 VM 仍在运行，通过 `virsh qemu-monitor-command` 交叉验证：

1. `info registers`：CPU 0 PC 仍为 `queued_spin_lock_slowpath+608`，与 vmcore 完全一致
2. 物理内存 `xp /8gx 0x08303610`：hrtimer_bases[3] offset 16 = 0x11，hres_active=1
3. 所有 CPU 保持冻结状态，全系统无任何代码在执行

Live 验证确认 vmcore 数据的准确性和 hres_active=1 的持续性。

## 10. 最终结论

### 三层因果链

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第一层 · 引爆点 —— BREAK 异常
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CPU 3: hrtimer_interrupt → BUG_ON(!cpu_base->hres_active) → break 0x1
         位置: 反汇编 + ERA (BRK_BUG=1) + struct 偏移 (offset 16) 三重锁定
         指令: ld.bu $t0, $s5, 16; bstrpick bit0; beqz → break 0x1
         ★ hres_active 在 dump 时全 CPU = 1，live QEMU 确认一致
         ★ 瞬态根因（load 读到错误值 0 / per-CPU offset 瞬时错位 /
            缓存一致性错误）无法从单一 vmcore 精确确定

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第二层 · 二次崩溃（两个 CPU 以相同模式触发）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CPU 3: die("Oops - BUG") → 拿 die_lock → show_registers → printk
           → console 输出中触发二次 page fault → die("Oops")
           → 再拿 die_lock → locked 是自己设的 → 自死锁 (pending=1)

  CPU 0: ip_route_input_slow → ALE → do_ale → show_registers → printk
           → console 输出中触发二次 page fault → die("Oops")
           → die_lock 已被 CPU 3 占据(pending)+持有(locked) → MCS 队列

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第三层 · 全系统瘫痪
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CPU 0,3: 关中断等 die_lock → 无法响应 TLB flush IPI → CSD LOCK 永久 set
  CPU 1,2,4,5,6,7: csd_lock_wait → 等 CPU 0,3 响应 → 全 CPU 卡死
  watchdog: 检测到 soft lockup → 每 20s 告警 → NMI backtrace
  1.7 小时后: virsh dump 捕获现场
```

### 核心发现

1. **触发源为 BREAK 异常，根因无法精确定位**：`break 0x1` 指令确凿在 `hrtimer_interrupt:1856`。但 dump 时全 CPU `hres_active = 1`（live QEMU 确认），BUG_ON 条件在 dump 时刻为 false。BREAK 发生时 `ld.bu $s5, 16` 读到瞬态错误值 0——是硬件 bit flip、per-CPU offset 错位、还是缓存一致性问题，无法从单一 vmcore 确定
2. **二次崩溃因素**：`die()` → `show_registers() → printk() → console_flush_all()` 在系统不稳定时可能触发二次异常。`do_ale` 中 `show_registers` 在模拟决策之前执行，可能将可恢复异常升级为死锁
3. **自死锁机制**：CPU 3 的外层 die() 持锁 + 内层嵌套 die() 等锁，形成不可恢复的单 CPU 死锁
4. **全系统波及**：两个 CPU 关中断无法响应 IPI → 其余 6 CPU 在 TLB flush 等待中永久阻塞
5. **console 路径并发崩溃**：uart_port 结构体 14 字段被损坏，`oops_in_progress` 绕锁导致两个 CPU 同时进入同一 `univ8250_console_write` 踩坏内存
6. **并行现象：RCU stall × KVM 定时器丢失**：TLB flush IPI 风暴中 KVM 软件 IPI 注入可覆盖 HW 定时器中断，导致 CPU0/3 tick 丢失、watchdog 静默。但与 die_lock 死锁**无因果交叉**——是共享同一触发源的并行现象（见 §12）
5. **console 路径并发崩溃**：uart_port 结构体 14 个字段被损坏（serial_in、serial_out 等函数指针被替换为非法值）。两个 CPU 通过独立路径同时进入 `serial8250_console_write`，`oops_in_progress` 绕锁导致对同一 uart_port 的并发访问踩坏内存

### 方法论

- **无堆栈 CPU 的手动回溯**：通过寄存器值 + 栈内存逐帧读取 + crash `sym` 解析，重建完整调用链
- **ERA + 反汇编 + struct 偏移**：三重交叉验证锁定 BUG_ON 精确位置
- **CSD u_flags 字段**：通过 LOCKED/UNLOCKED 状态确认各 CPU 的 IPI 完成情况
- **Live QEMU 验证**：在 VM 仍在运行时交叉验证 vmcore 数据的正确性

## 11. 上游修复状态

基于 Linux v7.1-rc1 (`linux-master`) 对比 6.6.0-97.0.0.102 内核，
逐一核查分析中发现的各个问题的修复状态：

### 已修复（但不在 6.6.0 中）

| `7f8fdd4dbffc` | Jul 2025 | **8250 串口并发访问 panic**（直接命中） | ✅ 不在 6.6.0 |

`7f8fdd4dbffc` 直接命中本次 crash 的并发场景：
> "When another CPU (e.g., using printk()) is accessing the UART,
> the current CPU fails the check... causing it to enter dw8250_force_idle().
> Put serial_port_out() under port->lock to fix this issue."

与 CPU 0/3 同时在 `serial8250_console_write` 并发访问同一 UART 的场景吻合。
commit 日期晚于内核构建日期（Jun 2025），不在当前 6.6.0 中。

---

## 12. 并行现象：RCU stall × KVM 定时器丢失

### Claude Code 分析中的 RCU stall 证据

Claude Code 独立分析同一 vmcore 时，从 dmesg ring buffer 中提取了 RCU stall 信息：

```
CPU0: rcu: 0-...!: (23 ticks this GP) idle=59a4/0/0x3
      softirq=314610332/314610332 fqs=0
      → softirq 计数冻结，CPU0 未处理任何软中断
      → 23 ticks this GP：Grace Period 内仅 23 个 tick，几乎静止

CPU3: rcu: 3-...!: (1 GPs behind) idle=a644/1/0x4000000000000000
      softirq=161060137/161060137 fqs=0
      → 同样 softirq 冻结

rcu_sched kthread starved for 203,890,287 jiffies!  (~815,561 秒)
```

CPU0 和 CPU3 在 RCU 视角下完全无响应。NMI backtrace 发送均失败：
```
Sending NMI from CPU N to CPUs 0:
Unable to send backtrace IPI to CPU0 - perhaps it hung?
```

### Bibo Mao KVM 定时器丢失补丁

`20260414072313.3801110-1-maobibo@loongson.cn` (Apr 14, 2026)
「LoongArch: KVM: Fix HW timer interrupt lost when inject interrupt from software」

**Bug 描述**：
> When inject emulated CPU interrupt by software such CPU_SIP0/CPU_IPI,
> HW timer interrupt may be lost.

**修复逻辑**（`arch/loongarch/kvm/interrupt.c`）：
```c
// kvm_irq_deliver() 中，软件注入 IPI/SWI 前后：
old = kvm_read_hw_gcsr(LOONGARCH_CSR_TVAL);  // 读定时器 tick
set_gcsr_estat(irq);                          // 注入软件中断
new = kvm_read_hw_gcsr(LOONGARCH_CSR_TVAL);  // 再读定时器 tick
if (new > old)                                 // tick 值反转 → 定时器被覆盖
    set_gcsr_estat(CPU_TIMER);               // 手动补注定时器中断
```

**`Fixes: f45ad5b8aa93` (v6.7+)**，意味着 **6.6.0 内核不包含原始 commit**，但同样受影响。

### 与主线死锁的关系

定时器丢失和 die_lock 死锁共享同一个触发源（TLB flush IPI 风暴），
但**没有因果交叉**：

```
                    IPI 风暴 (TLB flush)
                    /                  \
                   /                    \
      CPU0/3 进入 die()             KVM 软件注入 IPI
      关中断等 die_lock              覆盖 HW 定时器
           │                              │
           ▼                              ▼
      die_lock 死锁                    tick 丢失
      (主线分析已证明)                  watchdog 静默
           │                          RCU callback 静止
           │                              │
           │                              ▼
           │                     RCU stall 严重化
           │                     (~800k jiffies)
           ▼
      CPU1-7 TLB flush 永久阻塞
```

- **左侧**：根本原因，CPU0/3 已在 die_lock 中关中断，无法响应 IPI
- **右侧**：并行现象，KVM 定时器丢失使 watchdog 无法在 CPU0/3 上触发、
  RCU 报告严重恶化，但**不影响死锁机制本身**

KVM 定时器丢失解释的是「为什么 watchdog 检测晚、RCU stall 报告严重」，
不是「为什么 CPU0/3 会卡死」。

### 相关 fix commit：`02a6a1f9d77a`

上游 v7.1-rc1 已合入 `02a6a1f9d77a`：「LoongArch: Make arch_irq_work_has_interrupt()
true only if IPI HW exist」。该补丁检查 IPI 硬件是否真正存在，避免在无 IPI 硬件
的虚拟化配置下错误地依赖 `irq_work` 机制。与定时器丢失补丁互补，共同提升
LoongArch KVM 中断投递可靠性。

### 建议合入（新增）

| 补丁 | 日期 | 针对问题 |
|------|------|---------|
| `20260414072313` (PATCH 2/3) | Apr 2026 | KVM 软件 IPI 注入时 HW 定时器丢失 |
| `02a6a1f9d77a` | v7.1-rc1 | `arch_irq_work_has_interrupt()` 在无 IPI HW 时应返回 false |

可减轻 IPI 风暴期间的定时器静默和 RCU stall 严重程度（但不影响 die_lock 死锁本身）。

### 仍未修复（v7.1-rc1）

| 问题 | 当前状态 |
|------|---------|
| **`show_registers` 在 `emulate_load_store_insn` 之前执行** | v7.1-rc1 中 `do_ale()` 仍先调用 `show_registers()` 再模拟。可恢复异常仍可能被诊断打印中的二次异常升级为死锁 |

### 建议合入补丁

**必须合入（直接命中）：**

1. **`7f8fdd4dbffc` serial: 8250: fix panic due to PSLVERR**（Jul 2025）

   该补丁修改的是 `serial8250_initialize()`，此函数在 6.11+ 才从
   `serial8250_do_startup()` 拆分出来，无法直接 cherry-pick。

   6.6.0 backport 如下：

```diff
--- a/drivers/tty/serial/8250/8250_port.c
+++ b/drivers/tty/serial/8250/8250_port.c
@@ -2375,9 +2375,9 @@ int serial8250_do_startup(struct uart_port *port)
 	/*
 	 * Now, initialize the UART
 	 */
-	serial_port_out(port, UART_LCR, UART_LCR_WLEN8);
 
 	spin_lock_irqsave(&port->lock, flags);
+	serial_port_out(port, UART_LCR, UART_LCR_WLEN8);
 	if (up->port.flags & UPF_FOURPORT) {
 		if (!up->port.irq)
 			up->port.mctrl |= TIOCM_OUT1;
```

   生效逻辑：将 `serial_port_out(LCR)` 放入 `port->lock` 保护，防止 CPU A
   （printk→console）持有锁写 UART 时，CPU B（startup）在锁外通过
   `serial_port_out(LCR)` 触发 `dw8250_check_lcr()` → `dw8250_force_idle()`
   → `serial8250_clear_and_reinit_fifos()` → `serial_port_in(RX)` 导致 PSLVERR。
   `flags` 变量在函数开头（line 2186）已声明，上移无作用域问题。

**建议合入（本分析发现的代码缺陷）：**

2. **`do_ale` 重排**：`show_registers(regs)` 移到 `emulate_load_store_insn` 失败之后（`sigbus` 标签处），仅在模拟失败时才打印。当前 v7.1-rc1 仍未修复
