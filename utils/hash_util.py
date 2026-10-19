"""RE Engine hashing."""

from itertools import repeat

import mmh3

SEED = 0xFFFFFFFF


def murmur3_hash(data: bytes) -> int:
    return mmh3.mmh3_32_uintdigest(data, SEED)

def murmur3_hashes(items):
    return map(mmh3.mmh3_32_uintdigest, items, repeat(SEED))

def murmur3_hash_ascii(text: str) -> int:
    return murmur3_hash(text.encode('ascii', 'ignore'))

def murmur3_hash_utf16le(text: str) -> int:
    return murmur3_hash(text.encode('utf-16le'))