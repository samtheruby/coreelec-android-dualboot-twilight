#!/usr/bin/env python3
"""
Staged installer/orchestrator for the CoreELEC internal dual-boot.

Two execution contexts: ANDROID phase (--serial, adb) and COREELEC phase
(--host, ssh, only after first CoreELEC boot). [Xiaomi]=twilight-only,
[generic]=any Google TV / CoreELEC box.

  ANDROID phase (--serial):
    stage_unlock  unlock the bootloader via fastboot            [Xiaomi]  DESTRUCTIVE
                   run BEFORE stage_magisk on a locked unit (most are).
                   fastboot flashing unlock + unlock_critical -> factory reset;
                   device must be RE-SETUP from scratch afterwards.
    stage_magisk  flash Magisk-patched init_boot (active slot) fastboot [Xiaomi]
                   run BEFORE stage1 -- gives root that stage1 requires.
                   Reboots to bootloader, flashes, reboots back to Android.
    stage1  CORE install -> first reboot     [Xiaomi]   GPT/CE/kernel/dtb/misc/env
                                                         + arm userdata reformat  DESTRUCTIVE
             without --yes: read-only preflight + write plan (dry-run)
             with --yes: pulls SHA-256-verified backups to pulled_backups/
             BEFORE the first write, then installs
    --- reboot: recovery reformats userdata, boots Android, then ---
    stage2  apps + modules                   [mixed]    RebootToCoreELEC APK,
              flash-recovery [Xiaomi], toolbox_export module [generic],
              blockgms GMS system-update block [generic]
    stage2a Xiaomi auto-update block         [Xiaomi]   OPTIONAL (blockota)
    verify  layout/env readiness             [Xiaomi]   read-only
  --- reboot into CoreELEC (first CE boot), enable SSH, then ---
  COREELEC phase (--host):
    stage3  CoreELEC-side setup              [mixed]    Toolbox addon [generic],
              Kodi sources PM4K+jamal2362 [generic], Xiaomi remote keymap [Xiaomi,
              auto-detected; --xiaomi forces, --no-keymap skips]

The [generic] stage3 pieces (Toolbox addon, Kodi sources) also run standalone on
any CoreELEC box -- see deploy_toolbox_addon.py / deploy_kodi_sources.py.

Usage (device on USB; with one device attached --serial auto-picks, else add
--serial <serial>):
  python install.py stage_unlock --yes        # unlock bootloader (wipes device)
  python install.py stage_magisk              # bundled image for the identified unit
  python install.py stage_magisk --magisk-img <path>   # your own patched init_boot
  python install.py stage1  --yes
  python install.py stage2
  python install.py stage2a
  python install.py stage3  --host <coreelec-ip>          # device booted in CoreELEC
  python install.py all     --yes             # stage_magisk+stage1, guides the rest
"""
import argparse, glob, hashlib, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "..", "artifacts")
MAGISK_DIR = os.path.abspath(os.path.join(HERE, "..", "magisk"))
PY = sys.executable
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import devices  # noqa: E402  -- stick/box discrimination registry


def run(script, *args):
    r = subprocess.run([PY, os.path.join(HERE, script), *args])
    return r.returncode


def adb(serial, *args, **kw):
    return subprocess.run(["adb", "-s", serial, *args], **kw)


