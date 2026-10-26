import struct
from itertools import accumulate, chain

lazy import numpy as np

lazy from file_handlers.mmtr.material_preview import RANGE_CBV, RANGE_SRV, RANGE_UAV
lazy from file_handlers.mmtr.resident import BUFFER, UNORDERED_ACCESS, prepare_runtime
lazy from file_handlers.sdf.hlsl import compile_hlsl_source
lazy from file_handlers.pak.utils import filepath_hash
lazy from tools.file_list_generator import CombinedStems
lazy from tools.file_list_search import block_candidate, NumericTask, sorted_keys

CAPACITY, _GROUP = 1 << 20, 128
SLOT = 0x80000000
_REGISTERS = (('targets', 8), ('strings', 4), ('items', 8), ('blocks', 16))
_SHADER = f"""
StructuredBuffer<uint2> targets : register(t0);
StructuredBuffer<uint> strings : register(t1);
StructuredBuffer<uint2> items : register(t2);
StructuredBuffer<uint4> blocks : register(t3);
RWStructuredBuffer<uint> found : register(u0);

cbuffer Search : register(b0)
{{
    uint count, target_count, block_count;
}};

uint rot(uint x, uint n) {{ return (x << n) | (x >> (32u - n)); }}
uint block(uint h, uint k)
{{
    k *= 0xcc9e2d51u; k = rot(k, 15u); k *= 0x1b873593u; h ^= k;
    return rot(h, 13u) * 5u + 0xe6546b64u;
}}
uint finish(uint h, uint tail, uint length)
{{
    if ((length & 3u) != 0u) {{ tail *= 0xcc9e2d51u; tail = rot(tail, 15u); tail *= 0x1b873593u; h ^= tail; }}
    h ^= length; h ^= h >> 16u; h *= 0x85ebca6bu; h ^= h >> 13u; h *= 0xc2b2ae35u; h ^= h >> 16u;
    return h;
}}

struct Hashing {{ uint lower, upper, lower_pending, upper_pending, length; }};
void append(uint c, inout Hashing h)
{{
    uint lower = c >= 65u && c <= 90u ? c + 32u : c;
    uint upper = c >= 97u && c <= 122u ? c - 32u : c;
    if ((h.length & 1u) == 0u) {{
        h.lower_pending = lower;
        h.upper_pending = upper;
    }} else {{
        h.lower = block(h.lower, h.lower_pending | (lower << 16u));
        h.upper = block(h.upper, h.upper_pending | (upper << 16u));
    }}
    h.length++;
}}
void append_string(uint2 item, inout Hashing h)
{{
    for (uint i = item.x; i < item.x + item.y; i++)
        append((strings[i >> 2u] >> ((i & 3u) * 8u)) & 0xffu, h);
}}
void append_number(uint value, uint width, inout Hashing h)
{{
    uint digits = 1u, divisor = 1u;
    for (uint v = value; v >= 10u; v /= 10u) {{ digits++; divisor *= 10u; }}
    for (uint pad = digits; pad < width; pad++)
        append(48u, h);
    for (uint d = 0u; d < digits; d++) {{
        append(48u + (value / divisor) % 10u, h);
        divisor /= 10u;
    }}
}}
uint2 block_hash(uint i)
{{
    uint low = 0u, high = block_count;
    while (low + 1u < high) {{
        uint mid = (low + high) / 2u;
        if (blocks[mid * 4u].x <= i)
            low = mid;
        else
            high = mid;
    }}
    uint4 head = blocks[low * 4u], refs = blocks[low * 4u + 1u], sizes = blocks[low * 4u + 2u];
    uint4 widths = blocks[low * 4u + 3u];
    uint k = sizes.w + i - head.x;
    Hashing h;
    h.lower = 0xffffffffu; h.upper = 0xffffffffu; h.lower_pending = 0u; h.upper_pending = 0u; h.length = 0u;
    if (head.z == 0u) {{
        uint first = head.w != 0u ? k % refs.y : k / refs.w;
        uint second = head.w != 0u ? k / refs.y : k % refs.w;
        append_string(items[refs.x + first], h);
        append_string(items[refs.z + second], h);
    }} else if (head.z == 2u) {{
        uint plain = widths.y, extra = sizes.y;
        uint stem_index = k / sizes.z, r = k % sizes.z;
        uint low = 0u, high = refs.w;
        while (low + 1u < high) {{
            uint mid = (low + high) / 2u;
            if (mid * plain + items[widths.x + mid].x * extra <= r)
                low = mid;
            else
                high = mid;
        }}
        uint head_index = r - (low * plain + items[widths.x + low].x * extra);
        uint2 stem = items[refs.x + stem_index];
        if (head_index == 0u) {{
            append_string(stem, h);
        }} else {{
            append_string(uint2(stem.x, stem.y - head.w), h);
            if (head_index >= plain)
                append_string(items[sizes.x + head_index - plain], h);
        }}
        append_string(items[refs.z + low], h);
    }} else {{
        uint3 values;
        uint carry = sizes.w + i - head.x;
        values.x = carry % sizes.x;
        carry = widths.w + carry / sizes.x;
        values.y = carry % sizes.y;
        values.z = head.w + carry / sizes.y;
        for (uint n = refs.x; n < refs.x + refs.y; n++) {{
            uint2 item = items[n];
            if ((item.y & {SLOT}u) != 0u)
                append_number(values[item.y & 3u], widths[item.y & 3u], h);
            else
                append_string(item, h);
        }}
    }}
    return uint2(finish(h.lower, h.lower_pending, h.length * 2u), finish(h.upper, h.upper_pending, h.length * 2u));
}}
bool less_key(uint2 a, uint2 b) {{ return a.y < b.y || (a.y == b.y && a.x < b.x); }}

[numthreads({_GROUP}, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint i = id.x;
    if (i >= count)
        return;
    uint2 key = block_hash(i);
    uint low = 0u, high = target_count;
    while (low < high) {{
        uint mid = low + (high - low) / 2u;
        if (less_key(targets[mid], key))
            low = mid + 1u;
        else
            high = mid;
    }}
    if (low < target_count && all(targets[low] == key))
        InterlockedOr(found[i >> 5u], 1u << (i & 31u));
}}
"""


