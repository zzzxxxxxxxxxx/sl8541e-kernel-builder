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
