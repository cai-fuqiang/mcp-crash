# VMcore 分析报告：i-adt8rap1i0

## 基本信息

| 项目 | 值 |
|---|---|
| 主机名 | general-2-loongson-11-211-129-43.vm-1 |
| 内核版本 | 6.6.0-97.0.0.102.oe2403sp2.loongarch64 |
| 架构 | LoongArch64 (Loongson-3A5000, 2000 MHz × 8) |
| 内存 | 16 GB |
| 崩溃时间 | 2026-04-28 17:03:54 CST（运行 33 天后） |
| 系统负载 | **170.00 / 170.00 / 170.00**（严重过载，8 核机器） |
| vmcore 采集方式 | `virsh dump`（内存快照，非 kdump） |
| vmcore 路径 | `/root/wangfuqiang49/i-adt8rap1i0.core` |
| vmlinux 路径 | `/root/wangfuqiang49/usr/lib/debug/lib/modules/6.6.0-97.0.0.102.oe2403sp2.loongarch64/vmlinux` |
| Kernel tainted | `G L`（已加载第三方/GPL 模块） |
| 加载模块 | sch_tbf, binfmt_misc, rfkill, nls_cp936, vfat, fat, joydev, virtio_net, net_failover, efi_pstore, failover, virtio_balloon, rtc_efi, pstore, fuse, nfnetlink, virtio_gpu, virtio_dma_buf, drm_shmem_helper, virtio_blk, ipv6, crc_ccitt |

---

## 根因：`smp_call_function_many_cond` 死循环自旋

### 现象

系统所有 CPU（0~7）相继出现 soft lockup，卡死时长从数千秒到数千秒不等，watchdog 持续报告：

```
watchdog: BUG: soft lockup - CPU#N stuck for XXXXS! [task:PID]
```

### 触发调用链

所有卡死 CPU 的调用栈高度一致，存在两条触发路径：

#### 路径一：madvise(MADV_DONTNEED)

```
handle_syscall
  └─ do_syscall
       └─ sys_madvise
            └─ do_madvise
                 └─ madvise_vma_behavior
                      └─ madvise_dontneed_free
                           └─ zap_page_range_single
                                └─ tlb_finish_mmu
                                     └─ tlb_flush_mmu
                                          └─ flush_tlb_range
                                               └─ on_each_cpu_cond_mask
                                                    └─ smp_call_function_many_cond+0x420  ← 永久自旋
```

涉及 CPU：1（MonitorPlugin）、2（jdog-kunlunmirr）、5（jdog-monitor）、7（JCSAgentCore）

#### 路径二：mprotect

```
sys_mprotect
  └─ do_mprotect_pkey
       └─ tlb_finish_mmu → tlb_flush_mmu → flush_tlb_range
            └─ on_each_cpu_cond_mask
                 └─ smp_call_function_many_cond+0x420  ← 永久自旋
```

涉及 CPU：4（VM Thread）、6（VM Thread）

#### 路径三：CoW 缺页（Copy-on-Write page fault）

```
tlb_do_page_fault_1
  └─ do_page_fault → __do_page_fault → handle_mm_fault
       └─ __handle_mm_fault
            └─ wp_page_copy            ← 写时复制
                 └─ ptep_clear_flush
                      └─ flush_tlb_page
                           └─ on_each_cpu_cond_mask
                                └─ smp_call_function_many_cond+0x420  ← 永久自旋
```

涉及 CPU：2（jdog-kunlunmirr，早期阶段）

### 卡死位置汇编分析

```
ERA = 0x90000000003493c0  <smp_call_function_many_cond+0x420>

0x90000000003493c0:  ldptr.w   $t0, $t1, 8   ; 读取远端 CPU 的完成标志位
0x90000000003493c4:  andi      $t0, $t0, 0x1
0x90000000003493c8:  bnez      $t0, -8        ; 标志位为 1（未完成）→ 死循环
0x90000000003493cc:  dbar      0x15
```

