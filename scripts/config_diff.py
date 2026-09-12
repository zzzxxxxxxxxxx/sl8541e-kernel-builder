#!/usr/bin/env python3
"""Compare an input .config with the result of `make olddefconfig`.

Symbols that vanished were not defined by that kernel tree's Kconfig at all,
so the number of dropped symbols is a decent proxy for "how far is this tree
from the one the config came from".

usage: config_diff.py input.config kernel/.config
"""
import sys


def load(path):
    out = {}
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line[2:].split()[0]] = "n"
        elif "=" in line and line.startswith("CONFIG_"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def main():
    a, b = load(sys.argv[1]), load(sys.argv[2])
    dropped = [k for k in a if k not in b]
    changed = [(k, a[k], b[k]) for k in a if k in b and a[k] != b[k]]
    added = [k for k in b if k not in a]

    print("input=%d resulting=%d" % (len(a), len(b)))
    print("DROPPED=%d CHANGED=%d ADDED=%d" % (len(dropped), len(changed), len(added)))

    print("--- dropped (not defined by this tree at all) ---")
    for k in dropped:
        print("   - %s=%s" % (k, a[k]))
    print("--- changed ---")
    for k, x, y in changed:
        print("   ~ %s: %s -> %s" % (k, x, y))


if __name__ == "__main__":
    main()