class GpuUnavailable(RuntimeError):
    pass


class GpuPathValidator:
    def __init__(self, hashes):
        self._scene = None
        try:
            import fastmatpreview
            prepare_runtime()
            self._scene = fastmatpreview.Scene(True)
            self.device = self._scene.adapter()
            self._shader = bytes(compile_hlsl_source(_SHADER, entry_point="main", target="cs_5_0"))
            self._sized: dict[tuple[str, int], int] = {}
            self._held: dict[str, int] = {}
            self._steps: dict[tuple, int] = {}
            self._settings = self._scene.buffer(bytes(16), 0)
            self._found = self._scene.buffer(bytes(CAPACITY // 8), UNORDERED_ACCESS)
            probes = ['natives/stm/abc.mesh.10', 'natives/stm/ab_albd.tex.20', 'natives/stm/ab_nrmr.tex.20']
            self._targets(sorted_keys(map(filepath_hash, probes)))
            heads, tails = ['natives/stm/ab', 'natives/stm/abc'], ['.tex.20', '.mesh.10', '.MESH.1']
            texture = CombinedStems(['natives/stm/ab_albd'], '_albd', (True,))
            checks = [
                ([((heads, tails, False), 0, 6)], [probes[0]]),
                ([((heads, tails, True), 1, 5)], [probes[0]]),
                ([(NumericTask(('natives/stm/abc.mesh.', 0), (11, 1, 1), (0, 0, 0)), 0, 11)], [probes[0]]),
                ([((texture, ['.tex.20'], False), 0, len(texture.endings) + 2)], probes[1:]),
            ]
            if any(self.match_blocks(blocks) != expected for blocks, expected in checks):
                raise GpuUnavailable("GPU self-test failed; use CPU mode.")
            self._targets(sorted_keys(hashes))
        except Exception as error:
            self.close()
            raise GpuUnavailable(f"GPU initialization failed: {error}") from error

    def _targets(self, keys: np.ndarray) -> None:
        self._fill('targets', keys.view('<u4'))
        self.target_count = len(keys)

    def _fill(self, name: str, data) -> None:
        """Kept per size so a small upload never pads to an earlier large one."""
        if not isinstance(data, bytes):
            data = np.ascontiguousarray(data, '<u4').tobytes()
        size = max(16, 1 << (len(data) - 1).bit_length())
        resource = self._sized.get((name, size))
        if resource is None:
            resource = self._sized[name, size] = self._scene.buffer(bytes(size), 0)
        self._held[name] = resource
        self._scene.write(resource, data.ljust(size, b'\0'))

    def _dispatch(self, count: int, block_count: int) -> np.ndarray:
        if self._scene is None:
            raise GpuUnavailable("The GPU search was closed.")
        self._scene.write(self._settings, struct.pack('<3I4x', count, self.target_count, block_count))
        self._scene.write(self._found, bytes(CAPACITY // 8))
        bound = tuple(self._held[name] for name, _ in _REGISTERS)
        step = self._steps.get(bound)
        if step is None:
            step = self._steps[bound] = self._scene.dispatch({
                "compute_shader": self._shader, "heap": [], "groups": (CAPACITY // _GROUP, 1, 1),
                "tables": [(RANGE_SRV, 0, 0, len(_REGISTERS), BUFFER, 0, tuple(
                               (register, 0, stride, resource)
                               for register, ((_, stride), resource) in enumerate(zip(_REGISTERS, bound)))),
                           (RANGE_UAV, 0, 0, 1, BUFFER, 0, ((0, 0, 4, self._found),)),
                           (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._settings),))]})
        raw, = self._scene.run([step], [self._found])
        bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8, count=(count + 7) // 8), bitorder='little')
        return np.flatnonzero(bits[:count])

    def match_blocks(self, blocks):
        """A block is (source, first, count) over the source's candidates; a source is a (heads, tails, tails_outer)
        block (``expand_block``) or a ``NumericTask``. Candidates must be ASCII, at most ``CAPACITY`` in all."""
        texts, items, rows, used = [], [], [], []
        offset = start = 0
        placed = {}

        def place(strings):
            nonlocal offset
            key = id(strings)
            if key not in placed:
                lengths = list(map(len, strings))
                placed[key] = len(items) // 2, len(lengths)
                items.extend(chain.from_iterable(zip(accumulate(lengths, initial=offset), lengths)))
                texts.append(''.join(strings))
                offset += sum(lengths)
            return placed[key]

        for source, first, count in blocks:
            if count <= 0:
                continue
            if isinstance(source, NumericTask):
                if not (0 <= first and first + count <= source.count):
                    raise ValueError('Invalid GPU numeric task bounds')
                item_start = len(items) // 2
                for part in source.parts:
                    if isinstance(part, int):
                        items.extend((0, SLOT | part))
                    else:
                        items.extend((offset, len(part)))
                        texts.append(part)
                        offset += len(part)
                rest, values = first, []
                for size in source.sizes:
                    values.append(rest % size)
                    rest //= size
                rows.append((start, count, 1, values[2], item_start, len(source.parts), 0, 0, *source.sizes,
                             values[0], *source.widths, values[1]))
            else:
                heads, tails, tails_outer = source
                tails_at, tail_count = place(tails)
                if isinstance(heads, CombinedStems):
                    stems_at, stem_count = place(heads.stems)
                    endings_at, ending_count = place(heads.endings)
                    preceding = placed.get(id(heads.textures))
                    if preceding is None:
                        preceding = placed[id(heads.textures)] = len(items) // 2
                        before = list(accumulate(heads.textures, initial=0))[:-1]
                        items.extend(chain.from_iterable(zip(before, heads.textures)))
                    rows.append((start, count, 2, len(heads.ending), stems_at, stem_count, tails_at, tail_count,
                                 endings_at, ending_count, heads.per_stem, first, preceding, heads.plain, 0, 0))
                else:
                    heads_at, head_count = place(heads)
                    rows.append((start, count, 0, int(tails_outer), heads_at, head_count, tails_at, tail_count,
                                 1, 1, 1, first, 0, 0, 0, 0))
            used.append((source, first))
            start += count
        if not rows:
            return []
        if start > CAPACITY:
            raise ValueError(f'GPU block dispatches are limited to {CAPACITY:,} candidates')
        text = ''.join(texts)
        if not text.isascii():
            raise ValueError('GPU blocks hold ASCII strings only')
        self._fill('strings', text.encode('ascii'))
        self._fill('items', np.asarray(items, dtype=np.uint32))
        self._fill('blocks', np.asarray(rows, dtype=np.uint32))
        found = self._dispatch(start, len(rows))
        starts = np.asarray([row[0] for row in rows], dtype=np.int64)
        positions = (np.searchsorted(starts, found, side='right') - 1).tolist()
        return [block_candidate(used[position][0], used[position][1] + index - rows[position][0])
                for index, position in zip(found.tolist(), positions)]

    def close(self):
        self._scene = None
        self._steps = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