含义：发出 TLB shootdown IPI 后，以忙等方式轮询目标 CPU（**CPU0**）是否已完成 TLB 无效操作。由于 CPU0 已 hung 死，该标志位永远不会被清零，所有等待 CPU 陷入无限循环。

---

## 各 CPU 详细状态

### crash 快照时 CPU 分布（runq）

| CPU | current task | 状态 | 卡死时长 | 触发路径 |
|---|---|---|---|---|
| **CPU 0** | swapper/0 | **完全 hung**，NMI 无响应，vmcore 中无堆栈 | — | 根源 |
| CPU 1 | MonitorPlugin (PID 55933) | smp_call_function_many_cond 自旋 | **~5725 s** | madvise |
| CPU 2 | jdog-kunlunmirr (PID 2922910) | smp_call_function_many_cond 自旋 | **~5643 s** | madvise / CoW |
| **CPU 3** | telegraf (PID 2922950) | **NMI 无响应**，vmcore 中无堆栈 | — | 见下文 |
| CPU 4 | VM Thread (PID 2743100) | smp_call_function_many_cond 自旋 | **~5133 s** | mprotect |
| CPU 5 | jdog-monitor (PID 735) | smp_call_function_many_cond 自旋 | **~5666 s** | madvise |
| CPU 6 | VM Thread (PID 2742973) | smp_call_function_many_cond 自旋 | **~5681 s** | mprotect |
| CPU 7 | JCSAgentCore (PID 1022) | smp_call_function_many_cond 自旋 | **~5722 s** | madvise |

### 卡死 CPU 寄存器（以 CPU7 为代表，其余 CPU 寄存器几乎相同）

```
pc  90000000003493c0  <smp_call_function_many_cond+0x420>
ra  9000000000349394  <smp_call_function_many_cond+0x3f4>
t0  0000000000000001  ← 忙等判断值，始终为 1（目标 CPU 未完成）
t1  900000000802ff60  ← call_single_data 结构指针
CRMD: 000000b0  (PLV0 -IE -DA +PG)   ← 内核态，中断关闭
ESTAT: 00000800 [INT] (IS=11)         ← 由定时器中断（watchdog）打断
PRID: 0014c010  (Loongson-64bit, Loongson-3A5000)
```

---

## CPU0 与 CPU3 状态分析（virsh dump 特殊情况）

由于 vmcore 通过 `virsh dump` 采集（内存快照），**CPU0 和 CPU3 的 per-CPU 内核栈在 vmcore 中为空**，crash 工具无法展示其堆栈。状态信息只能从 dmesg ring buffer 还原。

### CPU0 状态（来自 RCU stall 报告）

```
rcu: 0-...!: (23 ticks this GP) idle=59a4/0/0x3
             softirq=314610332/314610332 fqs=0
```

| 字段 | 值 | 含义 |
|---|---|---|
| `23 ticks this GP` | 23 | 本 Grace Period 内仅收到 23 个 tick，CPU0 几乎静止 |
| `idle=59a4/0/0x3` | nohz_state=0x3 | CPU0 处于 **DYNTICK_TASK_ENTER_IDLE** 状态 |
| `softirq=314610332/314610332` | 前后相同 | softirq 计数**完全冻结**，CPU0 未处理任何软中断 |
| `fqs=0` | 0 | CPU0 未能响应 RCU Force Quiescent State |

**NMI 结果**：所有尝试向 CPU0 发送 NMI backtrace 的操作均失败：
```
Sending NMI from CPU N to CPUs 0:
Unable to send backtrace IPI to CPU0 - perhaps it hung?
```
（在日志中出现超过 5 次，来自不同发送 CPU）

**结论**：CPU0 在其他 CPU 发出首个 TLB shootdown IPI 之前，已经进入无法响应中断（包括 NMI）的状态，是整个连锁反应的**根源**。

### CPU3 状态（来自 RCU stall 报告）

```
rcu: 3-...!: (1 GPs behind) idle=a644/1/0x4000000000000000
             softirq=161060137/161060137 fqs=0
```

