"""Phonevault -- a deduplicating, versioned backup engine for Android phones.

The engine is deliberately split so the same core runs in two places:

* on a PC, reading the phone through ``adb`` (see :mod:`phonevault.sources.adb`)
* on the phone itself inside Termux, reading the filesystem directly
  (see :mod:`phonevault.sources.local`)

Everything below :mod:`phonevault.repo` is source-agnostic: it sees a stream of
files and writes content-addressed blobs into a repository on the NAS.
"""

__version__ = "0.1.0"

REPO_VERSION = 1
