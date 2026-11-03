#!/data/data/com.termux/files/usr/bin/bash
#
# Install phonevault inside Termux so the phone backs itself up to the NAS,
# with no PC involved.
#
# Run it on the phone:
#     pkg install -y curl
#     bash termux-setup.sh
#
# It is safe to re-run; everything it does is idempotent.

set -euo pipefail

say()  { printf '\n\033[36m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32m[ok]\033[0m   %s\n' "$1"; }
warn() { printf '   \033[33m[warn]\033[0m %s\n' "$1"; }
bad()  { printf '   \033[31m[fail]\033[0m %s\n' "$1"; }

if [ ! -d /data/data/com.termux ]; then
  bad "This script is meant to run inside Termux on the phone."
  echo "   To back the phone up from a PC instead, use the adb source -- see the README."
  exit 1
fi

# -- packages --------------------------------------------------------------

say "Installing packages"
pkg update -y >/dev/null 2>&1 || warn "pkg update failed; carrying on"
pkg install -y python git termux-api >/dev/null
ok "python $(python --version 2>&1 | cut -d' ' -f2)"

# zstd is in the standard library from Python 3.14. On older builds the
# zstandard wheel needs a compiler, so it is optional -- phonevault falls
# back to zlib, which is always present.
if python -c "from compression import zstd" 2>/dev/null; then
  ok "zstd available (standard library)"
else
  warn "no stdlib zstd; trying the zstandard package (needs a compiler)"
  pkg install -y build-essential >/dev/null 2>&1 || true
  pip install --quiet zstandard 2>/dev/null && ok "zstandard installed" \
    || warn "falling back to zlib compression, which is fine, just slower"
fi

# -- storage permission ----------------------------------------------------

say "Storage access"
if [ ! -d "$HOME/storage" ]; then
  echo "   Termux needs permission to read shared storage."
  echo "   Accept the Android prompt that is about to appear."
  termux-setup-storage
  sleep 3
fi
if [ -d /sdcard ] && ls /sdcard >/dev/null 2>&1; then
  ok "/sdcard is readable"
else
  bad "/sdcard is not readable. Grant Termux storage permission and re-run."
  exit 1
fi

# -- root ------------------------------------------------------------------

say "Root"
if command -v su >/dev/null 2>&1 && su -c id 2>/dev/null | grep -q 'uid=0'; then
  ok "root works -- /data can be backed up"
  echo "   Grant Termux permanent root in Magisk so unattended runs do not stall on a prompt."
else
  warn "no root; /sdcard still backs up fine, /data will not be readable"
fi

# -- phonevault ------------------------------------------------------------

say "Installing phonevault"
TARGET="$HOME/phonevault"
if [ -d "$TARGET/.git" ]; then
  git -C "$TARGET" pull --ff-only >/dev/null 2>&1 && ok "updated the existing checkout" || warn "could not update; using what is there"
elif [ -f "$(dirname "$0")/../pyproject.toml" ]; then
  # Running from inside a copy of the source tree.
  TARGET="$(cd "$(dirname "$0")/.." && pwd)"
  ok "using the source tree at $TARGET"
else
  bad "No source found. Copy the project folder onto the phone, or clone it:"
  echo "     git clone <your-repo-url> $TARGET"
  exit 1
fi

pip install --quiet -e "$TARGET[smb]" 2>/dev/null || pip install --quiet -e "$TARGET"
ok "phonevault installed"
phonevault --version

# -- configuration ---------------------------------------------------------

say "Configure"
cat <<'EOF'
   Point it at the NAS. Either mount the share and use a path, or talk SMB
   directly, which needs no root:

     phonevault config set source local
     phonevault config set roots /sdcard
     phonevault config set repository 'smb://NAS/backup/phone'
     phonevault config set smb_username YOURUSER
     export PHONEVAULT_SMB_PASSWORD='yourpassword'

   Then:

     phonevault init --repository 'smb://NAS/backup/phone'
     phonevault backup --dry-run
     phonevault backup

   Keep the password out of the config file: put the export line in
   ~/.bashrc, or in the cron entry below.
EOF

# -- scheduling ------------------------------------------------------------

say "Running it automatically"
cat <<'EOF'
   Termux has no systemd. Two options:

   1. Termux:Boot + cron (survives reboots)
        pkg install -y cronie termux-services
        sv-enable crond
        crontab -e
      then add, to run at 03:00 while charging:
        0 3 * * *  PHONEVAULT_SMB_PASSWORD='...' phonevault -q backup --forget

   2. Termux:Tasker, if you already use Tasker, so the backup can be
      triggered by "on Wi-Fi and charging" rather than by the clock.

   Hold a wake lock for long runs, or Android will suspend Termux mid-backup:
        termux-wake-lock
        phonevault -q backup --forget
        termux-wake-unlock

   If a backup is cut short, just run it again -- it picks up from a
   checkpoint rather than re-reading everything.
EOF

say "Done"
