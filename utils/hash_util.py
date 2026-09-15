"""RE Engine hashing."""

import mmh3


def murmur3_hash(data: bytes) -> int:
    return mmh3.hash(data, 0xFFFFFFFF, signed=False)

def murmur3_hash_ascii(text: str) -> int:
    return murmur3_hash(text.encode('ascii', 'ignore'))

def murmur3_hash_utf16le(text: str) -> int:
    return murmur3_hash(text.encode('utf-16le'))