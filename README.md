# Phonevault

Deduplicating, versioned backups of an Android phone to a NAS — Duplicati's
model, aimed at a phone.

It reads the phone either over `adb` from a PC, or from inside Termux on the
phone itself, splits files into content-addressed chunks, compresses them,
packs them into large files, and writes them to your NAS. Later runs only
read what actually changed. Snapshots are cheap, browsable, and restorable
file by file.

```
[ phone ] --adb--> [ PC: chunk / dedupe / compress ] --SMB--> [ NAS ]
[ phone: Termux agent ] --------------SMB------------------> [ NAS ]
```

---

## What it does

- **Deduplication.** Identical content is stored once, whether it is the same
  photo in two albums or an unchanged file across a hundred snapshots.
- **Real incrementals.** The second backup reads nothing it has already seen.
  On a 283 MiB folder of photos: 14 s first run, **1.8 s and 0 bytes read**
  for the second.
- **Compression.** zstd by default, chosen per chunk and skipped when it would
  not help — so app data and documents shrink to almost nothing while JPEGs
  are not pointlessly re-compressed.
- **Versioning and retention.** Keep last *N*, or hourly/daily/weekly/monthly/
  yearly. `forget` drops snapshots; `prune` reclaims the space, rewriting
  half-dead pack files.
- **Restore** to a folder on the PC, or straight back onto the phone. Every
  chunk is hash-verified on the way out.
- **Interruption-proof.** A dropped cable does not cost you the run: the
  backup waits for the phone to come back and carries on. If it cannot, a
  checkpoint means the next run resumes instead of re-reading everything.
- **Pause and resume**, from the web UI or with Ctrl+C.
- **Web UI** with live progress, a snapshot browser, and scheduling.
- **Root-aware.** With `su` it can back up `/data`; without it, `/sdcard`
  still works fine.

Verified against a real rooted Android 13 phone: hundreds of photos restored
**byte-for-byte identical**, checked against md5sums computed on the phone.

---

**Just want to run it?** See [docs/HOW-TO-USE.md](docs/HOW-TO-USE.md) — a
task-oriented guide with no internals. The rest of this file is reference
and design.

---

## Requirements

- **A PC running Windows** for the adb route. The engine itself is plain
  Python and runs anywhere, but `setup-adb.ps1` and `phonevault schedule` use
  PowerShell and Windows Task Scheduler.
- **Python 3.10 or newer.** No third-party packages are required; see the
  optional extras below.
- **An Android phone with USB debugging enabled.** Root (Magisk, KernelSU or
  similar) is needed for app data, `/data` and partition images; without it,
  `/sdcard` and SD cards still back up.
- **Somewhere to put the backups**: an SMB share on a NAS, a mapped drive, or
  any local folder.

Optional extras:

```powershell
pip install -e ".[smb]"    # talk to an SMB share directly (needed in Termux)
pip install -e ".[zstd]"   # zstd compression on Python older than 3.14
```

## Install

```powershell
git clone https://github.com/Minesotan/phone-vault.git
cd phone-vault
pip install -e .

# Installs adb if needed, then checks the phone end to end
.\tools\setup-adb.ps1
```

`setup-adb.ps1` verifies the device is authorised, that root works, and that
the device has the tools the fast paths use. Add `-Wireless` to switch the
phone to adb over Wi-Fi.

On the phone, enable **Developer options** → **USB debugging**, plug in, and
accept the prompt. Tick *Always allow from this computer* so scheduled runs
are not blocked.

---

## Quick start

```powershell
phonevault devices                                        # is the phone there?
phonevault init --repository \\NAS\backup\phone --save
phonevault backup --preset media --dry-run                # see what it would do
phonevault backup
phonevault snapshots
phonevault ui                                             # web interface
```

### Pointing it at your NAS

Any of these work as a repository:

| Form | Use when |
|---|---|
| `\\NAS\backup\phone` | The share is reachable as a UNC path (simplest on Windows) |
| `Z:\phone-backup` | You have mapped a drive letter |
| `smb://NAS/backup/phone` | No OS mount — needed in Termux, works on Windows too |

