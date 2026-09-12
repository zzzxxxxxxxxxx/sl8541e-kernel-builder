# sl8541e-kernel-builder

给 **SL8541E / SC9832E（sharklE）** 手表编 4.4.83 内核，并直接产出可刷的
`boot.img`。用的配置不是源码里的通用 defconfig，而是**从设备自己的
`boot.img` 里提取出来的真实 config**（`scripts/extract-ikconfig` 等价做法）。

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
| `kernel_repo` | 内核仓库，默认 `zzzxxxxxxxxxx/Linux-4.4.83` |
| `kernel_branch` | 默认 `WIP` |
| `extra_config` | 追加到 `.config` 的行，例如 `CONFIG_BPF_SYSCALL=y` |
| `build_modules` | 是否编模块（失败不阻断） |
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
- 用新 GCC 编 4.4 老内核常见 `multiple definition` 报错，workflow 里已经加了
  `KCFLAGS=-fcommon`；如果还是过不去，换 AOSP 的
  `aarch64-linux-android-4.9` 预编译工具链。
