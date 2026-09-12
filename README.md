# sl8541e-kernel-builder

给 **SL8541E / SC9832E（sharklE）** 手表编内核，并直接产出可刷的 `boot.img`。

默认走"原厂 ABI"那套（这样原厂 ramdisk 里的模块 vermagic 能直接对上）：

| 项 | 值 |
|---|---|
| 内核仓库 | `zzzxxxxxxxxxx/android_kernel_sprd_sl8541e_4.4.83`（`sprd-linux/Linux-4.4.83` 的 fork） |
|  分支 | `WIP` |
| 配置 | `configs/dw99-4.4.83.config`，从设备 boot.img 里提取的原厂 config |

想换 LineageOS 17.1 那套（4.4.147）：`kernel_repo=zzzxxxxxxxxxx/android_kernel_sprd_sc9832e`、
`kernel_branch=lineage-17.1`、`defconfig=lineageos_dw99_defconfig`。

克隆完会把 `kernel/.git` 挪成 `kernel/.git-off`：留着的话 `scripts/setlocalversion`
会给版本串加上 `-g<sha>` 或 `+`（`CONFIG_LOCALVERSION_AUTO=y`），`UTS_RELEASE`
就不是干净的 `4.4.83`，vermagic 和原厂模块对不上。

## 产物

`boot-*.img`：原厂 boot.img 的 header + ramdisk + dtb **原样保留**，只把
kernel 换成新编的 `Image`；如果模块编出来了，还会把 `lib/modules/*.ko`
一起换进 ramdisk（vermagic 才对得上）。

已验证：用原 kernel 重新打包，产物和原 boot.img **前 20,072,448 字节逐字节一致**
（原件只是填零到 36,700,160）。

## 怎么用

Actions → `kernel` → Run workflow。常用参数：

| 输入 | 说明 |
|---|---|
| `kernel_repo` | 内核仓库，默认 `zzzxxxxxxxxxx/android_kernel_sprd_sl8541e_4.4.83` |
| `kernel_branch` | 默认 `WIP` |
| `defconfig` | 留空 = 用 `configs/dw99-4.4.83.config`；也可填内核仓库里的 defconfig 名 |
| `extra_config` | 追加到 `.config` 的行，例如 `CONFIG_BPF_SYSCALL=y` |
| `build_modules` | 是否编模块（失败不阻断） |
| `dt_url` | 换 dt（留空 = `prebuilt/dt.bin`，即原厂那份） |
| `ramdisk_url` | 换 ramdisk（留空 = `prebuilt/ramdisk.gz`，即原厂那份） |
| `cmdline` | 覆盖 boot header 的 cmdline（留空 = 原厂 `console=ttyS1,115200n8 buildvariant=user`） |
| `out_name` | 产物文件名 |

## 里面的文件

| 路径 | 说明 |
|---|---|
| `configs/dw99-4.4.83.config` | 从 DW99 的 boot.img 提取的设备真实内核配置（4,258 行） |
| `prebuilt/header.bin` | 原厂 boot.img 的 2048 字节 header（cmdline/地址） |
| `prebuilt/ramdisk.gz` | 原厂 ramdisk（含 fstab、init.rc、四个 .ko） |
| `prebuilt/dt.bin` | 原厂 dt blob（面板配置，保持不动） |
| `scripts/boot_repack.py` | 用上面这些重新拼 boot.img，可替换/新增 ramdisk 文件 |

## 设备内核的现状（提取自 config）

```
CONFIG_PSTORE=y            CONFIG_PSTORE_CONSOLE=y
CONFIG_PSTORE_RAM=y        CONFIG_PSTORE_PMSG=y
CONFIG_USB_CONFIGFS=y      CONFIG_USB_F_FS=y
CONFIG_F2FS_FS=y           CONFIG_SECURITY_SELINUX=y
# CONFIG_BPF_SYSCALL is not set        ← 想跑 Android 12 需要它
```

pstore console 本来就是开的；`/sys/fs/pstore` 里看不到 `console-ramoops-0`
多半是因为设备是**断电式重启**（RAM 被清）而不是内核没编。

## 注意

- 刷了这个 boot.img 会**丢掉 FolkPatch 的内核补丁**（root）。要 root 的话，
  刷完再用 FolkPatch 对新的 boot.img 打一次补丁。
- 想换回原厂：`dd if=boot.img of=/dev/block/by-name/boot bs=4M`。
## 工具链

设备内核的版本串是：

```
Linux version 4.4.83 (powersys@X99-5)
  (gcc version 4.9.x 20150123 (prerelease) (GCC) ) #1 SMP PREEMPT Sun May 5 11:13:00 CST 2024
```

也就是 **AOSP 的 `aarch64-linux-android-4.9`** 预编译工具链。workflow 直接用
LineageOS 镜像的那两份（`aarch64` + `arm`，后者是因为 `CONFIG_COMPAT=y` 要编
vdso32），不再用系统自带的 GCC 11 —— 那个编 4.4 老内核会一堆 `-Werror` 报错。