For `smb://`, set the username in the profile and keep the password out of
the config file:

```powershell
phonevault config set smb_username YOURUSER
$env:PHONEVAULT_SMB_PASSWORD = 'yourpassword'
```

Writing a password into the config is possible but requires
`--allow-plaintext-password`, because the file is readable by anything running
as you.

---

## What to back up

```powershell
phonevault backup --preset media    # DCIM, Pictures, Movies, Music
phonevault backup --preset sdcard   # all of /sdcard  (the default)
phonevault backup --preset everything   # everything but the SD card (needs root)
phonevault config set roots "/sdcard,/data/data"
```

Caches, thumbnails and `.tmp` files are skipped by default; `/proc`, `/sys`
and `/dev` are refused outright. Add your own with `--exclude` or
`phonevault config set excludes "*.log,**/Downloads/**"`.

`everything` names the parts of `/data` rather than `/data` itself, because
`/data/media` is the same tree as `/sdcard` and `/data/user` is the same tree
as `/data/data` — both bind mounts, so backing up `/data` wholesale walks a
quarter of a million paths twice. It also leaves out what the phone rebuilds
by itself: `dalvik-cache`, `apex`, `preloads`, `rollback`, and crash dumps.

> `/sdcard` is itself a symlink (to `/storage/self/primary`) on every modern
> Android. Phonevault follows a root symlink deliberately — a plain `find`
> would report the link, descend into nothing, and cheerfully back up zero
> files. Symlinks *inside* the tree are recorded rather than followed, so
> nothing is stored twice.

### Partition images

Naming the virtual root `/partitions` images the partitions that are unique to
one handset and cannot be rebuilt from a firmware download: the modem's
calibration and IMEI (`modemst1`, `modemst2`, `fsg`, `fsc`), sensor and
fingerprint calibration (`persist`), and on a Sony the trim area (`TA`). They
come to about 65 MB, are byte-identical between runs, and so deduplicate to
nothing after the first backup.

This is the one thing a file-level backup cannot otherwise cover. Everything
under `/data` can be copied back after a wipe; these live outside any
filesystem, and a flash that corrupts them leaves a phone with no cellular or
no working sensors.

Deliberately excluded are the partitions a firmware image restores byte for
byte (`boot`, `system`, `vendor`, `product`) and `metadata`, which only
describes an encrypted `/data` that a wipe has already destroyed.

Restoring one **onto a running phone is refused**. Partition images are
restored to a folder and flashed deliberately from fastboot; the wrong image
written to a block device bricks the handset.

### Storage volumes

`/sdcard` is **not** all of a phone. A device with an SD card keeps most of
its photos and video on a second volume under `/storage/<UUID>`, and backing
up only `/sdcard` there misses the bulk of the device while still reporting
success. Phonevault enumerates the volumes and refuses to be quiet about it:

```
warning: SD card (ABCD-1234) at /storage/ABCD-1234 holds 224.4 GiB that this
         backup will NOT include. Add it with: phonevault config set roots
         "/sdcard,/storage/ABCD-1234"
```

Naming any path inside a volume counts as knowing about it, so deliberately
backing up one folder does not produce a warning every run.

See them all with `phonevault devices`.

> **Restoring `/data` is device-specific.** App data belongs to a particular
> Android user and SELinux context. Phonevault will back it up and give it
> back to you, but dropping it onto a different phone is not something any
> tool can make reliably work.

---

## Everyday use

```powershell
phonevault backup                      # incremental
phonevault backup --tag before-update  # label a snapshot
phonevault snapshots                   # what you have
phonevault ls latest /sdcard/DCIM      # look inside one
phonevault restore latest D:\Recovered --include /sdcard/DCIM/Camera
phonevault dump latest /sdcard/Documents/notes.txt > notes.txt
phonevault check --read-data           # verify every stored byte
```

Restoring onto the phone:

```powershell
phonevault restore latest /sdcard/Restored --to-phone --include /sdcard/DCIM
```

