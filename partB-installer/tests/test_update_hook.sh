#!/bin/sh
# Executes the write helpers out of payload/flash/user-update.sh for real.
#
# tests/test_boot_gate.py reads the hook as TEXT -- it can prove the shape of the code but not
# that it runs. These helpers gate a boot-critical write in an initramfs where a mistake is
# unattended and unrecoverable without a PC, so they get executed here against real files.
#
# Run: sh tests/test_update_hook.sh
set -u
HERE=$(dirname "$0")
HOOK="$HERE/../payload/flash/user-update.sh"
WORK=${TMPDIR:-/tmp}/user-update-test.$$
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

ENV_PROBE="$WORK/.probe.$$"
DD_LOG="$WORK/.ddlog.$$"
log() { echo "[hook] $*"; }

# Pull the helpers out of the shipped hook verbatim, so this tests the text that ships and
# cannot drift from it.
sed -n '/^file_size() {/,/^}/p'      "$HOOK" >  "$WORK/fns.sh"
sed -n '/^has_byte_at() {/,/^}/p'    "$HOOK" >> "$WORK/fns.sh"
sed -n '/^write_verified() {/,/^}/p' "$HOOK" >> "$WORK/fns.sh"
for fn in file_size has_byte_at write_verified; do
  grep -q "^$fn() {" "$WORK/fns.sh" || { echo "FAIL: $fn is no longer in the hook"; exit 1; }
done
. "$WORK/fns.sh"

fail=0

# --- file_size: dd + [ -s ] only, so it must be exact at the block edges -----------------
t_size() {
  dd if=/dev/urandom of="$WORK/f" bs=1 count="$1" 2>/dev/null
  got=$(file_size "$WORK/f")
  if [ "$got" = "$1" ]; then echo "  ok    file_size($1)"
  else echo "  FAIL  file_size($1) returned $got"; fail=1; fi
}
echo "file_size (the size the record check is compared against):"
for n in 0 1 2 511 512 513 65536 100000; do t_size "$n"; done

# --- write_verified: accepts good writes, at both block alignments ------------------------
echo "write_verified accepts a good write:"
dd if=/dev/urandom of="$WORK/aligned" bs=512 count=194 2>/dev/null      # exact multiple of 512
if write_verified "$WORK/aligned" "$WORK/o1" "aligned" >/dev/null && cmp -s "$WORK/aligned" "$WORK/o1"
then echo "  ok    512-aligned source"; else echo "  FAIL  512-aligned source"; fail=1; fi

dd if=/dev/urandom of="$WORK/odd" bs=1 count=100001 2>/dev/null          # forces "N+1 records out"
if write_verified "$WORK/odd" "$WORK/o2" "unaligned" >/dev/null && cmp -s "$WORK/odd" "$WORK/o2"
then echo "  ok    non-512-multiple source"; else echo "  FAIL  non-512-multiple source"; fail=1; fi

# --- write_verified: refuses sources it cannot trust --------------------------------------
echo "write_verified refuses an unusable source:"
if write_verified "$WORK/nope" "$WORK/o3" "missing" >/dev/null 2>&1
then echo "  FAIL  accepted a missing source"; fail=1; else echo "  ok    missing source"; fi
: > "$WORK/empty"
if write_verified "$WORK/empty" "$WORK/o4" "empty" >/dev/null 2>&1
then echo "  FAIL  accepted a 0-byte source"; fail=1; else echo "  ok    0-byte source"; fi

# --- the failure this helper exists for ---------------------------------------------------
# A stick was stranded when this dd wrote all but the last 659,456 bytes of kernel.img to
# boot_b and exited 0. The kernel region matched, the tail of the zstd initramfs was left over
# from the previous kernel, and the kernel panicked on a missing /init. Shadow dd to reproduce
# that shape (short copy, honest stderr, exit 0) and prove the guard catches it.
echo "write_verified catches the short write that caused the incident:"
dd() {
  for a in "$@"; do
    case "$a" in bs=1) command dd "$@"; return $? ;; esac   # file_size's probes pass through
  done
  command dd if="$WORK/odd" of="$WORK/o5" bs=512 count=100 2>/dev/null
  echo "100+0 records in"  >&2
  echo "100+0 records out" >&2
  echo "51200 bytes (50.0KB) copied, 0.001 seconds, 50.0MB/s" >&2
  return 0
}
if write_verified "$WORK/odd" "$WORK/o5" "short" >/dev/null 2>&1
then echo "  FAIL  accepted a short write -- the guard does not work"; fail=1
else echo "  ok    short write rejected"; fi
unset -f dd

echo
if [ "$fail" = 0 ]; then echo "all update-hook helper checks passed"; else echo "UPDATE-HOOK FAILURES"; fi
exit "$fail"
