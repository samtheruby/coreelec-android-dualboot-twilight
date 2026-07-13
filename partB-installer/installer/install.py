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
             with --yes: pulls SHA-256-verified backups to pulled_backups/<device>/
             BEFORE the first write, then installs
    --- reboot: recovery reformats userdata, boots Android, then ---
    stage1b re-install the Magisk APK        [Xiaomi]   INTERACTIVE
             the reformat wiped /data, taking the Magisk app (and its su database)
             with it -- init_boot stays patched, so no fastboot. Refuses to run
             unless this unit really is in the post-stage1 state.
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
  python install.py stage1b                   # after the reboot: Magisk app back on
  python install.py stage2
  python install.py stage2a
  python install.py stage3  --host <coreelec-ip>          # device booted in CoreELEC
  python install.py all     --yes             # stage_magisk+stage1, guides the rest
"""
import argparse, glob, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "..", "artifacts")
MAGISK_DIR = os.path.abspath(os.path.join(HERE, "..", "magisk"))
PY = sys.executable
sys.path.insert(0, os.path.join(HERE, "..", "build"))
import devices  # noqa: E402  -- stick/box discrimination registry
import bundle   # noqa: E402  -- SHA256SUMS.txt check for anything we flash
import install_state  # noqa: E402  -- the boot default stage1 recorded, for stage2


def run(script, *args):
    # Flush our own buffered output BEFORE the child writes any of its own. Piped to a file,
    # Python block-buffers stdout while the subprocess inherits the fd and streams straight
    # through -- so in a captured log every "-- doing X --" header landed AFTER the output it
    # was introducing. Fine on a terminal, actively misleading in the log you debug from.
    sys.stdout.flush()
    r = subprocess.run([PY, os.path.join(HERE, script), *args])
    sys.stdout.flush()
    return r.returncode


def adb(serial, *args, **kw):
    return subprocess.run(["adb", "-s", serial, *args], **kw)


def su(serial, cmd, timeout=None):
    """Run `cmd` as root on the device -> (stdout, returncode).

    `timeout` is not optional in spirit: a Magisk `su` whose manager has not granted the
    shell yet does not fail, it BLOCKS -- waiting for a grant prompt on a TV that nobody is
    looking at. Every root check in this file is therefore bounded. A timeout comes back as
    rc=124 (the shell's own convention) with no output, which reads as "not root" to callers
    -- exactly what an unanswered prompt means.
    """
    try:
        r = subprocess.run(["adb", "-s", serial, "exec-out",
                            "su -c '" + cmd.replace("'", "'\\''") + "'"],
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", 124
    return r.stdout.decode("utf-8", "replace"), r.returncode


def sh(serial, cmd, timeout=20):
    """Run `cmd` in the plain (NON-root) device shell -> stdout text ('' on failure)."""
    try:
        r = adb(serial, "shell", cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout or ""


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
    """Install the bundled Magisk manager APK over adb.

    `fatal` distinguishes the two callers: for stage1b the APK IS the stage, so a failure
    is fatal; for stage_magisk the fastboot flash is the stage, so a failed APK install is
    a warning and the root check at the end of that stage is where the human finds out.
    """
    apks = sorted(glob.glob(os.path.join(MAGISK_DIR, "Magisk*.apk")))
    if not apks:
        msg = f"no Magisk*.apk found in {os.path.basename(MAGISK_DIR)}/"
        if fatal:
            sys.exit(msg)
        print(f"  ({msg} -- skipping APK install)")
        return
    if len(apks) > 1:
        # The pick is a plain lexicographic last, which is NOT a version sort
        # (Magisk-v9.apk would sort after Magisk-v30.apk). One APK is the expected case,
        # so say something rather than silently choosing.
        print(f"  WARNING: {len(apks)} Magisk APKs present; using "
              f"{os.path.basename(apks[-1])}. Keep exactly one.")
    apk = apks[-1]
    # This APK is what grants root: it carries the manager that answers every later su
    # request. Check it against the manifest like anything else we put on the device.
    bundle.verify(apk)
    print(f"  installing {os.path.basename(apk)} ...")
    r = adb(serial, "install", "-r", apk, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0:
        if fatal:
            sys.exit(f"  APK install failed: {out}")
        print(f"  WARNING: APK install failed: {out}")
        return
    print("  Magisk APK installed OK")


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
    bundle.verify(img)                # vs SHA256SUMS.txt, when the bundle ships one
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
            # Bounded: a freshly-flashed unit has no Magisk manager yet, so su may sit
            # waiting for a grant prompt on the TV rather than answering.
            root_out, _ = su(a.serial, "id", timeout=15)
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


# ---- post-stage1 state: did the reboot actually do its job? -------------------
def check_post_stage1_state(serial, dev):
    """Confirm stage1's reboot did what it promised, BEFORE stage1b changes anything.

    Every read here is deliberately NON-root, and that is the whole design. Root is exactly
    what this stage exists to restore, so a root-only check could only run AFTER the Magisk
    APK is installed -- by which point we would have installed an app onto a device whose
    still-pending reformat is about to erase it. Both facts below read fine from the plain
    shell (verified on the hardware):

      by-name/CE_*      the carved GPT is live AND the kernel has re-read it, which only
                        happens across a reboot. Missing -> either stage1 never ran here,
                        or it ran and the box has not been rebooted since.
      statfs(/data)     the size of the filesystem Android actually came up on.

    The second one is the one that matters. stage1 arms the reformat by writing
    'boot-recovery' into the BCB -- the same 32-byte field of `misc` that `adb reboot
    bootloader` overwrites with 'bootonce-bootloader'. That field is NOT self-clearing on
    this SoC (a stale 'bootonce-bootloader' is still sitting in it on the dev stick), so
    anything that detours through the bootloader between stage1 and its reboot silently
    disarms the reformat. Android then comes back up on the OLD, larger f2fs living on the
    new, SMALLER userdata: it mounts happily and corrupts itself the moment usage crosses
    the partition end. A healthy reformatted /data is always smaller than its partition, so
    a filesystem bigger than the partition it sits on IS that failure -- and it is the one
    state stage1b must refuse to build on top of.
    """
    print("-- post-stage1 state (read-only, no root needed) --")
    byname = sh(serial, "ls /dev/block/by-name/").split()
    if not {"CE_FLASH", "CE_STORAGE"} <= set(byname):
        sys.exit(
            "CE_FLASH / CE_STORAGE are not present on this device.\n"
            "The carved partition table is not live, which means one of:\n"
            "  * stage1 has not been run on this unit    -> run: python install.py stage1 --yes\n"
            "  * stage1 ran but the box has NOT REBOOTED -> reboot it (adb reboot), let\n"
            "    recovery reformat userdata and Android finish its first-boot setup, then\n"
            "    re-run stage1b.\n"
            "Refusing to install Magisk onto a device that is not in the post-stage1 state "
            "(a pending reformat would erase it again anyway).")
    print(f"  CE_FLASH + CE_STORAGE present -> carve is live (the reboot happened)")

    # statfs: total blocks x block size == the size of the fs Android is running on.
    parts = sh(serial, "stat -f -c '%b %S' /data").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        print("  WARNING: could not read the size of /data (stat -f gave "
              f"{' '.join(parts) or '(nothing)'}) -- cannot confirm the userdata reformat "
              "took. Continuing on the CE_* check alone.")
        return
    fs = int(parts[0]) * int(parts[1])
    cap = dev.carved_userdata_bytes
    if fs > cap:
        sys.exit(
            f"USERDATA WAS NEVER REFORMATTED -- this device is corrupting itself.\n"
            f"  /data filesystem: {fs:,} B\n"
            f"  userdata partition: {cap:,} B  ({dev.name}, carved)\n"
            f"The filesystem is LARGER than the partition holding it. stage1 shrank "
            f"userdata and armed a recovery reformat to resize the filesystem to match, "
            f"but that reformat did not run -- most likely the armed BCB was overwritten "
            f"by a reboot into the bootloader (`adb reboot bootloader` writes the same "
            f"field).\nAndroid will keep running on it and will hit I/O errors as soon as "
            f"usage crosses the partition end.\n\n"
            f"Re-arm the reformat and reboot (this WIPES /data again -- you will redo the "
            f"Android setup, then re-run stage1b):\n"
            f"    python installer/finish_install.py --serial {serial} --arm-reformat --yes\n"
            f"    adb -s {serial} reboot")
    print(f"  /data is {fs:,} B inside a {cap:,} B partition -> the reformat ran")


# ---- stage 1b: re-install Magisk APK after the stage1 factory reset ---------
def stage1b(a):
    print("== stage1b: re-install Magisk APK (factory reset wiped userdata) ==")
    print("  (the active slot's init_boot is still patched -- no fastboot needed)")

    # Identify the unit and confirm stage1's reboot landed BEFORE touching anything.
    # Pre-root, so no eMMC-size cross-check (read_sectors=None), exactly as stage_magisk
    # does -- and the carve checks that follow are a hardware fact of their own.
    dev = devices.identify(lambda p: getprop(a.serial, p), None,
                           require_layout=True, log=print)
    check_post_stage1_state(a.serial, dev)

    install_magisk_apk(a.serial, fatal=True)
    print()
    print("  On the device:")
    print("    1. Open the Magisk app and complete any first-time setup")
    print("    2. Magisk -> Settings -> Default su permission -> Allow")
    print("  Then in a separate terminal verify root is working:")
    print("    adb shell su -c id   # should return uid=0(root)")
    print()
    input("  Press Enter here once uid=0 is confirmed ...")
    root_out, _ = su(a.serial, "id", timeout=10)
    if "uid=0" in root_out:
        print("  Root confirmed.")
    else:
        print(f"  WARNING: could not confirm root ({root_out.strip() or 'no output'}).")
        print("  Check Magisk is set up, then continue -- stage2 will fail if root is missing.")
    print("\nstage1b done. Run stage2 now:")
    print("  python install.py stage2")
    return 0


# ---- stage 1: core destructive install ---------------------------------------
def stage1(a):
    print("== stage1: CORE install (destructive) ==")
    default = a.default or "android"
    args = ["--serial", a.serial, "--default", default] + (["--yes"] if a.yes else ["--dry-run"])
    rc = run("flash_to_coreelec.py", *args)
    if rc == 0 and a.yes:
        print("\nstage1 done. The NEXT reboot enters recovery and reformats userdata")
        print("(factory-reset-like) to the new size, then boots Android. Reboot now,")
        print("let it finish the wipe + Android first-boot setup, re-enable ADB, then:")
        print("  adb reboot")
        print("  python install.py stage1b   # re-install Magisk APK")
        print("  python install.py stage2    # after root confirmed")
        # stage1 recorded default='{default}', and stage2 reads it back, so the flag does
        # not have to be repeated. Say so rather than leave the user guessing -- the boot
        # direction silently flipping is exactly the bug this replaced.
        print(f"  (boot default '{default}' was recorded -- stage2 picks it up automatically)")
    return rc


# ---- stage 2: apps + universal OTA block -------------------------------------
def resolve_default(a, dev):
    """Which OS a normal reboot should boot: the flag, else what stage1 recorded for THIS
    unit, else android. The device cannot be asked -- stage1's factory reset wiped the env
    that held the answer -- so a stage2 that just assumed 'android' silently flipped a
    --default coreelec install back to Android."""
    if a.default:
        return a.default, "--default on the command line"
    saved = install_state.load(dev).get("default")
    if saved in ("android", "coreelec"):
        return saved, f"recorded by stage1 ({os.path.basename(install_state.path_for(dev))})"
    return "android", "the built-in default (stage1 recorded nothing for this unit)"


def stage2(a):
    print("== stage2: apps + universal GMS OTA block (Google TV) ==")

    # Precondition: this really is a post-stage1 unit, and it is rooted. stage2 writes the
    # boot gate and mounts CE_FLASH -- neither means anything on a device stage1 never
    # touched, and every sub-script below needs root.
    dev = devices.identify(lambda p: getprop(a.serial, p), None,
                           require_layout=True, log=print)
    check_post_stage1_state(a.serial, dev)
    if "uid=0" not in su(a.serial, "id", timeout=15)[0]:
        sys.exit("su root not available -- run stage1b first (it re-installs the Magisk app "
                 "that the userdata reformat erased).")
    print("  root: ok")

    default, why = resolve_default(a, dev)
    print(f"  boot default: {default}  ({why})")

    # Steps that the dual-boot cannot live without are fatal; the two feature modules at the
    # end are not (blockgms legitimately does not apply to a non-GMS box), so they warn and
    # let an otherwise-complete install finish. stage2 used to return install_blockgms's
    # exit code -- the LEAST critical step -- while ignoring the rc of everything above it.
    warned = []

    # The stage1 reboot runs a recovery factory-reset (to reformat userdata) which on this
    # SoC resets the u-boot env to stock -- dropping the boot gate AND the generic boot
    # helpers it needs. Re-apply the FULL gate now, post-reset, so it persists.
    # Idempotent: if the env still has the gate, reassert_env_gate just re-asserts it.
    print("\n-- (re)assert env boot gate (stage1's factory reset clears env) --")
    if run("reassert_env_gate.py", "--serial", a.serial, "--default", default) != 0:
        sys.exit("env gate (re)assert FAILED -- CoreELEC would be unreachable. Fix this "
                 "before continuing; nothing below it matters without the gate.")

    # The switcher app IS how a user enters CoreELEC on an android-default box.
    print("\n-- install RebootToCoreELEC app --")
    apk = os.path.join(ART, "RebootToCoreELEC.apk")
    if not os.path.exists(apk):
        sys.exit("missing RebootToCoreELEC.apk")
    bundle.verify(apk)
    r = adb(a.serial, "install", "-r", apk, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0 or "Success" not in out:
        # rc was never checked here, and the old code also indexed [-1] of an empty stdout
        # (adb reports failures on stderr) -- an IndexError instead of a diagnosis.
        sys.exit(f"RebootToCoreELEC install FAILED: {out or '(no output)'}\n"
                 f"Without it there is no way to enter CoreELEC on an android-default box.")
    print(f"  {out.splitlines()[-1]}")

    # Without these, a CoreELEC OS update rewrites bootcmd to stock and the dual-boot is
    # gone -- with nothing on /flash to put it back. Silent failure here surfaces months
    # later, as a box that stops booting CoreELEC after an update.
    print("\n-- /flash recovery files (ce_slot.conf, env_dualboot.bin, hook) [Xiaomi] --")
    if run("deploy_flash_recovery.py", "--serial", a.serial, "--default", default) != 0:
        sys.exit("/flash recovery deploy FAILED -- the dual-boot would not survive a "
                 "CoreELEC OS update. Fix this before continuing.")

    print("\n-- toolbox_export module: Android->CoreELEC BT/MAC export [generic] --")
    if run("install_toolbox_export.py", "--serial", a.serial) != 0:
        warned.append("toolbox_export (Bluetooth/MAC export to CoreELEC) was NOT installed")

    print("\n-- blockgms: GMS system-update components [generic Google TV] --")
    if run("install_blockgms.py", "--serial", a.serial) != 0:
        warned.append("blockgms (GMS system-update block) was NOT installed -- a Google TV "
                      "OTA could still overwrite the dual-boot")

    print("\nstage2 done. Reboot to activate the modules; then optionally stage2a (Xiaomi).")
    for w in warned:
        print(f"  WARNING: {w}")
    if warned:
        print("  (the dual-boot itself is complete -- these are optional protections)")
    print("After rebooting into CoreELEC (first CE boot), run stage3:")
    print("  python install.py stage3 --host <coreelec-ip>")
    return 0


# ---- stage 2a: Xiaomi updater block (optional) -------------------------------
def stage2a(a):
    print("== stage2a: Xiaomi auto-update block (optional, Xiaomi only) ==")
    # Same precondition as stage2: a Magisk module and a persistent `pm disable` mean
    # nothing on a device that is not a rooted, post-stage1 unit.
    dev = devices.identify(lambda p: getprop(a.serial, p), None,
                           require_layout=True, log=print)
    check_post_stage1_state(a.serial, dev)
    if "uid=0" not in su(a.serial, "id", timeout=15)[0]:
        sys.exit("su root not available -- run stage1b first.")
    # install_blockota verifies its OWN end state now (it exits non-zero if the updater is
    # not actually disabled), so its rc is worth propagating.
    return run("install_blockota.py", "--serial", a.serial)


# ---- stage 3: CoreELEC-side setup (device in CoreELEC, SSH/--host) ------------
def stage3(a):
    """Everything here is a convenience layer on top of a dual-boot that already works.

    Nothing in this stage is on the path back to Android: with --default android a normal
    reboot IS Android, and with --default coreelec you get there via CoreELEC's own
    'reboot to eMMC/nand'. Both are the env gate, written in stage1 and re-asserted in
    stage2. So no step here is worth aborting the stage for -- a failure costs the user a
    feature, not their box. Each one reports, and stage3 exits non-zero if any of them
    failed, rather than reporting the first one's result as the whole stage's.
    """
    print("== stage3: CoreELEC-side setup (device booted into CoreELEC, SSH) ==")
    if not a.host:
        sys.exit("stage3 needs --host <coreelec-ip> (boot into CoreELEC, enable SSH)")
    pw = ["--pass", a.ssh_pass] if a.ssh_pass else []
    failed = []

    print("-- CoreELEC Toolbox addon: BT-sync / boot-default / WiFi-MAC [generic] --")
    if run("deploy_toolbox_addon.py", "--host", a.host, *pw) != 0:
        failed.append("Toolbox addon (BT sync / boot-default / WiFi-MAC)")

    print("-- Kodi sources: PM4K + jamal2362 [generic] --")
    if run("deploy_kodi_sources.py", "--host", a.host, *pw) != 0:
        failed.append("Kodi sources (PM4K + jamal2362)")

    if a.no_keymap:
        print("-- Xiaomi remote keymap: skipped (--no-keymap) --")
    else:
        tag = "forced (--xiaomi)" if a.xiaomi else "auto-detect"
        print(f"-- Xiaomi remote keymap [{tag}] --")
        km = ["--host", a.host] + pw + ([] if a.xiaomi else ["--auto"])
        if run("deploy_remote_keymap.py", *km) != 0:
            failed.append("Xiaomi remote keymap")

    print("\nstage3 done. Install PM4K (script.plexmod) / TinyPPI (script.tinyppi) from the "
          "new sources:\n  Add-ons > Install from zip file > <source>.")
    for f in failed:
        print(f"  WARNING: {f} did NOT complete -- re-run stage3, or the script on its own.")
    if failed:
        print("  (the dual-boot itself is unaffected -- these are CoreELEC-side extras)")
    return 1 if failed else 0


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
    ap.add_argument("--ssh-pass", dest="ssh_pass", default="",
                    help="stage3: CoreELEC root SSH password (default: the scripts' own "
                         "'coreelec'). Pass this if you changed it -- without it stage3 "
                         "could not reach a box with a non-default password at all.")
    ap.add_argument("--yes", action="store_true", help="perform destructive stage1 writes")
    # default=None, NOT "android": stage2 has to tell "the user asked for android" apart
    # from "the user said nothing", because in the second case the right answer is whatever
    # stage1 recorded for this unit -- the device itself cannot remember (the factory reset
    # wipes the env). See resolve_default() / build/install_state.py.
    ap.add_argument("--default", choices=["android", "coreelec"], default=None,
                    help="which OS a normal reboot boots (default android). 'coreelec' = "
                         "CoreELEC default + reboot-to-eMMC/nand -> Android. stage1 records "
                         "this; stage2 reuses what stage1 recorded unless you pass it again.")
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