### If the backup is interrupted

Nothing is lost and nothing needs cleaning up. Run `phonevault backup` again:

- Files already stored are recognised from a **checkpoint** and not re-read.
- If the phone merely went away mid-run, the backup waits for it and
  continues on its own.

To stop on purpose, press **Ctrl+C** once (or hit **Pause** in the web UI).
It finishes the file in flight, records a checkpoint, and exits cleanly.
Run it again to carry on.

### Keeping the repository from growing forever

```powershell
phonevault config set retention.daily 7
phonevault config set retention.weekly 4
phonevault config set retention.monthly 12

phonevault forget --dry-run     # what would go
phonevault forget --prune       # drop them and reclaim the space
phonevault backup --forget      # or fold it into every backup
```

`forget` removes snapshots. `prune` is what actually frees disk, and it is
the only command that deletes data — it takes an exclusive lock and refuses
to run if anything it needs is unreadable.

### Scheduling

```powershell
phonevault schedule install --at daily@02:30
phonevault schedule list
phonevault schedule remove
```

This registers a Windows Task Scheduler entry. The phone has to be reachable
when it fires — plugged in, or paired over Wi-Fi.

### No cable

```powershell
phonevault wifi                        # enable and connect, phone plugged in
phonevault config set device 192.168.1.50:5555
```

Wireless adb lasts until the phone reboots; re-run `phonevault wifi` after
one.

---

## Running on the phone instead

Copy the project to the phone and, in Termux:

```bash
bash tools/termux-setup.sh
```

The same engine runs there with the local filesystem as its source and
`smb://` as its destination, so the phone backs itself up whenever it is on
Wi-Fi, with no PC involved. The script covers storage permissions, root, cron
and wake locks.

---

## How it works

```
repository/
  config.json           chunking parameters and repository id
  packs/<xx>/<id>.pack  blob data, ~48 MB per file, with its own index appended
  index/<id>.idx        which blobs are in which pack, and where
  snapshots/<id>.json   one per run: metadata + the chunk list of its tree
  checkpoints/<key>.ckpt  resume state for an interrupted run
  locks/<id>.lock       advisory locks
```

**Chunks, not files.** Files are split into chunks, hashed with BLAKE2b-256,
and stored by content. A chunk already present is never stored twice.

**Packs, not loose chunks.** Tens of thousands of photos would become
hundreds of thousands of small files, and SMB pays a round trip for each one.
Blobs are appended into large packs and located by a side index, so writing
is sequential and reading one file is a single ranged read.

**Trees are chunked too.** A snapshot's file list goes through the same
chunker, so consecutive snapshots share nearly all of it and a new snapshot
costs kilobytes.

**The index is a cache.** It lives on your machine for speed and is rebuilt
from the repository whenever it is missing, so a repository is fully
self-describing and readable from any machine.

**Packs describe themselves.** Each pack ends with a footer listing what is
inside it -- the same listing that goes into `index/<id>.idx`. The duplication
looks wasteful until an index object is lost: a pack is otherwise an opaque
run of concatenated compressed blobs, with nothing marking where one ends and
the next begins, so one missing kilobyte-sized index file would strand 48 MB
of intact data. `phonevault repair` rebuilds the index object from the pack.

**Repair is deliberately timid.** It rebuilds what it can and reports the
rest; it removes nothing unless asked with `--discard-damaged`. A lost pack is
real data loss that no repair can undo, so the useful thing it can do is name
the affected snapshots and files -- the remedy is to run a backup, which
re-stores them from the phone.

### Chunking

| | `fixed` (default) | `cdc` |
|---|---|---|
| Speed | ~1.7 GiB/s | ~10× slower |
| Handles insertions | no | yes |
| Good for | photos, video, app data | large files edited in place |

Most of a phone is media that is written once, so `fixed` is the right
default. Choose at creation time — it cannot change later without destroying
deduplication against what is already stored:

```powershell
phonevault init --chunker cdc --chunk-size 1M -r \\NAS\backup\phone
```

### Compression