| 字段 | 值 | 含义 |
|---|---|---|
| `1 GPs behind` | 落后 1 个 GP | CPU3 未完成上一轮 RCU QS 汇报 |
| `idle=0x4000000000000000` | DYNTICK_TASK_OLDROOT | CPU3 处于 NOHZ 扩展 idle 或 RCU 视其为 idle |
| `softirq=161060137/161060137` | 前后相同 | softirq 计数冻结，CPU3 完全静止 |
| `fqs=0` | 0 | CPU3 同样无响应 |

**NMI 结果**：
```
Sending NMI from CPU 1 to CPUs 3:
Unable to send backtrace IPI to CPU3 - perhaps it hung?
```

**结论**：CPU3 与 CPU0 性质相同，均对 NMI 无响应。crash 快照中 CPU3 的 current 任务为 telegraf，说明 CPU3 在某个执行路径中卡死，未记录堆栈信息。

> **注**：`info_register_a.txt`（位于 macOS 本地 `/Users/wangfuqiang49/workspace/kernel/openeuler-2403-sp2/kernel/`）中可能包含 CPU0/CPU3 的完整寄存器快照，可将其内容补充到此处进一步分析。

### CPU0/CPU3 卡死的可能原因（按可能性排序）

1. **虚拟化层 VCPU 调度异常**（最可能）：JD JCloud Jvirt 宿主机将 VCPU 挂起或迁移时，guest 内对应 CPU 的中断注入通道被切断，导致 IPI/NMI 均无法投递。

2. **关中断临界区内被 hypervisor 抢占**：CPU0/CPU3 在某个 `local_irq_disable()` 临界区内被 hypervisor 调度走，导致 IPI 队列中的请求无人处理。

3. **LoongArch64 平台 IPI/NMI 投递 bug**：Loongson-3A5000 的 IPI 通过 IOCSR 接口实现，KVM 模拟层若存在 bug，可能造成特定 VCPU 的 NMI 丢失。

---

## 次生影响

### RCU stall

`rcu_sched` kthread 因长期得不到 CPU 时间，触发严重 RCU stall：

```
rcu: rcu_sched kthread starved for 203,890,287 jiffies!
     g748848265 f0x2 RCU_GP_WAIT_FQS(5) ->state=0x0 ->cpu=0
```

203,890,287 jiffies（HZ=250 时约 815,561 秒，即约 9.4 天等效积累）。

rcu_sched 的调用栈：

```
kthread
  └─ rcu_gp_kthread
       └─ rcu_gp_fqs_loop
            └─ schedule_timeout
                 └─ schedule  ← 正常等待状态，被其他 CPU 饥饿
```

### 运行队列严重积压

CPU1 运行队列中积压了 40+ 个 RUNNABLE 任务，包括大量 `wrk`、`iperf`、`nginx`、JVM 线程等，系统完全失去调度能力。

---

## 时间线

| 时间戳（s） | 事件 |
|---|---|
| ~2,058,430 | **最早的 RCU stall 出现**（CPU0/CPU3 已无响应） |
| ~2,060,549 | CPU1/5 上开始出现 `madvise` → `flush_tlb_range` 路径 |
| ~2,066,211 | 首批 soft lockup 预兆（dmesg 出现 smp_call_function_many_cond 调用栈） |
| **2,066,215** | **首次 soft lockup 报告**：CPU5（jdog-monitor，卡 5666s）、CPU6（VM Thread，卡 5681s） |
| 2,066,231 | CPU7 soft lockup（JCSAgentCore，卡 5722s） |
| 2,066,235 | CPU2 soft lockup（jdog-kunlunmirr，卡 5643s） |
| 2,066,239 | CPU4 soft lockup（VM Thread，卡 5133s）、CPU1 soft lockup（MonitorPlugin，卡 5725s） |
| 2,066,386 | RCU 检测到 stall，尝试向 CPU0 发 NMI → 失败 |
| 2,066,396 | 尝试向 CPU3 发 NMI → 失败 |
| 2,066,406 | rcu_sched 报告饥饿 203,890,287 jiffies |
| **2026-04-28 17:03:54** | virsh dump 采集时刻（崩溃/采集时间） |

