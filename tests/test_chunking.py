"""Chunking, hashing and compression."""

from __future__ import annotations

import os

import pytest

from phonevault import compress, hashing
from phonevault.chunker import Chunker


@pytest.mark.parametrize("algorithm", ["fixed", "cdc"])
def test_chunks_reassemble_exactly(algorithm):
    chunker = Chunker(algorithm, min_size=1024, avg_size=8192, max_size=32768)
    data = os.urandom(250_000)
    chunks = list(chunker.split_bytes(data))
    assert b"".join(c for _, c in chunks) == data
    # Offsets must describe where each chunk actually sits.
    position = 0
    for offset, chunk in chunks:
        assert offset == position
        position += len(chunk)


@pytest.mark.parametrize("algorithm", ["fixed", "cdc"])
def test_streaming_matches_buffered(algorithm):
    """Feeding odd-sized blocks must not change the boundaries."""
    chunker = Chunker(algorithm, min_size=1024, avg_size=8192, max_size=32768)
    data = os.urandom(200_000)
    blocks = [data[i : i + 777] for i in range(0, len(data), 777)]
    assert list(chunker.split_iter(blocks)) == list(chunker.split_bytes(data))


@pytest.mark.parametrize("algorithm", ["fixed", "cdc"])
def test_empty_and_tiny_inputs(algorithm):
    chunker = Chunker(algorithm, min_size=1024, avg_size=8192, max_size=32768)
    assert list(chunker.split_bytes(b"")) == []
    assert list(chunker.split_bytes(b"hi")) == [(0, b"hi")]
    assert list(chunker.split_iter([b"", b"abc", b""])) == [(0, b"abc")]


def test_cdc_survives_an_insertion():
    """The whole point of content-defined chunking."""
    chunker = Chunker("cdc", min_size=1024, avg_size=8192, max_size=32768)
    base = os.urandom(400_000)
    mutated = base[:5000] + b"INSERTED BYTES" + base[5000:]
    before = {c for _, c in chunker.split_bytes(base)}
    after = {c for _, c in chunker.split_bytes(mutated)}
    # Only the chunk around the edit should change.
    assert len(before & after) >= len(before) - 3


def test_fixed_chunking_dedupes_an_appended_file():
    """Growing files -- logs, sqlite -- must reuse their unchanged prefix."""
    chunker = Chunker("fixed", min_size=1024, avg_size=4096, max_size=8192)
    base = os.urandom(40_000)
    grown = base + os.urandom(4_000)
    before = {c for _, c in chunker.split_bytes(base)}
    after = {c for _, c in chunker.split_bytes(grown)}
    assert len(before & after) >= len(before) - 1


def test_boundaries_are_stable_across_instances():
    """Dedup depends on the gear table never drifting."""
    data = os.urandom(120_000)
    a = list(Chunker("cdc", min_size=512, avg_size=4096, max_size=16384).split_bytes(data))
    b = list(Chunker("cdc", min_size=512, avg_size=4096, max_size=16384).split_bytes(data))
    assert a == b


def test_chunker_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        Chunker("magic")
    with pytest.raises(ValueError):
        Chunker("fixed", min_size=100, avg_size=10, max_size=5)


def test_chunker_config_round_trips():
    chunker = Chunker("cdc", min_size=2048, avg_size=16384, max_size=65536)
    restored = Chunker.from_dict(chunker.to_dict())
    assert restored.to_dict() == chunker.to_dict()


# -- hashing ---------------------------------------------------------------


def test_hash_is_stable_and_stream_equivalent():
    data = b"phonevault" * 1000
    digest = hashing.hash_bytes(data)
    assert len(digest) == hashing.HEX_LEN
    assert hashing.hash_stream([data[:500], data[500:]]) == digest
    assert hashing.is_digest(digest)
    assert not hashing.is_digest("nope")
    assert not hashing.is_digest("z" * 64)


# -- compression -----------------------------------------------------------


@pytest.mark.parametrize("codec", [compress.CODEC_NONE, compress.CODEC_ZLIB, compress.CODEC_ZSTD])
def test_compression_round_trips(codec):
    if codec == compress.CODEC_ZSTD and not compress.zstd_available():
        pytest.skip("no zstd available")
    data = b"repeat me " * 5000
    used, payload = compress.compress(data, codec)
    assert compress.decompress(payload, used) == data


def test_incompressible_data_is_stored_raw():
    """Spending CPU on already-compressed media would be pure waste."""
    data = os.urandom(8192)
    used, payload = compress.compress(data, compress.default_codec())
    assert used == compress.CODEC_NONE
    assert payload == data


def test_unknown_codec_is_rejected():
    with pytest.raises(ValueError):
        compress.compress(b"x", 99)
    with pytest.raises(ValueError):
        compress.decompress(b"x", 99)


# -- tree parsing must stay linear ------------------------------------------


def _tree_bytes(count: int) -> bytes:
    from phonevault.snapshot import FileEntry, serialize_tree

    entries = [
        FileEntry(path=f"/sdcard/DCIM/IMG_{i:07d}.jpg", kind="f", size=1000 + i,
                  mtime=1.0 * i, chunks=[f"{i:064x}"])
        for i in range(count)
    ]
    return b"".join(serialize_tree(entries))


def test_parsing_a_large_tree_does_not_go_quadratic():
    """Guards the complexity of the parser, not the speed of the machine.

    Reassigning the read buffer per line re-copies everything still unparsed,
    so cost grows with the square of the tree. It stayed invisible until a
    real phone produced 286,000 entries, at which point opening the browser
    took 224 seconds while fetching the same 65 MB took 0.35.

    Measured on this input: the linear parser takes 0.31s, the quadratic one
    167.9s. The bound below sits two orders of magnitude above the former and
    an order of magnitude below the latter, so it is decisive either way
    without being sensitive to how fast the machine is.
    """
    import time

    from phonevault.snapshot import parse_tree

    blocks = [_tree_bytes(80_000)]
    start = time.perf_counter()
    assert sum(1 for _ in parse_tree(blocks)) == 80_000
    seconds = time.perf_counter() - start
    assert seconds < 20, (
        f"parsing 80,000 entries took {seconds:.1f}s; linear parsing is well "
        f"under a second, so this indicates quadratic buffer handling"
    )


def test_parsing_handles_records_split_across_blocks():
    """The tail-carrying version must still join a record cut in half."""
    from phonevault.snapshot import parse_tree

    whole = _tree_bytes(500)
    cut = len(whole) // 3
    split = [whole[:cut], whole[cut : cut * 2], whole[cut * 2 :]]
    assert sum(1 for _ in parse_tree(split)) == 500
    assert [e.path for e in parse_tree(split)] == [e.path for e in parse_tree([whole])]
