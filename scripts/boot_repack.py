#!/usr/bin/env python3
"""
Assemble an Android boot image (v0 layout) from:
  * header.bin  - the original 2048-byte header page (keeps cmdline/addresses)
  * kernel      - a freshly built Image
  * ramdisk.gz  - the original gzip cpio ramdisk
  * dt.bin      - the original dt blob (SPRD container, kept verbatim)

Optionally replaces/adds files inside the ramdisk, which is what you want for
freshly built kernel modules (the ramdisk ships lib/modules/*.ko and they must
match the new kernel's vermagic).

usage:
  boot_repack.py --header hdr.bin --kernel Image --ramdisk ramdisk.gz \\
                 --dt dt.bin [--replace-dir dir]... --out boot.img

Each --replace-dir is walked recursively; a file at <dir>/lib/modules/x.ko
replaces (or adds) the ramdisk entry "lib/modules/x.ko".
"""
import argparse
import gzip
import io
import os
import struct
import sys


def align(n, a):
    return (n + a - 1) // a * a


def cpio_entries(buf):
    off = 0
    while True:
        if buf[off:off + 6] != b"070701":
            raise SystemExit("bad cpio magic at %d" % off)
        hdr = buf[off:off + 110]
        fields = [int(hdr[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        size, namesize = fields[6], fields[11]
        name_off = off + 110
        name = buf[name_off:name_off + namesize - 1]
        data_off = align(name_off + namesize, 4)
        data = buf[data_off:data_off + size]
        yield name, hdr, data
        if name == b"TRAILER!!!":
            return
        off = align(data_off + size, 4)


def build_cpio(entries):
    out = io.BytesIO()
    for name, hdr, data in entries:
        hdr = bytearray(hdr)
        hdr[6 + 6 * 8:14 + 6 * 8] = b"%08X" % len(data)
        hdr[6 + 11 * 8:14 + 11 * 8] = b"%08X" % (len(name) + 1)
        out.write(bytes(hdr))
        out.write(name + b"\x00")
        out.write(b"\x00" * (align(110 + len(name) + 1, 4) - (110 + len(name) + 1)))
        out.write(data)
        out.write(b"\x00" * (align(len(data), 4) - len(data)))
    return out.getvalue()


_ino = [0x71000000]


def newc_header(name, size, mode=0o100644):
    _ino[0] += 1
    fields = [_ino[0], mode, 0, 0, 1, 0, size, 0, 0, 0, 0, len(name) + 1, 0]
    return b"070701" + b"".join(b"%08X" % f for f in fields)


def page_size_of(header):
    page = struct.unpack_from("<I", header, 0x24)[0]
    if page not in (2048, 4096, 8192, 16384):
        raise SystemExit("unexpected page size %d in header.bin" % page)
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--header", required=True)
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--ramdisk", required=True)
    ap.add_argument("--dt", required=True, help="original dt blob (kept verbatim)")
    ap.add_argument("--replace-dir", action="append", default=[],
                    help="directory tree whose files replace/add ramdisk entries")
    ap.add_argument("--device-tree-size-field", action="store_true",
                    help="also write the dt size into the header (v0 dt_size)")
    ap.add_argument("--cmdline", default=None,
                    help="overwrite the cmdline field in the header (max 511 bytes)")
    ap.add_argument("--pad-to", type=int, default=0,
                    help="zero-pad the output to this size (use the partition size)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    header = bytearray(open(args.header, "rb").read())
    page = page_size_of(header)
    kernel = open(args.kernel, "rb").read()
    dt = open(args.dt, "rb").read()
    ram_gz = open(args.ramdisk, "rb").read()

    if ram_gz[:2] != b"\x1f\x8b":
        raise SystemExit("ramdisk is not gzip")

    # ---- optional ramdisk file replacement ----
    replace = {}
    for d in args.replace_dir:
        for root, _dirs, files in os.walk(d):
            for n in files:
                p = os.path.join(root, n)
                rel = os.path.relpath(p, d).replace(os.sep, "/")
                replace[rel.encode()] = open(p, "rb").read()

    if replace:
        cpio = gzip.decompress(ram_gz)
        entries, done = [], set()
        for name, hdr, data in cpio_entries(cpio):
            if name in replace:
                print("   replace %-28s %8d -> %8d" % (name.decode(), len(data),
                                                       len(replace[name])))
                data = replace[name]
                done.add(name)
            entries.append((name, hdr, data))
        missing = set(replace) - done
        if missing:
            idx = next(i for i, (n, _, _) in enumerate(entries)
                       if n == b"TRAILER!!!")
            for name in sorted(missing):
                print("   add     %-28s %8d" % (name.decode(), len(replace[name])))
                entries.insert(idx, (name, newc_header(name, len(replace[name])),
                                     replace[name]))
                idx += 1
        new_cpio = build_cpio(entries)
        ram_gz = gzip.compress(new_cpio, compresslevel=9, mtime=0)
        print("   ramdisk: %d -> %d bytes (gzip)" % (len(gzip.decompress(
            open(args.ramdisk, "rb").read())), len(gzip.decompress(ram_gz))))

    # ---- write sizes into the header page ----
    struct.pack_into("<I", header, 0x08, len(kernel))
    struct.pack_into("<I", header, 0x10, len(ram_gz))
    if args.device_tree_size_field:
        struct.pack_into("<I", header, 0x28, len(dt))
    if args.cmdline is not None:
        cb = args.cmdline.encode()
        if len(cb) > 511:
            raise SystemExit("cmdline too long (%d bytes)" % len(cb))
        header[0x40:0x240] = cb + b"\x00" * (512 - len(cb))
        print("   cmdline: %s" % args.cmdline)

    out = bytearray()
    out += header
    out += kernel
    out += b"\x00" * (align(len(out), page) - len(out))
    out += ram_gz
    out += b"\x00" * (align(len(out), page) - len(out))
    out += dt
    out += b"\x00" * (align(len(out), page) - len(out))

    if args.pad_to:
        if args.pad_to < len(out):
            raise SystemExit("--pad-to %d is smaller than the image (%d)"
                             % (args.pad_to, len(out)))
        out += b"\x00" * (args.pad_to - len(out))

    open(args.out, "wb").write(out)
    print("wrote %s: %d bytes (kernel %d, ramdisk %d, dt %d)"
          % (args.out, len(out), len(kernel), len(ram_gz), len(dt)))


if __name__ == "__main__":
    main()
