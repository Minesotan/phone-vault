"""Content hashing.

BLAKE2b truncated to 256 bits: it is in the standard library, implemented in C,
and faster than SHA-256 on the 64-bit machines this runs on. Digests are
handled as lowercase hex strings everywhere above this module because they end
up as JSON keys and filenames.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

DIGEST_BYTES = 32
HEX_LEN = DIGEST_BYTES * 2


def new():
    return hashlib.blake2b(digest_size=DIGEST_BYTES)


def hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=DIGEST_BYTES).hexdigest()


def hash_stream(chunks: Iterable[bytes]) -> str:
    h = new()
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()


def is_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != HEX_LEN:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