---

## 问题定性

这是一个 **TLB shootdown IPI 风暴 × CPU hung 引发的全局 soft lockup** 问题，属于虚拟化环境下的复合故障：

```
根源：CPU0（可能 CPU3）被 hypervisor 挂起/死锁，无法响应 IPI
  ↓
高负载下大量 madvise/mprotect/CoW 触发 TLB shootdown IPI → CPU0
  ↓
smp_call_function_many_cond 等待 CPU0 完成，永久自旋
  ↓
CPU1/2/4/5/6/7 全部卡死
  ↓
RCU Grace Period 无法推进，softirq 无法执行
  ↓
系统完全失去响应
```

---

## 排查与修复建议

### 短期排查

1. **检查宿主机 Hypervisor 日志**：查看 JD JCloud Jvirt 宿主机在 2026-04-28 ~17:00 前后的 VCPU 迁移、挂起、热迁移、内存气球操作记录，定位 CPU0 VCPU 挂死的直接原因。

2. **确认 CPU0/CPU3 寄存器状态**：将 `info_register_a.txt` 内容补充到本报告，分析 CPU0/CPU3 挂死时的 PC/ERA，确认是否卡在特定内核函数中。

### 中期优化

3. **降低 TLB shootdown 频率**：
   - 检查 Java 应用（JVM 线程大量存在：C2 CompilerThread、VM Thread、VM Periodic Task）是否存在大内存批量释放场景（如 G1 GC、大堆 madvise 释放），考虑调整 JVM GC 参数（`-XX:+UseLargePages`、减少 `madvise(DONTNEED)` 调用）。
   - 检查 `wrk` / `iperf` 等压测工具是否在进行大规模内存映射操作。

4. **内核参数调整**：
   ```bash
   # 降低 TLB shootdown 批量触发压力
   echo 1 > /proc/sys/vm/lazy_tlb_flush   # 如内核支持
   # 适当降低 softlockup 阈值告警敏感度（非根本解决）
   echo 60 > /proc/sys/kernel/watchdog_thresh
   ```

### 长期（内核层面）

5. **为 `smp_call_function_many_cond` 添加超时保护**：当前 LoongArch64 6.6 内核的 `smp_call_function_many_cond` 在等待远端 CPU 响应时没有超时机制，一旦目标 CPU hung 死，等待者永远无法退出。部分架构（如 x86）已有相关 fallback 机制，建议向 openEuler/上游社区反馈或提交补丁。

6. **关注 KVM/LoongArch IPI 投递可靠性**：确认 Loongson-3A5000 在 KVM 虚拟化下 IOCSR IPI 接口的可靠性，必要时升级 QEMU/KVM 版本。

---

## 附录：关键函数偏移

| 函数 | 地址 | 偏移 |
|---|---|---|
| `smp_call_function_many_cond` 卡死点 | `0x90000000003493c0` | +0x420 |
| `smp_call_function_many_cond` 返回地址 | `0x9000000000349394` | +0x3f4 |
| `on_each_cpu_cond_mask` | `0x90000000003495a0` | +0x20 |
| `flush_tlb_range` | `0x9000000000233648` | +0x88 |
| `flush_tlb_page` | `0x90000000002327d4` | +0x7c |
| `tlb_flush_mmu` | `0x9000000000525a48` | +0x80 |
| `tlb_finish_mmu` | `0x9000000000525da8` | +0x50 |
| `zap_page_range_single` | `0x90000000005103f0` | +0x130 |
| `madvise_dontneed_free` | `0x9000000000551ab4` | +0x18c |
| `wp_page_copy` | `0x9000000000510ae0` | +0x2d0 |
| `ptep_clear_flush` | `0x900000000052b580` | +0x78 |
| `do_mprotect_pkey` | `0x9000000000527308` | — |
