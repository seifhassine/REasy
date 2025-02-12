# RE hashing
# Credits: https://github.com/TrikzMe/RE-Engine-Hash-tool/

import struct

def compute_namehash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h

def rotl32(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & 0xffffffff

def fmix(h: int) -> int:
    h ^= h >> 16
    h = (h * 0x85ebca6b) & 0xffffffff
    h ^= h >> 13
    h = (h * 0xc2b2ae35) & 0xffffffff
    h ^= h >> 16
    return h

def murmur3_hash(data: bytes) -> int:
    c1 = 0xcc9e2d51
    c2 = 0x1b873593
    seed = 0xffffffff
    h1 = seed
    stream_length = 0
    i = 0
    n = len(data)
    
    while i < n:
        chunk = data[i:i+4]
        i += len(chunk)
        stream_length += len(chunk)
        if len(chunk) == 4:
            k1 = (chunk[0] | (chunk[1] << 8) | (chunk[2] << 16) | (chunk[3] << 24))
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
            h1 = rotl32(h1, 13)
            h1 = (h1 * 5 + 0xe6546b64) & 0xffffffff
        elif len(chunk) == 3:
            k1 = (chunk[0] | (chunk[1] << 8) | (chunk[2] << 16))
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
        elif len(chunk) == 2:
            k1 = (chunk[0] | (chunk[1] << 8))
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
        elif len(chunk) == 1:
            k1 = chunk[0]
            k1 = (k1 * c1) & 0xffffffff
            k1 = rotl32(k1, 15)
            k1 = (k1 * c2) & 0xffffffff
            h1 ^= k1
    h1 ^= stream_length
    h1 = fmix(h1)
    return h1
