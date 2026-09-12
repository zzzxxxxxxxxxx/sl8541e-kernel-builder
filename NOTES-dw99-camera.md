# DW99 相机 + 内核排查笔记（2026-09-12）

这份笔记记录"为什么底包（SL8541E_4.4.147_VNDK28）上相机用不了"以及过程中
修掉的内核 bug。所有结论都有实测证据，不是推测。

## 1. 内核侧：修掉的 5 个问题

内核 fork：`zzzxxxxxxxxxx/android_kernel_sprd_sc9832e`，分支 `iommu-fix`
（基于 `lineage-17.1`，5 个提交）。

| 提交 | 问题 |
|---|---|
| `da578c40` | `sprd_iommu_map()` 缓存命中只比 `buf` 不比 `size`，而 unmap 按 `(iova,size)` 比 → 同一个 buffer 用不同长度映射时拿到旧 IOVA |
| `fb4b7b30` | sg_table 路径（相机 CPP 走这条）完全没有 ion 生命周期跟踪：`sprd_ion_set_dma()` 只在 buf 路径调用、`cpp_get_addr()` 连 `buf` 都不填，ion_buffer 销毁时 orphan 清理被跳过 |
| `2696b679` | **`sprd_iommuex_cll_unmap_orphaned()` 只清页表、不刷 TLB**（正常 unmap 路径会 `mmuex_tlb_enable` / `mmu_ex_tlb_update`）。这条路径恰恰是"buffer 已死、页即将还给系统"的情况，TLB 里留着旧翻译就会继续往回收页写 |
| `1881fc5f` | `/proc/dw99_iommu_trace`：记录相机 master（DCAM/DCAM1/CPP/JPG/ISP/ISP1）每次 MAP/UNMAP/ORPHAN 的 `iova/size/buf/sg/caller`，4096 环形缓冲 |
| `a6b755da` | ISP 统计缓冲区地址诊断：`isp_int.c` 把 `node.phy_addr`（用户态 ioctl 传入）写进 `ISP_AEM_DDR_ADDR` 等寄存器，与内核映射出来的 IOVA 不一致时打点 |

实测效果：修 `da578c40`+`fb4b7b30`+`2696b679` 之后
`sprd_iommu_pool_show()` 的 `Warning! buffer ... should be unmapped!`
从每次 4 条变成 0 条，trace 里 map/hit/unmap 计数平衡。

## 2. 相机为什么用不了（结论）

### 2.1 两套相机栈，各有各的坏法

**底包自带（`vendor.img` 里那套）**：`libispalg 04fbbc43` / `libae 4ffa0bfa`
/ `libcamoem 29a34442` / `libcamera_client c33c4318` / `libmemion 37d11f8a`
/（独有）`libcam_otp_parser 19d2547e`。开相机时一层层崩：

1. `awb_ctrl_init+300`：adapter 返回成功但 ops 指针是 NULL，这版没做判空。
   2 字节补丁可过（`0x17638`：`ldr r2,[r0]` → `movs r2,#0`）。
2. `ae_sprd_init+1282`：拷 8×**10000** 字节，源缓冲区只有 **8704** → 读到
   scudo 保护页。改成 **8212**（8.1 版循环用的就是 8212）可过。
3. 过了这两关后 sensor 能正常打开（`sensor_open: open success`，
   `sp0a09f_mipi_raw` 640x480），相机设备 392ms 打开成功，然后 aborts 在
   `libcamdrv.so isp_alg_fw_capability+130`：**`stack corruption detected
   (-fstack-protector)`** —— capability 结构把栈写爆，说明算法库和它拿到的
   结构不是一代的。

**原厂 8.1 vendor（DW99_20240716）那套**：`libispalg 348ec48f` /
`libae 3f7db919` / `libawb1 b2f4514c` / `libcamdrv 71fbae43` /
`libcamsensor 146be063` …（`port108` 那 21 个文件就是这套**改依赖名**后的
产物：`libgui.so`→`gui8.so`、`libui.so`→`ui8.so`；改名后用原厂库 md5 对得上）。
这套能出帧（实测 25 帧），但会把系统踩进 TWRP。

原厂那套库**不能直接整套搬**：原厂 HAL `8fbf6d88` 需要 `libpowermanager.so`、
原厂 `libsprdfd.so` 需要 `libstdc++.so`、原厂 `device@3.2.so` 需要
`graphics.common@1.0.so` —— 这些 8.1 时代的库 VNDK28 底包里都没有，
所以只能用 port108 那套去依赖的移植版。

### 2.2 踩内存的机制

受害者签名高度一致：system_server 崩在 `__epoll_pwait`、camera2 崩在
`__ioctl`、mediametrics 崩在 `__ioctl`、wifi 崩在 `__ppoll`——都是
**系统调用包装里用了被写坏的指针**（堆上的 epoll 数组 / binder 缓冲）。
system_server 一崩，Android 的 boot-failure 计数就到顶，bootloader 直接
`reboot: Restarting system with command 'recovery'`（pstore 里能看到，不是内核 panic）。