def su(serial, cmd):
    r = subprocess.run(["adb", "-s", serial, "exec-out", "su -c '" + cmd.replace("'", "'\\''") + "'"],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


def getprop(serial, name):
    """One Android property, as a stripped string ('' if unset). No root needed."""
    return adb(serial, "shell", "getprop", name, capture_output=True, text=True).stdout.strip()


# ---- fastboot helpers (shared by stage_unlock and stage_magisk) ---------------
def fastboot_devices(fb):
    """Serials of the attached fastboot devices, [] if none. `fastboot devices` prints
    one '<serial>\tfastboot' line per device -- no header line (that's adb)."""
    try:
        r = subprocess.run(fb + ["devices"], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("fastboot not found on PATH -- install Android platform-tools")
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]


def wait_for_fastboot(fb, timeout=60):
    """Block until a fastboot device appears (after `adb reboot bootloader`), or exit.

    Two units in fastboot at once (typically one stranded there by an earlier run) is
    ambiguous, and the fastboot commands that follow are unqualified -- they would either
    fail or hit the wrong box. Guessing which unit to flash is not an option; the caller
    must say which with --fastboot-serial. Same rule adb_serial.resolve applies on the
    adb side.
    """
    bound = "-s" in fb                  # --fastboot-serial given -> every command is qualified
    print(f"  waiting for fastboot device (up to {timeout} s) ...")
    for _ in range(timeout):
        devs = fastboot_devices(fb)
        if devs:
            if len(devs) > 1 and not bound:
                sys.exit(f"{len(devs)} fastboot devices attached ({', '.join(devs)}) -- "
                         f"cannot tell which one to flash. Unplug the others, or name the "
                         f"target with --fastboot-serial <serial>.")
            print(f"  fastboot: {devs[0] if not bound else fb[fb.index('-s') + 1]}")
            return
        time.sleep(1)
    sys.exit(f"fastboot device did not appear within {timeout} s -- "
             "check USB cable and driver (Xiaomi bootloader driver / WinUSB)")


# ---- Magisk helpers (shared by stage_magisk and stage1b) ----------------------
def install_magisk_apk(serial, fatal):
    """Install the bundled Magisk manager APK over adb; True if it went in.

    `fatal` distinguishes the two callers: for stage1b the APK IS the stage, so a failure
    is fatal; for stage_magisk the fastboot flash is the stage, so a failed APK install is
    a warning and the root check at the end of that stage is where the human finds out.
    """
    apks = sorted(glob.glob(os.path.join(MAGISK_DIR, "Magisk*.apk")) +
                  glob.glob(os.path.join(ART, "Magisk*.apk")))
    if not apks:
        msg = "no Magisk*.apk found in magisk/ or artifacts/"
        if fatal:
            sys.exit(msg)
        print(f"  ({msg} -- skipping APK install)")
        return False
    if len(apks) > 1:
        # The pick is a plain lexicographic last, which is NOT a version sort
        # (Magisk-v9.apk would sort after Magisk-v30.apk). One APK is the expected case,
        # so say something rather than silently choosing.
        print(f"  WARNING: {len(apks)} Magisk APKs present; using "
              f"{os.path.basename(apks[-1])}. Keep exactly one.")
    apk = apks[-1]
    print(f"  installing {os.path.basename(apk)} ...")
    r = adb(serial, "install", "-r", apk, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0:
        if fatal:
            sys.exit(f"  APK install failed: {out}")
        print(f"  WARNING: APK install failed: {out}")
        return False
    print("  Magisk APK installed OK")
    return True


def check_boot_image(path):
    """Refuse anything that is not an Android boot image, before it reaches init_boot.

    fastboot writes whatever bytes it is handed. A zip, a truncated download or a stock
    firmware payload lands on a boot-critical partition and the unit stops booting. The
    header magic plus a plausible size is the cheapest gate there is on the one
    irreversible write this stage makes -- and the only one the --magisk-img path gets,
    since a hand-supplied image skips the bundled-image checks.
    """
    n = os.path.getsize(path)
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic != b"ANDROID!":
        sys.exit(f"{os.path.basename(path)} is not an Android boot image (header magic "
                 f"{magic!r}, expected b'ANDROID!') -- REFUSING to flash it to init_boot.")
    if not 64 * 1024 <= n <= 64 * devices.MIB:
        sys.exit(f"{os.path.basename(path)} is {n:,} B, which is not a plausible init_boot "
                 f"image -- REFUSING to flash it.")


def verify_bundled_sha256(path):
    """Verify `path` against the bundle's SHA256SUMS.txt, when it lists that file.

    make_dist writes SHA256SUMS.txt into every dist bundle, covering magisk/*.img. A bad
    unzip or a half-copied image is exactly the failure this catches, and 8 MiB of it is
    about to be written to a boot-critical partition. No-ops (does not fail) when there is
    no manifest or the file is not in it -- e.g. a source checkout, or a --magisk-img the
    user patched themselves. That is a real gap, not a hidden one: the manifest can only
    vouch for what shipped in the bundle.
    """
    sums = os.path.abspath(os.path.join(HERE, "..", "SHA256SUMS.txt"))
    if not os.path.exists(sums):
        return
    rel = os.path.relpath(os.path.abspath(path), os.path.dirname(sums)).replace(os.sep, "/")
    want = None
    with open(sums, encoding="utf-8", errors="replace") as f:
        for ln in f:
            p = ln.split()
            if len(p) == 2 and p[1].lstrip("*") == rel:   # sha256sum marks binary as '*path'
                want = p[0].lower()
                break
    if want is None:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != want:
        sys.exit(f"SHA-256 MISMATCH for {rel}\n"
                 f"  SHA256SUMS.txt says: {want}\n"
                 f"  the file on disk is: {got}\n"
                 f"The bundle is corrupt or the image was modified. REFUSING to flash it -- "
                 f"re-download / re-extract the bundle.")
    print(f"  sha256 OK ({rel})")


# ---- stage_unlock: unlock the bootloader (fastboot flashing unlock) ----------
def stage_unlock(a):
    """Unlock the bootloader so stage_magisk can flash a patched init_boot.

    Most units ship with a LOCKED bootloader that was never unlocked. fastboot
    refuses to flash on a locked bootloader, so stage_magisk fails. This stage
    reboots a locked unit to fastboot, re-checks the lock state there via
    `getvar unlocked`, and runs `flashing unlock` + `flashing unlock_critical`.

    DESTRUCTIVE: unlocking triggers a factory reset. After this stage the device
    reboots and must be re-setup from scratch (skip Google sign-in / re-enable
    ADB) BEFORE running stage_magisk.

    Everything that can be decided from Android is decided BEFORE the device is moved
    out of it: which unit this is, whether OEM unlocking is even permitted, whether the
    bootloader is already unlocked, and whether the user confirmed. A unit that needs
    nothing done is never rebooted at all.
    """
    print("== stage_unlock: unlock the bootloader (fastboot flashing unlock) ==")

    fb = ["fastboot"] + (["-s", a.fastboot_serial] if a.fastboot_serial else [])

    def fb_run(*args, **kw):
        try:
            return subprocess.run(fb + list(args), **kw)
        except FileNotFoundError:
            sys.exit("fastboot not found on PATH -- install Android platform-tools")

    def refuse():
        print()
        print("  ATTENTION: unlocking the bootloader ERASES ALL DATA (factory reset).")
        print("  Re-run with --yes to proceed:")
        print("    python install.py stage_unlock --yes")
        print("  Nothing was changed." + ("" if a.serial else
              " The device is in fastboot -- `fastboot reboot` returns it to Android."))
        return 1

    # ---- A. Android-side preflight (the device has not been touched yet) ---------
    # a.serial is None only when no adb device is ready. That is legal HERE and nowhere
    # else: a unit left in fastboot by an earlier run has no adb at all, and this stage
    # must still be able to finish the job. Everything in this section needs Android, so
    # it is skipped in that case and the bootloader (section D) is the only witness.
    if a.serial:
        gp = lambda p: getprop(a.serial, p)

        # Identify the unit BEFORE triggering a factory reset ON it. Pre-root, so the
        # eMMC-size cross-check is unavailable (SELinux) -> read_sectors=None, exactly as
        # stage_magisk does. Unlocking is geometry-independent -> require_layout=False.
        devices.identify(gp, None, require_layout=False, log=print)

        # The OEM-unlocking toggle (Developer options). The bootloader refuses
        # `flashing unlock` when it is off, so catch it here rather than 60 s and one
        # reboot later, with the unit stranded on the Mi-logo splash.
        if gp("sys.oem_unlock_allowed") == "0":
            sys.exit("OEM unlocking is OFF on this device -- the bootloader will refuse to "
                     "unlock.\nOn the device: Settings > System > Developer options > OEM "
                     "unlocking -> ON. Then re-run this stage.")

        # Lock state as Android sees it. ro.boot.* is handed to the kernel by the
        # bootloader itself, so an ALREADY-UNLOCKED unit can be answered without moving
        # it. Only these two exact readings are trusted; anything else falls through to
        # the bootloader's own answer in section D.
        locked, vbmeta = gp("ro.boot.flash.locked"), gp("ro.boot.vbmeta.device_state")
        if locked == "0" or vbmeta == "unlocked":
            print(f"  Bootloader is already unlocked (ro.boot.flash.locked={locked or '?'}, "
                  f"vbmeta.device_state={vbmeta or '?'}). Nothing to do.")
            print("  Continue with stage_magisk.")
            return 0
        if locked == "1":
            print("  Bootloader is LOCKED (ro.boot.flash.locked=1).")
        else:
            print(f"  Lock state unclear from Android (ro.boot.flash.locked={locked or '?'}) "
                  f"-- the bootloader is asked directly below.")

        # ---- B. Confirm the destructive unlock, while the device is still untouched --
        if not a.yes:
            return refuse()

    # ---- C. Get to fastboot ------------------------------------------------------
    # Serial first, deliberately: if adb sees the unit we just identified, THAT is the
    # unit to reboot -- never assume some already-present fastboot device is the same
    # one, or we would vet one box and unlock another. (If a --serial unit turns out to
    # be in fastboot already, the reboot is a no-op and the wait returns immediately.)
    if a.serial:
        print(f"  rebooting {a.serial!r} into bootloader ...")
        print("  A Mi-logo splash appears on the set-top box during fastboot.")
        adb(a.serial, "reboot", "bootloader")
        wait_for_fastboot(fb)
    elif fastboot_devices(fb):
        print("  no adb device, but a unit is already in fastboot -- picking up there.")
    else:
        sys.exit("no adb device and no fastboot device found. Plug the unit in over USB -- "
                 "either booted in Android with USB debugging on, or already in fastboot.")

    # ---- D. Ask the bootloader itself (authoritative) ----------------------------
    # `getvar unlocked` prints to stderr as `unlocked: yes|no`.
    r = fb_run("getvar", "unlocked", capture_output=True, text=True)
    var_out = (r.stdout or "") + (r.stderr or "")
    unlocked = None
    for line in var_out.splitlines():
        if "unlocked:" in line:
            val = line.split("unlocked:", 1)[1].strip().lower()
            unlocked = val.startswith("yes")
            break
    if unlocked is True:
        print("  Bootloader already unlocked. Nothing to do.")
        print("  Rebooting to Android; continue with stage_magisk.")
        fb_run("reboot")
        return 0
    if unlocked is None:
        print("  WARNING: could not read the lock state from `getvar unlocked`.")
        print(f"  raw output: {var_out.strip() or '(empty)'}")
        print("  Proceeding on the assumption the bootloader is LOCKED.")
    else:
        print("  Bootloader is LOCKED (unlocked: no).")

    # The --yes gate again, for the path that entered with the unit already in fastboot
    # (section A was skipped, so it has not been asked yet). Re-asking after a section-A
    # confirmation is impossible: that path sets a.yes.
    if not a.yes:
        return refuse()

    # ---- E. Unlock ---------------------------------------------------------------
    for cmd in (["flashing", "unlock"], ["flashing", "unlock_critical"]):
        print(f"  fastboot {' '.join(cmd)} ...")
        r = fb_run(*cmd)
        if r.returncode != 0:
            print(f"  WARNING: `fastboot {' '.join(cmd)}` returned {r.returncode}.")
            print("  If the device screen is asking to confirm, use the remote/volume+power")
            print("  keys on the set-top box to CONFIRM the unlock, then re-run this stage.")
            sys.exit(f"fastboot {' '.join(cmd)} FAILED")

    # ---- F. Verify + reboot ------------------------------------------------------
    r = fb_run("getvar", "unlocked", capture_output=True, text=True)
    var_out = (r.stdout or "") + (r.stderr or "")
    if "unlocked: yes" in var_out:
        print("  Bootloader unlocked (unlocked: yes).")
    else:
        print("  Unlock commands sent; could not re-confirm `unlocked: yes`.")
        print(f"  raw output: {var_out.strip() or '(empty)'}")

    print("  rebooting the device ...")
    fb_run("reboot")
    print()
    print("  Bootloader unlocked. The device factory-reset itself and will boot to")
    print("  Android first-time setup. RE-SETUP FROM SCRATCH:")
    print("    1. Complete Android setup (you can skip Google sign-in)")
    print("    2. Re-enable Developer options + USB debugging")
    print("    3. Re-authorize ADB, then re-plug the USB cable")
    print("  Then run stage_magisk:")
    print("    python install.py stage_magisk")
    return 0


# ---- stage_magisk: install Magisk APK + flash patched init_boot via fastboot --
def stage_magisk(a):
    print("== stage_magisk: install Magisk + flash patched init_boot ==")

    # ---- A. Install the Magisk manager APK --------------------------------------
    # Not fatal: the fastboot flash below is what actually roots the unit. (The app still
    # matters -- without a manager the su daemon has nothing to grant requests with -- so
    # a failure here shows up as an unconfirmed root in section F.)
    install_magisk_apk(a.serial, fatal=False)

    # ---- B. Identify the unit ---------------------------------------------------
    # ALWAYS, including when the user passes --magisk-img: WHICH IMAGE to flash is the
    # user's business, WHICH DEVICE IS ATTACHED is ours -- and the bootloader gate in
    # section E has nothing to compare against without `dev`.
    #
    # read_sectors=None: the eMMC size CANNOT be read here. This stage runs before root
    # exists (rooting is what it does), and SELinux denies the non-root shell domain that
    # sysfs read on both units -- so we don't pretend to check it. The BOOTLOADER
    # re-verifies this unit in section E, in the moment before the flash.
    # require_layout=False: rooting is geometry-independent (the box can be rooted before
    # its carve layout exists).
    gp = lambda p: getprop(a.serial, p)
    dev = devices.identify(gp, None, require_layout=False, log=print)
    have_fp = gp("ro.bootimage.build.fingerprint") or gp("ro.build.fingerprint")

    # ---- C. Pick the patched init_boot image ------------------------------------
    # A Magisk-patched init_boot is tied to the EXACT stock build it was patched from
    # (ro.bootimage.build.fingerprint, baked into its ramdisk). Flashing one onto a unit
    # running a different build can bootloop. How hard we enforce that depends on where
    # the image came from.
    if a.magisk_img:
        img = os.path.abspath(a.magisk_img)
        if not os.path.exists(img):
            sys.exit(f"--magisk-img not found: {img}")
        # Hand-supplied image -> the firmware match is ADVICE, not a gate: the user may
        # have patched against a build we cannot know about. Their image, their call.
        # (The device-identity gate in section E still applies -- that one is not theirs
        # to waive.)
        want = devices.boot_fingerprint_from_img(img)
        if want is None:
            print(f"  WARNING: no ro.bootimage.build.fingerprint inside "
                  f"{os.path.basename(img)} -- cannot check it against this unit's build.")
        elif want != have_fp:
            print(f"  WARNING: FIRMWARE MISMATCH -- flashing this image can bootloop:")
            print(f"    this {dev.name} runs:  {have_fp or '(unknown)'}")
            print(f"    image patched from:    {want}")
        else:
            print(f"  firmware match OK: {have_fp}")
    else:
        # Pick by the IDENTIFIED unit, NOT by codename: stick and box both report
        # device=twilight, so a codename-derived filename would hand the box the STICK's
        # rooted init_boot. The last two candidates are device-agnostic legacy names; the
        # firmware guard below is what stops them crossing the units over (the two
        # fingerprints differ -- .../adastra/... on the stick, .../twilight/... on the box).
        candidates = [os.path.join(MAGISK_DIR, dev.magisk_img),
                      os.path.join(ART, dev.magisk_img),
                      os.path.join(ART, "init_boot_patched.img"),
                      os.path.join(HERE, "..", "init_boot_patched.img")]
        img = next((os.path.abspath(c) for c in candidates if os.path.exists(c)), None)
        if img is None:
            print(f"  (no init_boot image for {dev.name} found: expected "
                  f"magisk/{dev.magisk_img})")
            return None      # `all` skips the stage; a direct run exits non-zero (main)
        # Bundled image -> the firmware match is a HARD gate. Nobody chose this file, the
        # registry did, so a mismatch is a bug or the wrong bundle, not an informed choice.
        want = devices.expected_boot_fingerprint(dev, img)
        if want is None:
            sys.exit(f"could not read the firmware fingerprint out of "
                     f"{os.path.basename(img)} -- refusing to flash it blindly. Patch your "
                     f"own init_boot for this unit and pass --magisk-img.")
        if want != have_fp:
            sys.exit(f"FIRMWARE MISMATCH -- refusing to flash {os.path.basename(img)}.\n"
                     f"  this {dev.name} runs:  {have_fp or '(unknown)'}\n"
                     f"  image patched from:    {want}\n"
                     f"Flashing a mismatched init_boot can bootloop. Update the unit to that "
                     f"build, or patch your own init_boot for THIS build and pass "
                     f"--magisk-img.")
        print(f"  firmware match OK: {have_fp}")

    check_boot_image(img)             # Android boot magic + plausible size
    verify_bundled_sha256(img)        # vs SHA256SUMS.txt, when the bundle ships one
    print(f"  image: {img}  ({os.path.getsize(img):,} B)")

    # ---- D. Which slot ----------------------------------------------------------
    # Patch the ACTIVE slot's init_boot so the *running* system gets the rooted ramdisk.
    # Never guess: the stick in hand runs slot _b, and flashing init_boot_a there would
    # root the INACTIVE slot -- the unit boots fine, unrooted, and the failure surfaces
    # stages later as "su root not available".
    slot = gp("ro.boot.slot_suffix")
    if slot not in ("_a", "_b"):
        sys.exit(f"could not read the active slot (ro.boot.slot_suffix="
                 f"{slot or '(empty)'}) -- refusing to guess which init_boot to flash. "
                 f"Let Android finish booting, re-plug USB, and re-run.")
    ib_part = f"init_boot{slot}"
    print(f"  active slot={slot} -> will flash {ib_part}")

    # ---- E. Reboot to fastboot, re-verify the unit, flash -----------------------
    fb = ["fastboot"] + (["-s", a.fastboot_serial] if a.fastboot_serial else [])

    print(f"  rebooting {a.serial!r} into bootloader ...")
    adb(a.serial, "reboot", "bootloader")
    wait_for_fastboot(fb)

    # Bootloader-side identity re-check (the hardware fact). Everything that picked `img`
    # was an Android property -- a string out of build.prop. The eMMC size, the one fact
    # that cannot be spoofed, is unreadable pre-root (SELinux). So ask the BOOTLOADER
    # instead: it reads the real GPT off the eMMC, with no Android in the path. userdata
    # is ~6x apart between the two units in either state (stock or already-carved), so a
    # wrong device cannot hide. Last gate before the first write.
    r = subprocess.run(fb + ["getvar", "partition-size:userdata"],
                       capture_output=True, text=True)
    ud = devices.parse_fastboot_size((r.stdout or "") + (r.stderr or ""))
    if ud is None:
        # Variable absent/unsupported. Absence of evidence is not evidence of a wrong
        # device -- do not block a legitimate install on an older bootloader.
        print("  WARNING: the bootloader did not report partition-size:userdata, so this "
              "unit could not be re-verified against the eMMC. Continuing on the "
              "Android-side identity alone.")
    elif not dev.userdata_size_ok(ud):
        expect = " or ".join(f"{n // devices.MIB:,} MiB"
                             for n in dev.expected_userdata_bytes())
        subprocess.run(fb + ["reboot"])
        sys.exit(f"DEVICE MISMATCH -- the bootloader reports a userdata partition of "
                 f"{ud // devices.MIB:,} MiB, which does not belong to the "
                 f"{dev.name} (expected {expect}).\n"
                 f"Android claimed this was a {dev.name}, the hardware disagrees, and "
                 f"flashing the wrong unit's init_boot can brick it. REFUSING TO FLASH "
                 f"-- nothing was written. Rebooting to Android.")
    else:
        state = ("stock" if ud == dev.stock_userdata_bytes else "carved")
        print(f"  bootloader check: partition-size:userdata = "
              f"{ud // devices.MIB:,} MiB -> {dev.slug} ({state}) -- CONFIRMED")

    print(f"  fastboot flash {ib_part} ...")
    r = subprocess.run(fb + ["flash", ib_part, img])
    if r.returncode != 0:
        # Deliberately NOT rebooting. init_boot may be half-written, and this stage cannot
        # pick a unit back up from fastboot (it needs adb to identify it), so hand the
        # human the one command that finishes the job from where the unit actually is.
        sys.exit(f"fastboot flash {ib_part} FAILED -- the unit is still in the bootloader "
                 f"and its {ib_part} may be half-written. Retry the flash from there:\n"
                 f"  fastboot flash {ib_part} {img}\n"
                 f"Only once that succeeds: `fastboot reboot`. (Booting on a partially "
                 f"written init_boot can leave the unit unable to start Android.)")
    print(f"  {ib_part} flashed OK")

    # ---- F. Reboot to Android and verify root -----------------------------------
    print("  rebooting to Android ...")
    subprocess.run(fb + ["reboot"])

    print(f"  waiting for ADB {a.serial!r} to reconnect (up to 90 s) ...")
    for _ in range(90):
        r = subprocess.run(["adb", "-s", a.serial, "get-state"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == "device":
            print("  ADB reconnected.")
            time.sleep(5)  # let the system settle before checking root
            root_out, _ = su(a.serial, "id")
            if "uid=0" in root_out:
                print("  Root verified: Magisk is active.")
            else:
                print("  Root not yet confirmed -- open the Magisk app to complete")
                print("  any first-time setup, then verify: adb shell su -c id")
            return 0
        time.sleep(1)
    print(f"  {ib_part} flashed successfully.")
    print("  ADB did not reconnect within 90 s.")
    print("  Re-plug the USB cable, then continue: python install.py stage1")
    sys.exit(0)


# ---- stage 1b: re-install Magisk APK after the stage1 factory reset ---------
def stage1b(a):
    print("== stage1b: re-install Magisk APK (factory reset wiped userdata) ==")
    print("  (the active slot's init_boot is still patched -- no fastboot needed)")

    install_magisk_apk(a.serial, fatal=True)
    print()
    print("  On the device:")
    print("    1. Open the Magisk app and complete any first-time setup")
    print("    2. Magisk -> Settings -> Default su permission -> Allow")
    print("  Then in a separate terminal verify root is working:")
    print("    adb shell su -c id   # should return uid=0(root)")
    print()
    input("  Press Enter here once uid=0 is confirmed ...")
    try:
        r = subprocess.run(["adb", "-s", a.serial, "exec-out", "su -c 'id'"],
                           capture_output=True, timeout=10)
        root_out = r.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        root_out = ""
    if "uid=0" in root_out:
        print("  Root confirmed.")
    else:
        print(f"  WARNING: could not confirm root ({root_out.strip() or 'no output'}).")
        print("  Check Magisk is set up, then continue -- stage2 will fail if root is missing.")
    print(f"\nstage1b done. Run stage2 now:")
    print("  python install.py stage2")
    return 0


# ---- stage 1: core destructive install ---------------------------------------
def stage1(a):
    print("== stage1: CORE install (destructive) ==")
    args = ["--serial", a.serial, "--default", a.default] + (["--yes"] if a.yes else ["--dry-run"])
    rc = run("flash_to_coreelec.py", *args)
    if rc == 0 and a.yes:
        print("\nstage1 done. The NEXT reboot enters recovery and reformats userdata")
        print("(factory-reset-like) to the new size, then boots Android. Reboot now,")
        print("let it finish the wipe + Android first-boot setup, re-enable ADB, then:")
        print("  adb reboot")
        print("  python install.py stage1b   # re-install Magisk APK")
        print("  python install.py stage2    # after root confirmed")
    return rc


# ---- ce_slot.conf: drop the slot file on /flash (belt-and-suspenders) ---------
def write_ce_slot_conf(serial):
    """Mount CE_FLASH, detect CE slot from the env gate, write /flash/ce_slot.conf.
    The hook (user-update.sh v2) reads the slot from the env partition directly, so
    this is a fallback -- but cheap and robust."""
    gate, _ = su(serial, "dd if=/dev/block/by-name/env bs=512 count=128 2>/dev/null "
                         "| tr '\\000' '\\n' | grep 'imgread kernel boot_' | head -1")
    slot = "a" if "boot_a" in gate else ("b" if "boot_b" in gate else "")
    if not slot:
        print("  ce_slot.conf: could not detect slot from env -- skipped")
        return
    out, rc = su(serial,
                 "mkdir -p /mnt/ceflash; mount -t vfat -o rw /dev/block/by-name/CE_FLASH /mnt/ceflash 2>/dev/null; "
                 "mount -o rw,remount /mnt/ceflash 2>/dev/null; "
                 f"printf 'CE_SLOT={slot}\\n' > /mnt/ceflash/ce_slot.conf && sync && "
                 "umount /mnt/ceflash 2>/dev/null; echo OK")
    print(f"  ce_slot.conf: CE_SLOT={slot} {'written' if 'OK' in out else 'FAILED: ' + out}")


# ---- stage 2: apps + universal OTA block -------------------------------------
def stage2(a):
    print("== stage2: apps + universal GMS OTA block (Google TV) ==")
    # The stage1 reboot runs a recovery factory-reset (to reformat userdata) which on
    # this SoC resets the u-boot env to stock -- dropping the boot gate AND the generic
    # boot helpers it needs. Re-apply the FULL gate now, post-reset, so it persists.
    # Idempotent: if the env still has the gate, reassert_env_gate just re-asserts it.
    print("-- (re)assert env boot gate (stage1's factory reset clears env) --")
    rc = run("reassert_env_gate.py", "--serial", a.serial, "--default", a.default)
    if rc != 0:
        sys.exit("env gate (re)assert failed -- CoreELEC would be unreachable; fix before continuing")
    apk = os.path.join(ART, "RebootToCoreELEC.apk")
    if not os.path.exists(apk):
        sys.exit("missing RebootToCoreELEC.apk")
    print("-- install RebootToCoreELEC app --")
    r = adb(a.serial, "install", "-r", apk, capture_output=True)
    print("  " + r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    print("-- /flash recovery files (ce_slot.conf, env_dualboot.bin, hook) [Xiaomi] --")
    run("deploy_flash_recovery.py", "--serial", a.serial, "--default", a.default)
    print("-- toolbox_export module: Android->CoreELEC BT/MAC export [generic] --")
    run("install_toolbox_export.py", "--serial", a.serial)
    print("-- blockgms: GMS system-update components [generic Google TV] --")
    rc = run("install_blockgms.py", "--serial", a.serial)
    if rc == 0:
        print("\nstage2 done. Reboot to activate the modules; then optionally stage2a (Xiaomi).")
        print("After rebooting into CoreELEC (first CE boot), run stage3:")
        print("  python install.py stage3 --host <coreelec-ip>")
    return rc


# ---- stage 2a: Xiaomi updater block (optional) -------------------------------
def stage2a(a):
    print("== stage2a: Xiaomi auto-update block (optional, Xiaomi only) ==")
    return run("install_blockota.py", "--serial", a.serial)


# ---- stage 3: CoreELEC-side setup (device in CoreELEC, SSH/--host) ------------
def stage3(a):
    print("== stage3: CoreELEC-side setup (device booted into CoreELEC, SSH) ==")
    if not a.host:
        sys.exit("stage3 needs --host <coreelec-ip> (boot into CoreELEC, enable SSH)")
    print("-- CoreELEC Toolbox addon: BT-sync / boot-default / WiFi-MAC [generic] --")
    rc = run("deploy_toolbox_addon.py", "--host", a.host)
    print("-- Kodi sources: PM4K + jamal2362 [generic] --")
    run("deploy_kodi_sources.py", "--host", a.host)
    if a.no_keymap:
        print("-- Xiaomi remote keymap: skipped (--no-keymap) --")
    else:
        tag = "forced (--xiaomi)" if a.xiaomi else "auto-detect"
        print(f"-- Xiaomi remote keymap [{tag}] --")
        km = ["--host", a.host] + ([] if a.xiaomi else ["--auto"])
        run("deploy_remote_keymap.py", *km)
    print("\nstage3 done. Install PM4K (script.plexmod) / TinyPPI (script.tinyppi) from the "
          "new sources:\n  Add-ons > Install from zip file > <source>.")
    return rc


# ---- verify: layout/env readiness (Android, read-only) -----------------------
def verify(a):
    print("== verify: layout/env readiness (read-only) ==")
    if not a.serial:
        sys.exit("verify needs a device attached over USB (in Android)")
    return run("validate_nondestructive.py", "--serial", a.serial)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["stage_unlock", "stage_magisk", "stage1", "stage1b",
                                      "stage2", "stage2a", "stage3", "verify", "all"])
    ap.add_argument("--serial", help="adb serial for the Android stages (USB device id); "
                    "omit to auto-pick the only attached device")
    ap.add_argument("--host", help="CoreELEC IP (stage3; device booted into CoreELEC)")
    ap.add_argument("--yes", action="store_true", help="perform destructive stage1 writes")
    ap.add_argument("--default", choices=["android", "coreelec"], default="android",
                    help="which OS a normal reboot boots (default android). 'coreelec' = "
                         "CoreELEC default + reboot-to-eMMC/nand -> Android.")
    ap.add_argument("--xiaomi", action="store_true",
                    help="stage3: force the Xiaomi remote keymap (skip auto-detect)")
    ap.add_argument("--no-keymap", dest="no_keymap", action="store_true",
                    help="stage3: skip the Xiaomi remote keymap (generic CoreELEC box)")
    ap.add_argument("--magisk-img", dest="magisk_img", default="",
                    help="stage_magisk: path to your own Magisk-patched init_boot image. "
                         "Omit to use the bundled image for the identified unit "
                         "(magisk/<per-device name>, see build/devices.py). A supplied "
                         "image only WARNS on a firmware mismatch instead of refusing.")
    ap.add_argument("--fastboot-serial", dest="fastboot_serial", default="",
                    help="stage_magisk: fastboot device serial (auto-detected if omitted)")
    a = ap.parse_args()

    if a.stage in {"stage_unlock", "stage_magisk", "stage1", "stage1b", "stage2", "stage2a", "verify", "all"}:
        import adb_serial
        # stage_unlock alone tolerates a missing adb device: a unit left in fastboot by an
        # earlier run has no adb, and that stage knows how to pick the job back up there.
        a.serial = adb_serial.resolve(a.serial, required=(a.stage != "stage_unlock"))

    if a.stage == "all":
        print("Running stage_magisk (if image found) + stage1.")
        print("After stage1 reboot into Android and re-run:")
        print("  python install.py stage2     (then stage2a optional)")
        print("Then boot CoreELEC and:  python install.py stage3 --host <coreelec-ip>")
        # None = stage_magisk found no image for this unit and flashed nothing. In `all`
        # that is survivable (an already-rooted unit still installs), so carry on to
        # stage1 -- which fails closed on a missing root ("su root not available").
        if stage_magisk(a) is None:
            print("  (stage_magisk skipped: no patched init_boot for this unit in magisk/)")
        sys.exit(stage1(a))

    rc = {"stage_unlock": stage_unlock, "stage_magisk": stage_magisk, "stage1": stage1,
          "stage1b": stage1b, "stage2": stage2, "stage2a": stage2a,
          "stage3": stage3, "verify": verify}[a.stage](a)
    if rc is None:
        # Only stage_magisk returns None, and only when it found no image. Run on its own
        # (rather than inside `all`), doing nothing is a failure, not a success -- exiting
        # 0 here would tell the user the unit was rooted when nothing was flashed.
        sys.exit("stage_magisk: no patched init_boot found for this unit -- NOTHING was "
                 "flashed and the device is unchanged. Put the image in magisk/ (see "
                 "magisk/README.md) or pass --magisk-img <path>.")
    sys.exit(rc)


if __name__ == "__main__":
    main()
