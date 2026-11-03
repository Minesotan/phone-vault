# Phonevault — how to use it

Everything you need to set up a backup of your phone, run it, check it, and
get files back — with no internals. For the design and reference material, see
the [README](../README.md).

---

## First-time setup

1. **Install** phonevault (see [Install](../README.md#install)) and run
   `.\tools\setup-adb.ps1`. It installs adb if needed and checks the phone
   end to end.
2. **On the phone**, enable *Developer options* → *USB debugging*, plug it in,
   and accept the prompt. Tick *Always allow from this computer* so scheduled
   runs are not blocked.
3. **Create a repository** — the folder your backups go into. A NAS share, a
   mapped drive or a local disk all work:
   ```powershell
   phonevault init --repository \\NAS\backup\phone --save
   ```
   If the share needs a login, either map it in Explorer first, or use an
   `smb://` URL and supply the password through the `PHONEVAULT_SMB_PASSWORD`
   environment variable (see the README).
4. **Choose what to back up.** The default is `/sdcard`. For the whole phone
   (needs root), see [What "the whole phone" means](#what-the-whole-phone-means):
   ```powershell
   phonevault backup --preset everything --dry-run
   ```
5. **Schedule it**, if you want it to run on its own:
   ```powershell
   phonevault schedule install --at daily@02:30
   ```

Your settings are kept in `%APPDATA%\phonevault\config.json`.

Run commands from **PowerShell**. If you use Git Bash, put
`MSYS_NO_PATHCONV=1` in front of any command with a phone path in it, or Git
Bash will silently rewrite `/sdcard` into a Windows path.

---

## The short version

```powershell
phonevault backup          # back up now
phonevault snapshots       # what has been backed up
phonevault ui              # open the web interface
```

That is 95% of normal use. A scheduled run does the same thing.

---

## Before a backup runs

The phone has to be reachable. Either:

- **Plug it in** by USB, with the screen unlocked, or
- **Pair it over Wi-Fi** once per reboot:
  ```powershell
  phonevault wifi
  ```

Check it is visible:

```powershell
phonevault devices
```

You want to see your phone listed with `device` and `root: yes`. If it says
`unauthorized`, unlock the phone and accept the *Allow USB debugging* prompt.

---

## Backing up

```powershell
phonevault backup                      # normal incremental run
phonevault backup --tag before-update  # label this one, so it is easy to find
phonevault backup --dry-run            # show what would happen, change nothing
```

The first backup of anything is slow because everything has to be read off
the phone. Every run after that only reads what changed, so it usually
finishes in seconds.

**Pausing.** Press `Ctrl+C` once. It finishes the file it is on, saves its
place, and exits. Run `phonevault backup` again whenever you like and it
carries on from where it stopped — it does not start over.

**If the cable falls out.** Nothing is lost. The backup waits for the phone
to come back and continues on its own. If it gives up, just run it again; it
resumes.

---

## Seeing what you have

```powershell
phonevault snapshots
```

```
ID            WHEN                     FILES        SIZE  TAGS / PATHS
fe859539f86e  2026-09-21 09:07:20       4049     7.6 GiB  first-full  /sdcard
4886d6701b87  2026-09-21 09:16:45       4049     7.6 GiB  nightly     /sdcard
```

Each line is one backup. The **ID** is how you refer to it; the first few
characters are enough.

Look inside one:

```powershell
phonevault ls latest /sdcard/DCIM
phonevault ls fe859539 /sdcard/Download
```

`latest` always means the most recent backup.

---

## Getting files back

### A whole backup, or a folder

```powershell
phonevault restore latest D:\Recovered
phonevault restore latest D:\Recovered --include /sdcard/DCIM/Camera
```

Files land under the destination, keeping their folder structure. Every file
is hash-checked on the way out, so a corrupted copy is reported rather than
silently written.

### A single file

```powershell
phonevault dump latest /sdcard/Documents/notes.txt > notes.txt
```

### Back onto the phone

```powershell
phonevault restore latest /sdcard/Restored --to-phone --include /sdcard/DCIM
```

It asks for confirmation first. The files go to the path you name, so
restoring into `/sdcard/Restored` never overwrites the originals. Point it at
the original path only if you actually mean to overwrite.

### An older version of a file

Restore from an older snapshot instead of `latest`:

```powershell
phonevault snapshots                                  # find the date you want
phonevault restore fe859539 D:\Recovered --include /sdcard/Documents
```

---

## The web interface

```powershell
phonevault ui
```

Opens `http://localhost:8723`. It gives you:

- **Overview** — run a backup, watch live progress, pause it
- **Snapshots** — every backup, with file counts and sizes
- **Browse & restore** — click through the phone's folders as they were at
  the time, tick what you want, restore it
- **Maintenance** — integrity checks, clearing out old backups
- **Settings** — what gets backed up, and the schedule

It only listens on this computer. Nobody else on the network can reach it.

---

## Keeping the NAS from filling up

Old backups are thinned automatically after each scheduled run. The default
policy keeps:

- every backup from the last **7 days**
- one per week for **4 weeks**
- one per month for **12 months**
- one per year for **3 years**

To change it:

```powershell
phonevault config set retention.daily 14
phonevault config set retention.monthly 24
```

To run the cleanup by hand:

```powershell
phonevault forget --dry-run     # show what would be dropped
phonevault forget --prune       # drop them and reclaim the space
```

`forget` removes backups from the list. `prune` is what actually frees space
on the NAS. Only `prune` deletes anything.

---

## Checking it is healthy

```powershell
phonevault check               # quick structural check
phonevault check --read-data   # re-reads and re-hashes everything (slow, thorough)
```

Worth running `--read-data` occasionally — maybe once a quarter. It is the
only way to catch a disk on the NAS quietly corrupting a file.

```powershell
phonevault repo show           # where it is, how big, how many backups
```

---

## When the backup and the NAS disagree

`check` tells you something is wrong. `repair` fixes what can be fixed.

```powershell
phonevault repair              # rebuild and report; never deletes anything
```

Run it after a crash or a yanked cable mid-backup, if you move to a different
PC, or any time `check` complains. It is safe to run whenever you are unsure —
on a healthy repository it does nothing but confirm that.

Three different things can go out of step, and they are not equally bad:

| What drifted | How bad | What repair does |
|---|---|---|
| The index on **this PC** | Harmless — it is only a cache | Rebuilds it from the NAS |
| An **index entry** on the NAS | Recoverable | Rebuilds it from the pack file itself |
| A **pack file** on the NAS | Real data loss | Names the snapshots and files affected |

That middle row is why every pack file carries a copy of its own contents
listing. Without it, losing one small index file would strand a whole 48 MB
pack of perfectly good photos.

For the last row, repair cannot invent the bytes back. What it gives you is
the list of affected files — and **running a backup re-stores them from the
phone**, as long as the phone still has them. That is the fix.

```powershell
phonevault repair --list-damaged-files   # every affected file, not just a count
phonevault repair --discard-damaged      # clear out what cannot be recovered
```

`--discard-damaged` only removes things that are already useless: index
entries whose pack is gone, packs whose contents cannot be located, and
half-written leftovers. It brings nothing back — it just stops the same
complaints appearing every run. Run plain `repair` first to see what would go.

There is also a **Repair** panel on the Maintenance tab of the web interface.

---

## What "the whole phone" means

The `everything` preset backs up the paths below. Together they cover every
byte of your data that is not regenerated by the phone itself. Most of it
needs root.

| Path | What it is |
|---|---|
| `/sdcard` | Internal shared storage — photos, downloads, documents |
| `/data/data` | App private data |
| `/data/app` | Installed apps |
| `/data/user_de` | App data readable before first unlock |
| `/data/adb` | Magisk modules and root config |
| `/data/misc` | Wi-Fi, Bluetooth pairings, VPN profiles |
| `/data/system_ce` | Per-user system data |
| `/data/system` | Accounts, settings, installed-package records |
| `/data/vendor` | Vendor-specific device state |
| `/partitions` | Raw images of the partitions unique to your handset — see [below](#the-partitions-that-decide-whether-the-phone-survives) |

An **SD card** is not in the preset, because its path contains a per-card ID
(`/storage/XXXX-XXXX`). Phonevault warns you at the start of every backup if a
volume with data on it is not covered, and prints the exact command to add it.

Some of this looks like it is missing, and is not:

- **`/sdcard` *is* `/data/media/0`.** The same files under two names, verified
  by inode. Backing up both would be pure duplication.
- **`/data/data` *is* `/data/user/0`.** Same again.
- A **work profile or second user** lives under `/data/user/10` and
  `/storage/emulated/10`. If your phone has one, add those paths yourself.

Deliberately **not** backed up, because the phone rebuilds them and they would
add churn to every run:

`/data/dalvik-cache` (compiled app bytecode), `/data/apex` (OS modules),
`/data/preloads` (carrier files from firmware), `/data/rollback`,
`/data/tombstones` (crash dumps), and app caches inside the paths above.

Also not backed up: the read-only OS partitions (`/system`, `/vendor`,
`/product`). Those come from your phone maker's firmware, not from you.

> **One thing to know.** `/data/misc` and `/data/system` hold Wi-Fi passwords
> and account records, and this repository is **not encrypted**. Anyone who
> can read the share can read them. If that matters, drop those two paths:
> ```powershell
> phonevault config set roots "/sdcard,/data/data,/data/app,/data/user_de,/data/adb,/data/system_ce,/data/vendor,/partitions"
> ```

---

## Before you flash new firmware

A firmware flash wipes `/data`. This backup is what you rebuild from — but be
clear-eyed about what it can and cannot give back, because the gap is where
people lose things.

### Comes back cleanly

Photos, videos, music, downloads, documents — everything on `/sdcard` and the
SD card. This is the bulk of what you would miss, and it restores to a folder
or straight back onto the phone. Installed APKs in `/data/app` let you
reinstall the exact versions you were running.

### Comes back, but reinstall the app first

App data in `/data/data`. It is captured in the clear (the phone is unlocked
while the backup runs, so credential-encrypted storage is readable), but
dropping it onto a fresh install is not a simple copy: every app
gets a new UID at install time, SELinux labels have to match, and some apps
change their data format between Android versions. Reinstall the app, then
restore its folder — and expect a few apps to reject it anyway.

### Does not come back, no matter what any tool does

Anything sealed by the phone's hardware keystore. Those keys live in the
secure element and are destroyed by a wipe, so the encrypted blobs in the
backup become undecryptable — the data is there and is permanently unreadable.
In practice that means:

| What | Migrate it before the flash |
|---|---|
| Authenticator / 2FA codes | Export or transfer seeds in the app |
| Banking and payment apps | Expect to re-enrol from scratch |
| Signal | In-app **Transfer or restore account** |
| WhatsApp | In-app chat backup |
| Saved passkeys | Re-register after the upgrade |

Do these *first*. They are the only items on this page with no second chance.

### The partitions that decide whether the phone survives

`/partitions` images the parts of your phone that a firmware download cannot
replace, because they are unique to your handset:

| Partition | Size | Losing it means |
|---|---|---|
| `modemst1`, `modemst2`, `fsg`, `fsc` | 9.4 MB | No cellular — IMEI and radio calibration live here |
| `persist` | 32 MB | Sensors and fingerprint reader stop working |
| `TA`, `LTALabel` | 8 MB | Sony only: trim area — DRM, camera tuning, device config |
| `efs`, `nvram`, `nvdata`, … | varies | Samsung and MediaTek equivalents of the modem partitions |
| `devinfo`, `secdata` | small | Device identity and security state |

Only the ones your phone actually has are imaged. Typically well under 100 MB
in total, and identical between runs, so after the first backup they
deduplicate to nothing. **This is the single most valuable thing here if the
flash goes wrong**, and it is the part no file-level backup can capture.

> **`persist` will not hash the same twice, and that is normal.** It is a
> mounted, writable ext4 filesystem, so its contents move while the phone is
> running. Comparing a stored image against the live partition an hour later
> gives a different checksum — the partition changed, not the backup. Every
> other partition here is raw data and does hash identically.
>
> A raw image of a mounted filesystem is *crash-consistent*: the same thing
> you would get by pulling the power. ext4 journals recover from that, and
> the calibration data inside changes rarely, so the image remains usable.
> It simply cannot be verified by checksum the way the others can.

Get them out to a folder you can reach without the phone:

```powershell
phonevault restore latest D:\phone-partitions --include /partitions
```

Phonevault will **not** write these back onto a running phone — restoring a
partition image is a fastboot operation, and the wrong image bricks the
handset. It hands you the files; flashing them is a deliberate act.

### Saving the build you are on

Phone makers usually only offer the newest firmware, so the image of
the version you are running now is usually **already unavailable** by the time
you want to go back to it. Chasing it around mirrors means trusting a stranger
with the software that boots your phone.

You do not have to. The build is still on the phone, and you have root:

```powershell
phonevault backup /firmware --tag pre-upgrade
```

That images the slot you actually booted — `super` (system, vendor, product),
`boot`, `vendor_boot`, `dtbo`, `vbmeta`, `recovery`, `oem` and `modem`,
whichever of them your phone has. Often 10–20 GB raw, far less once stored,
since `super` is mostly empty space. Pull it back out with:

```powershell
phonevault restore latest D:\firmware-rollback --include /firmware
```

`/firmware` is deliberately **not** in any preset: the images are re-read in
full every time they are asked for, so having it in the nightly job would mean
gigabytes across the cable each night for data that only changes when you update
Android. Run it once, the night before you flash.

**Check the sizes afterwards.** These are fixed-size images, so any shortfall
is lost data rather than a file that changed:

```powershell
phonevault ls latest /firmware -l
```

Every image should match its partition exactly. A short read now fails the
backup rather than storing a truncated image, but a flaky connection can still
make the run fail — better that than a `super.img` missing twelve gigabytes
that you only discover when you need it.

It is not a signed vendor package and does not replace one. What it is: a copy of
exactly what you are running, taken from your own phone, that you did not have
to download from anybody.

### A checklist worth following

1. Migrate the hardware-key items above. They are unrecoverable.
2. `phonevault backup` — a fresh one, with the phone unlocked.
3. `phonevault check` — confirm the repository is healthy.
4. **Restore something real to a folder and open it.** A backup you have never
   restored from is a hypothesis.
5. Copy `/partitions` somewhere off the NAS — a USB stick will do.
6. Write down your exact current firmware version, so you can go back.
7. Keep your bootloader unlock code where you can find it after the wipe.

---

## Changing what gets backed up

```powershell
phonevault config show                                    # current settings
phonevault config set roots "/sdcard,/storage/XXXX-XXXX"  # what to include
phonevault config set excludes "*.log,**/Trash/**"        # what to skip
```

Caches, thumbnails and `.tmp` files are skipped automatically.

**If you add or remove an SD card**, check the paths still make sense.
Phonevault warns you at the start of a backup if a storage volume with real
data on it is not being covered — read those warnings; that is exactly the
mistake that makes a backup look fine while missing most of your phone.

---

## The schedule

```powershell
phonevault schedule list                      # when it next runs
phonevault schedule install --at daily@02:30  # change the time
phonevault schedule remove                    # turn it off
```

This is a normal Windows scheduled task, so you can also see it in Task
Scheduler under `Phonevault-<profile>` (`Phonevault-phone` for the default
profile).

**The phone must be reachable when the task fires** or the run does nothing. If you
usually charge it overnight on a cable, that is ideal.

---

## When something goes wrong

**"no device found"**
The phone is unplugged, locked, or USB debugging is off. Run
`phonevault devices` to see what it thinks is going on.

**"lost the device" part-way through, again and again**
Before blaming the cable, check whether Windows is putting the port to
sleep. A backup leaves adb idle while the PC hashes and compresses, and an
idle USB port is exactly what power management switches off — the phone then
vanishes mid-run and comes straight back, which reads like a hardware fault
and is not one.

Two settings, both worth turning off:

```powershell
# never suspend idle USB ports, on mains or battery
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setactive SCHEME_CURRENT
```

Then, in Device Manager (this part needs an administrator), under **Universal
Serial Bus controllers**, open the **USB Root Hub** your phone is plugged into
and untick *Allow the computer to turn off this device to save power*. Setting
it on the phone's own entry is not enough: suspending the hub above it takes
the phone with it.

Plugging into a rear motherboard port rather than a front panel or a hub
removes another link that can do the same thing.

**"the device is connected but not authorised"**
Unlock the phone and accept the *Allow USB debugging* prompt. Tick *Always
allow from this computer* so scheduled runs are not blocked by it.

**"root is not available"**
Your root manager (Magisk or similar) is waiting for you to approve the
request, or it timed out. Unlock the phone and re-run. Grant *shell* permanent
root so unattended runs do not stall. Without root, `/sdcard` and the SD card still back up fine —
only app data needs it.

**"repository is locked"**
A backup is already running, or one was interrupted. Wait for it, or:

```powershell
phonevault unlock            # show what holds the lock
phonevault unlock --force    # break it, if you are sure nothing is running
```

**The backup seems far too small**
Check the warnings at the start of the run. If a storage volume is not
covered, phonevault says so and tells you the exact command to add it.

**It is slow**
The first backup of new data is limited by USB, around 20 MB/s. Later runs
should take seconds. If every run is slow, something is defeating the change
detection — try `phonevault backup --dry-run` and look at how many files it
says are "new".

---

## Where things live

| | |
|---|---|
| Settings | `%APPDATA%\phonevault\config.json` |
| Local speed cache | `%LOCALAPPDATA%\phonevault\cache` (safe to delete; rebuilds) |
| Your data | The repository you passed to `phonevault init` (`phonevault repo show`) |

The NAS folder is self-describing: every backup can be read on any machine
with phonevault installed, using nothing but the folder itself. The local
cache only makes things fast — losing it costs time, never data.

---

## Two things worth knowing

**There is no encryption.** Your data on the NAS is compressed but not
encrypted. Anyone who can read the share can read your files. That is fine
for a NAS at home; it would not be for cloud storage.

**App data restores reliably only to the same phone.** `/data/data` is tied
to that device's Android user and security setup. Photos, videos and
documents restore anywhere; app data is realistically a same-phone recovery.