**机制**：`isp_int.c` 把 `ISP_AEM_DDR_ADDR`、anti-flicker、PDAF、binning 的
DDR 地址写成 `node.phy_addr`，而这个值来自用户态 ioctl（`isp_buf.c:293`
`frm_statis.phy_addr = parm->phy_addr`）。IOMMU 池是 `0x50000000-0x60000000`，
而 carveout 物理地址是 `0xbdf6xxxx` 这种——不在池里却能工作，说明这些统计
引擎就是**按物理地址直接 DMA、绕过 IOMMU**。所以只要 HAL 在硬件还会写的时候
释放/复用这些 carveout buffer，硬件就写进已经还给系统、随后被 page cache
或其他进程拿走的物理页。

这也解释了为什么 IOMMU 那层看起来完全干净（trace 144 个事件只有 1 次
orphan 且被正常回收）：**这条写路径根本不经过 IOMMU，查不到**。

## 3. 构建参数（复现用）

```
kernel_repo    = zzzxxxxxxxxxx/android_kernel_sprd_sc9832e
kernel_branch  = iommu-fix
config_file    = configs/dw99-4.4.147-vndk28.config
build_modules  = false
dt_url         = <raw>/prebuilt/vndk28/dt.bin
ramdisk_url    = <raw>/prebuilt/vndk28/ramdisk.gz
```

要点：

- **ramdisk/dt 必须用底包那套**（`1369774` / `280576`）。默认的
  `prebuilt/ramdisk.gz`、`prebuilt/dt.bin` 是 DW99 原厂 4.4.83 的
  （`1374689` / `278528`），拿它拼出来的 boot.img 直接进 fastboot。
- `build_modules=false`：开了的话 workflow 会把新编的
  `sprdwl_ng.ko`（4.5MB、没 strip）塞进 ramdisk，把它从 3.4MB 撑到 7.7MB，
  同样起不来。要么 strip 模块要么别替换。
- 编出来的镜像头里 `os_version` 字段与底包不同（`268697915` vs
  `301990300`），实测不影响启动；要完全对齐就把偏移 `0x2c` 那 4 字节拷成底包的值。

ccache 已接好（`CCACHE_SLOPPINESS=time_macros,include_file_mtime,
include_file_ctime,file_stat_matches` + `CCACHE_COMPILERCHECK=content`）：
同一分支+同一配置第二次开始 **2.4 分钟**（命中 99.2%），冷缓存 6–7 分钟。
只抄 Watch-GKI 那几行（NOHASHDIR/HARDLINK/NOCOMPRESS）命中率只有 1%——
内核每次重新生成 `include/generated/*`，mtime/ctime 一变 manifest 就整体失效。

## 4. 设备侧的必备配置

- **HIDL stub 必须是 A11（VNDK-28）那对**：`android.hardware.camera.provider@2.4.so
  = 82546e43`、`android.hardware.camera.device@3.2.so = 4f6e724e`。只换一个会
  `Unable to enumerate camera device`；被 `cam_81.sh` 里的 8.1 版 provider stub
  覆盖后会 linker 报 `cannot locate symbol ...toString(PixelFormat)` 直接崩循环。
- `cam_81.sh` 安装的是 port108 移植版 8.1 栈；`cam_stock.sh`/`a9backup` 是底包原版。
- `/data/adb/service.d/99-fix-systemui.sh`：Rescue Party 会重置运行期权限，
  SystemUI 丢了 `READ_CONTACTS` 就 inflate 不了 `super_notification_shade`，
  表现是黑壁纸+无导航手势+通知栏划不下来。这个脚本开机自动补权限。
- 备份都在 `/data/local/tmp/`：`a9backup/`（底包原版 18 个）、`port108/`（8.1 移植版 21 个）、
  `basecam/`（从 vendor.img 抽出的底包整套）、`boot-before-*.img`（各阶段 boot 分区）。

## 5. 还没做的（如果要继续）

1. **把统计 DMA 变成安全路径**：给 `isp_int.c` 里写 `node.phy_addr` 的地方加
   buffer 固定（硬件还在写时不让 ion_buffer 释放），或者 buffer 销毁时把寄存器
   改写到驱动自留的 `statis_buf_reserved`。这是唯一可能让"能出帧"和"不踩内存"
   同时成立的改法，工作量中等、风险中等。
2. **补 `isp_alg_fw_capability` 的栈溢出**：需要逆 libcamdrv 那个函数，且要
   知道 capability 结构的期望大小（A9 库返回的比 libcamdrv 的栈缓冲大）。