zstd when available, otherwise zlib, decided per chunk: if compressing does
not shrink a chunk by at least 5% it is stored raw, because most of a phone's
bytes are already-compressed media and squeezing them is pure CPU cost.

| Data | Saved |
|---|---|
| App databases, text, documents | ~97–100% |
| JPEG, MP4, encrypted files | 0% (stored raw) |

Every backup reports what it actually saved. Tune it at any time:

```powershell
phonevault repo show
phonevault repo set compression_level 10   # 1-19, default 3
```

Compression is recorded per blob, so changing it is safe and affects only new
data.

---

## Commands

| Command | What it does |
|---|---|
| `devices` | List attached phones, root status, free space |
| `wifi` | Set up adb over Wi-Fi |
| `init` | Create a repository |
| `backup` | Back the phone up |
| `snapshots` | List snapshots |
| `ls` | List files inside a snapshot |
| `restore` | Restore to a folder or back to the phone |
| `dump` | Write one file to stdout |
| `forget` | Apply a retention policy |
| `prune` | Reclaim space |
| `check` | Verify integrity (`--read-data` re-hashes everything) |
| `repair` | Reconcile the repository with the local index after a crash |
| `repo` | Show or change repository settings |
| `config` | Profiles and settings |
| `schedule` | Automatic backups |
| `unlock` | Show or break stale locks |
| `ui` | Web interface |

Profiles let one install handle several jobs:

```powershell
phonevault config add-profile tablet
phonevault --profile tablet config set repository \\NAS\backup\tablet
phonevault --profile tablet backup
```

---

## Web interface

```powershell
phonevault ui
```

Serves on `http://localhost:8723`. Live progress with a pause button,
snapshot browser with search, selective restore, retention and prune,
settings and scheduling.

It binds to localhost and checks the `Host` header, because the API can start
backups and write files. Exposing it on the LAN (`--host 0.0.0.0`) gives
anyone who can reach the port the same power — there is no authentication.

---

## Known limits

- **Change detection uses size and mtime.** A file rewritten within the same
  second, to the same length, with different content, is missed. Use
  `--rescan` when that matters.
- **No encryption at rest.** Data on the NAS is compressed, not encrypted.
  Do not use an untrusted destination.
- **Locks are advisory.** They prevent the dangerous overlap (a prune during
  a backup), not every conceivable race.
- **Windows filename rules.** Android names containing `:` `?` `*` and
  friends are written with `_` substituted on restore, and every substitution
  is reported.
- **Git Bash rewrites paths.** `/sdcard/DCIM` becomes `C:/Program
  Files/Git/sdcard/DCIM`. Phonevault detects and refuses this rather than
  backing up nothing; use PowerShell, or prefix with `MSYS_NO_PATHCONV=1`.

---

## License

MIT with attribution: use, modify and redistribute it however you like, but
any public copy, fork or derivative must credit **Rod Morabito** as the
original author and link back to
[github.com/Minesotan/phone-vault](https://github.com/Minesotan/phone-vault).
See [LICENSE](LICENSE) for the full text.

---

## Disclaimer

Phonevault reads a rooted phone with full privileges and can image raw
partitions. It never writes a partition, but rooting, flashing and restoring
`/data` are inherently risky and device-specific. Use it at your own risk,
test a restore before you rely on a backup, and see [LICENSE](LICENSE).

Phonevault is not affiliated with Google, any phone maker, or any NAS vendor.

---

## Development

```powershell
pip install -e ".[dev]"
python -m pytest            # no device needed
python -m ruff check .
```

The tests run against real repositories on temporary filesystems rather than
mocks — backup bugs live in the seams between chunking, packing, indexing and
tree building, and mocking those hides exactly the failures worth catching.
Disconnects and pauses are covered by a fault-injecting source.

Layout: `chunker` → `blobstore`/`index` → `repo` → `backup`/`restore`/`prune`,
with `sources/` (adb, local) and `backends/` (local, smb) at the edges.
Everything above the source layer is device-agnostic, which is what lets the
same engine run on a PC and on the phone.