3. 想要"立刻可用"的相机：刷原厂 8.1 的 `vendor.img`（`DW99_20240716`）配
   LOS 17.1 GSI——那边整套是自洽的。

## 6. r2 反编译的成果（底包那套继续往下修）

底包那套的三处崩点里，前两处是改 libispalg 的字节，第三处是**用 r2 反编译
`libcamdrv.so` 找到并修掉的**：

`isp_alg_fw_capability()`（0x16e14，204 字节，有符号）反编译后是这样：

```c
uint isp_alg_fw_capability(ctx, which, out)
{
	uint slot = 0;                       /* 4 字节栈槽 */
	...
	slot = (*ops->capability)(ctx->dev, cmd /*0x32/0x35/0x2c*/, 0, &slot);
	*out = slot;
}
```

它把一个**只有 4 字节的栈槽**当作输出缓冲交给算法库的能力查询函数，而 A9 那版
算法库往这个指针里写的内容超过 4 字节，紧跟其后的**栈 canary 就被盖掉**，
函数返回时 `__stack_chk_fail` → `stack corruption detected (-fstack-protector)`。
同一个 libcamdrv（8.1 栈用的是同一个文件，md5 `71fbae43`）在 8.1 算法库上不炸，
因为 8.1 库只写指针大小——这是又一处"A9 算法库 vs 其它组件"的代差。

补丁（5 条指令，`libcamdrv_capfix.so`，md5 `a05edd94`）：

| 地址 | 原 | 改 |
|---|---|---|
| 0x16e16 | `sub sp, 0x10` (`84b0`) | `sub sp, 0x40` (`90b0`) |
| 0x16e40 / 0x16e52 / 0x16e64 | `add r3, sp, 8` (`02ab`) | `add r3, sp, 0x20` (`08ab`) |
| 0x16e72 | `ldr r0, [sp, 8]` (`0298`) | `ldr r0, [sp, 0x20]` (`0898`) |

（用 `rasm2 -a arm -b 16 "add r3, sp, 0x20"` 取编码，`r2 -w -c 's <addr>; wx <bytes>'` 写入。）

打完这处之后 `isp_alg_fw_capability` 不再崩，provider 继续往下走，露出下一个
崩点：**HAL 自己的陀螺仪线程** `sprdcamera::SprdCamera3OEMIf::gyro_ASensorManager_process`
里 NULL 解引用（fault addr 0x0）——那条路径走 `libsensorndkbridge.so`，
而 8.1 版那个库需要一整套 8.1 sensor HAL（`android.hardware.sensors@1.0.so`、
`libsensorservice.so`、`sensorcalibration.so`、`libsensor.so`…），VNDK28 底包里都没有。

配合前面那两处 libispalg 的补丁，底包相机栈现在的进度是：
**AWB 初始化 → AE 初始化（表大小 10000→8212）→ capability 查询（栈溢出）全部通过**，
卡在 HAL 的 sensor 桥调用上。每一步都是"这套库跟这个 ROM 不是一代的"的同一种病。

### 6.1 下一层：provider 自己的 GOT 槽被清成 0（确定性复现）

capability 补丁之后，再开相机不再崩在 capability 上，改成：

```
signal 11 (SIGSEGV), fault addr 0x0
#00 pc 00000000  <unknown>
#01 pc 00026707  /vendor/lib/libispalg.so (ae_sprd_io_ctrl+1654)
#02 pc 0004cf3e  [anon:scudo:primary]
```

`ae_sprd_io_ctrl+1654` = 0x26706，那条指令是走 PLT 的 `blx pthread_mutex_unlock`
（`sym.imp.pthread_mutex_unlock`）。`pthread_mutex_unlock` 是 libc 的普通符号，
不可能解析失败，所以**这个 GOT 槽在运行时是被写成 0 的**。连跑两次，崩点、
帧、故障地址完全一致——**确定性**，不是随机踩。

这条线索比之前"若干无关进程随机崩"窄得多：**踩内存首先发生在 provider 自己
的进程内**（它的 GOT / scudo anon 页），之后才是 systemui/cameraserver 那一批。
换句话说，ISP 往"自己的客户进程"的内存里写了东西——这跟前面观察到的
"受害者都是刚启动、刚加载库的进程"是同一件事的两种表现。

两处 libispalg 补丁（`libispalg_a9fix2.so`，md5 `da735787`）：

| 地址 | 原 | 改 | 原因 |
|---|---|---|---|
| 0x17638 | `ldr r2,[r0]` (`0268`) | `movs r2,#0` (`0022`) | adapter 返回成功但 ops 为 NULL，原版直接解引用 |
| 0x2a3a6 | `movw r2,#0x2710` (`42f21072`) | `movw r2,#0x2014` (`42f21402`) | 要拷 10000 字节，源缓冲区只有 8704（scudo 保护页就在后面） |
