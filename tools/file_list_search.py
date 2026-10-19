import os
lazy from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from itertools import chain, combinations, compress, cycle, islice, repeat
from math import ceil, inf, prod
from time import perf_counter

lazy from file_handlers.pak.utils import filepath_hash
lazy from tools.file_list_generator import DIGITS, CombinedStems, ImproverMode, block_size, expand_block
lazy from utils.native_build import ensure_fast_path_search
lazy from utils.hash_util import murmur3_hashes
lazy import numpy as np
lazy import gc
lazy import random

BATCH_SIZE = 65_536
REPORT_INTERVAL = 0.1
QUEUED_CHARACTERS = 32 << 20
CPU_CAPACITY, CPU_THREAD_SHARE = 1 << 22, 1 << 16
SAMPLE_SECONDS = 0.4


@dataclass(frozen=True)
class NumericTask:
    parts: tuple
    sizes: tuple
    widths: tuple

    @cached_property
    def count(self):
        return prod(self.sizes)

    @cached_property
    def ascii(self):
        return all(isinstance(part, int) or part.isascii() for part in self.parts)

    def render(self, index):
        return self.render_range(index, 1)[0]

    def render_range(self, first, count):
        size, width = self.sizes[0], self.widths[0]
        paths, index, end = [], first, first + count
        while index < end:
            low, rest = index % size, index // size
            run = min(size - low, end - index)
            values = [None]
            for other_size, other_width in zip(self.sizes[1:], self.widths[1:]):
                values.append(str(rest % other_size).zfill(other_width))
                rest //= other_size
            pieces, piece = [], []
            for part in self.parts:
                if type(part) is not int:
                    piece.append(part)
                elif part:
                    piece.append(values[part])
                else:
                    pieces.append(''.join(piece))
                    piece = []
            pieces.append(''.join(piece))
            paths.extend(str(value).zfill(width).join(pieces) for value in range(low, low + run))
            index += run
        return paths


def numeric_tasks(collector, entries, maximum, depth, shallowest=1):
    """Independent numbers are tried besides linked groups, which are only guesses."""
    seen = set()
    for count in range(shallowest, depth + 1):
        for stem, extension in entries:
            groups = [matches for matches, _ in collector._get_numeric_replacement_groups(stem, extension)]
            groups.extend(matches for matches, _ in collector._get_linked_repeat_groups(stem, extension))
            groups.extend((match,) for match in collector._iter_numeric_matches(stem, extension))
            groups = list({tuple((match.start(), match.end()) for match in group): group
                           for group in groups}.values())
            for selected in combinations(groups, count):
                spans = sorted((match.start(), match.end(), slot)
                               for slot, group in enumerate(selected) for match in group)
                if any(start < previous_end for (_, previous_end, _), (start, _, _) in zip(spans, spans[1:])):
                    continue
                sizes = (maximum + 1,) * count
                widths = tuple(len(group[-1].group()) if group[-1].group().startswith('0') else 0
                               for group in selected)
                parts, previous = [], 0
                for start, end, slot in spans:
                    parts.extend((stem[previous:start], slot))
                    previous = end
                parts.append(stem[previous:])
                for suffix in collector._iter_improver_suffixes(extension):
                    tails = (collector._iter_combined_stems(parts[-1], extension, collector._suffix_extension(suffix))
                             if collector.improver_mode is ImproverMode.COMBINED else (parts[-1],))
                    for tail in tails:
                        task = NumericTask(tuple(parts[:-1] + [tail + '.' + suffix]),
                                           sizes + (1,) * (3 - count), widths + (0,) * (3 - count))
                        key = '\0'.join(map(str, (*task.parts, *task.sizes, *task.widths)))
                        if key not in seen:
                            seen.add(key)
                            yield task


def number_groups(collector, entries):
    """Groups can be sampled and scaled apart: ``numeric_tasks`` almost never makes the same template from two groups.
    """
    groups = {}
    for entry in entries:
        groups.setdefault(DIGITS.sub('#', collector._split_tex_stem_variant(entry[0])[0]), []).append(entry)
    return list(groups.values())


def revisits(tasks):
    first = BATCH_SIZE
    while tasks:
        tasks = [task for task in tasks if first < task.count]
        for task in tasks:
            yield task, first, min(BATCH_SIZE, task.count - first)
        first += BATCH_SIZE


def numeric_batches(tasks):
    large = []
    for task in tasks:
        yield task, 0, min(BATCH_SIZE, task.count)
        if task.count > BATCH_SIZE:
            large.append(task)
    yield from revisits(large)


def format_duration(seconds):
    seconds = max(0, round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds} s"


def sorted_keys(hashes):
    return np.sort(np.fromiter(hashes, dtype=np.uint64))


def block_candidate(source, k):
    if isinstance(source, NumericTask):
        return source.render(k)
    heads, tails, tails_outer = source
    if isinstance(heads, CombinedStems):
        return heads.candidate(tails, k)
    if tails_outer:
        return heads[k % len(heads)] + tails[k // len(heads)]
    return heads[k // len(tails)] + tails[k % len(tails)]


class CpuBlockMatcher:
    def __init__(self, hashes):
        self._match = ensure_fast_path_search().match_blocks
        self._keys = sorted_keys(hashes)
        top = (self._keys & np.uint64(0xFFFFFFFF)) >> np.uint64(6)
        self._bits = np.zeros(1 << 23, dtype=np.uint8)
        np.bitwise_or.at(self._bits, (top >> np.uint64(3)).astype(np.intp),
                         (np.uint64(1) << (top & np.uint64(7))).astype(np.uint8))
        self.threads = max(1, (os.process_cpu_count() or 2) - 1)
        self._pool = ThreadPoolExecutor(self.threads, thread_name_prefix='file-list-search')

    @staticmethod
    def _native(source, first, count):
        if isinstance(source, NumericTask):
            return 1, source.parts, source.sizes, source.widths, first, count
        heads, tails, tails_outer = source
        if isinstance(heads, CombinedStems):
            return 2, heads.stems, len(heads.ending), heads.endings, heads.textures, tails, first, count
        return 0, heads, tails, tails_outer, first, count

    def match_blocks(self, blocks):
        total = sum(count for _, _, count in blocks)
        if not total:
            return []
        share = max(CPU_THREAD_SHARE, ceil(total / self.threads))
        parts, part, room = [], [], share
        for source, first, count in blocks:
            while count > 0:
                taken = min(count, room)
                part.append((source, first, taken))
                first, count, room = first + taken, count - taken, room - taken
                if not room:
                    parts.append(part)
                    part, room = [], share
        if part:
            parts.append(part)

        def search(part):
            found = self._match([self._native(*block) for block in part], self._bits, self._keys)
            return [block_candidate(part[index][0], k) for index, k in found]
        return [*paths for paths in self._pool.map(search, parts)]

    def close(self):
        self._pool.shutdown(wait=True)


@dataclass(frozen=True)
class SearchEstimate:
    candidates: int
    seconds: float
    false_matches: float
    unfound: int


class Search:
    """Removes the hashes it finds from ``hashes``; ``unfound`` is how many of them the list does not name. ``prepare``
    it, then ``run`` it, which closes it. ``callback(message, current, total)`` returns whether to stop."""

    def __init__(self, collector, entries, hashes, unfound=None):
        self.collector, self.entries, self.hashes = collector, entries, hashes
        self.unfound = len(hashes) if unfound is None else unfound
        self.budget = collector.search_budget
        self.limit = inf if self.budget is None else self.budget
        self.advanced = collector.advanced_numeric and collector.uses_numeric_generation
        self.validated, self.generated = set(), 0
        self.stopped = self.limited = False
        self.gpu = self.matcher = None
        self.device = 'CPU'
        self.capacity = CPU_CAPACITY
        self._callback, self._closed, self._keeping = None, False, True
        self._started = self._reported = perf_counter()
        self._shown = 0.0
        self.weights, self.suffixes = [], {}
        self.standard_total = self.standard_done = self.entry_weight = self.entry_start = self.standard_yielded = 0
        self.advanced_total = self.advanced_done = 0
        self.sample_units = self.sample_tested = 0
        self.sample_seconds = 0.0
        self.advanced_plan = []
        self.standard_began = self.standard_ended = self.advanced_began = None
        self.pending, self.queued = [], [0, 0]
        self._lower_hashes = {key & 0xFFFFFFFF for key in hashes}
        self._murmur3_hashes = murmur3_hashes

    def _result(self):
        return dict(source_entries=len(self.entries), generated_candidates=self.generated,
                    validated_paths=len(self.validated), stopped=self.stopped, limited=self.limited,
                    backend=self.collector.compute_backend, device=self.device)

    def _remaining(self, now):
        standard_units = self.standard_done + min(self.entry_weight, self.standard_yielded - self.entry_start)
        standard_seconds = ((self.standard_ended or now) - self.standard_began) if self.standard_began else 0.0
        if self.standard_yielded and standard_seconds > REPORT_INTERVAL:
            standard_rate = self.standard_yielded / standard_seconds
            tested_share = self.standard_yielded / standard_units if standard_units else 1.0
        elif self.sample_seconds:
            standard_rate = self.sample_tested / self.sample_seconds
            tested_share = self.sample_tested / self.sample_units
        else:
            standard_rate, tested_share = None, 1.0
        left = self.limit - self.generated
        standard_left = min(max(0, self.standard_total - standard_units) * tested_share, left)
        if standard_left and not standard_rate:
            return None
        done = self.advanced_done
        advanced_left = max(0, min(self.advanced_total - done, left - standard_left))
        advanced = self._advanced_seconds(done + advanced_left) - self._advanced_seconds(done)
        advanced_seconds = now - self.advanced_began if self.advanced_began is not None else 0.0
        if done and advanced_seconds > REPORT_INTERVAL:
            advanced *= advanced_seconds / max(1e-9, self._advanced_seconds(done))
        return (standard_left / standard_rate if standard_left else 0.0) + advanced

    def _advanced_seconds(self, candidates):
        seconds = 0.0
        for count, time in self.advanced_plan:
            take = min(count, candidates)
            seconds += time * take / count
            candidates -= take
            if candidates <= 0:
                break
        return seconds

    def _progress(self, message, complete=False, force=False, prepared=None):
        """The PAK scan fills the bar to 5% before this."""
        now = perf_counter()
        if not self._callback or not (complete or force or now - self._reported >= REPORT_INTERVAL):
            return self.stopped
        self._reported = now
        tested = f"{self.generated:,} / {self.budget:,} budget" if self.budget is not None else f"{self.generated:,}"
        lines = [message, f"Candidates tested: {tested}", f"Valid paths: {len(self.validated):,}",
                 f"Speed: {self.generated / max(0.001, now - self._started):,.0f} candidates/s"]
        if prepared is not None:
            value = 500 + int(500 * prepared)
        elif complete:
            value = 10_000
        else:
            remaining = self._remaining(now) if self.standard_began is not None else None
            if remaining is not None:
                elapsed = now - self.standard_began
                self._shown = max(self._shown, elapsed / (elapsed + remaining) if elapsed + remaining else 0.0)
                lines.append(f"Time left: about {format_duration(remaining)}")
            value = 1000 + int(8900 * min(self._shown, 1.0))
        if self._callback('\n'.join(lines), value, 10_000):
            self.stopped = True
        return self.stopped

    def _accept(self, matches):
        if not self._keeping:
            return
        for path in matches:
            key = filepath_hash(path)
            if key in self.hashes:
                self.validated.add(path)
                self.hashes.discard(key)

    def _match(self, paths):
        lowers = self._murmur3_hashes(map(str.encode, paths, repeat('utf-16le')))
        self._accept(compress(paths, map(self._lower_hashes.__contains__, lowers)))

    def _queue(self, source, first, count, characters=0):
        if self.queued[0] + count > self.capacity or self.queued[1] + characters > QUEUED_CHARACTERS:
            self._flush()
        self.pending.append((source, first, count))
        self.queued[0] += count
        self.queued[1] += characters

    def _flush(self):
        if self.pending:
            self._accept(self.matcher.match_blocks(self.pending))
            self.pending.clear()
            self.queued[:] = 0, 0

    def _test_block(self, block, first, count):
        heads, tails, tails_outer = block
        text = ''.join(heads.stems if isinstance(heads, CombinedStems) else heads)
        if text.isascii() and ''.join(tails).isascii():
            for start in range(first, first + count, self.capacity):
                self._queue(block, start, min(self.capacity, first + count - start), len(text))
        else:
            for start in range(first, first + count, BATCH_SIZE):
                self._match(expand_block(heads, tails, tails_outer, start, min(BATCH_SIZE, first + count - start)))

    def _test_batch(self, task, first, count):
        if task.ascii:
            self._queue(task, first, count)
        else:
            self._match(task.render_range(first, count))

    def _blocks(self, collector=None, entries=None):
        collector = collector or self.collector
        sibling_paths = set()
        sibling_mode = collector.improver_mode is ImproverMode.TEX_MDF2_MESH_SWAPS
        for index in entries if entries is not None else range(len(self.entries)):
            weight = self.weights[index]
            if not weight:
                continue
            stem, extension = self.entries[index]
            self.entry_weight, self.entry_start = weight, self.standard_yielded
            for heads, tails, tails_outer in collector._iter_mode_blocks(stem, extension, self.suffixes):
                if sibling_mode:
                    heads = [path for path in heads if path not in sibling_paths]
                    sibling_paths.update(heads)
                if heads and tails:
                    self.standard_yielded += block_size(heads, tails)
                    yield heads, tails, tails_outer
            self.standard_done += weight
        self.entry_weight = 0

    def prepare(self, callback):
        collector = self.collector
        self._callback = callback
        try:
            if collector.compute_backend == 'gpu':
                if self._progress('Initializing GPU…', force=True, prepared=0.0):
                    return True, None, self._result(), set()
                from tools.file_list_gpu import CAPACITY, GpuPathValidator
                self.gpu = self.matcher = GpuPathValidator(self.hashes)
                self.capacity, self.device = CAPACITY, self.gpu.device
            else:
                self.matcher = CpuBlockMatcher(self.hashes)
            preparing = f"Preparing search on {self.device}…"
            if self._progress(preparing, force=True, prepared=0.0):
                return True, None, self._result(), set()
            processed = None
            collector._numeric_layout_cache.clear()
            collector._numeric_plans.clear()
            if collector.uses_numeric_generation:
                collector._linked_dual_tail_values = collector._build_linked_dual_tail_values(self.entries)
                collector._linked_repeat_values = collector._build_linked_repeat_values(self.entries)
                collector._numeric_structure_values = collector._build_numeric_structure_values(self.entries)
                collector._processed_numeric_replacement_families = set()
                collector._processed_power_of_two_families = set()
                collector._processed_linked_dual_tail_families = set()
                collector._processed_linked_repeat_families = set()
                processed = (set(), set(), set(), set())

            texture_bases = set()
            for index, (stem, extension) in enumerate(self.entries):
                if not index % 1024 and self._progress(preparing, prepared=0.8 * index / len(self.entries)):
                    return True, None, self._result(), set()
                if collector.improver_mode is ImproverMode.TEX_VARIANTS and extension == 'tex':
                    base = collector._split_tex_stem_variant(stem)[0]
                    if base in texture_bases:
                        self.weights.append(0)
                        continue
                    texture_bases.add(base)
                self.weights.append(collector._candidate_estimate(stem, extension, self.suffixes, processed))
            self.standard_total = sum(self.weights)
            if self._progress(f"Measuring speed on {self.device}…", force=True, prepared=0.8):
                return True, None, self._result(), set()
            self._measure()
            return None
        except Exception as error:
            self.close()
            return False, f"{collector.compute_backend.upper()} search failed: {error}. Try CPU mode or a smaller search.", None, set()

    def _measure(self):
        gc.collect()
        collector = self.collector
        sampler = collector._sampling_copy()
        processed = (set(), set(), set(), set()) if collector.uses_numeric_generation else None
        order = [index for index, weight in enumerate(self.weights) if weight]
        random.Random(0).shuffle(order)
        state = (self.standard_done, self.entry_weight, self.entry_start, self.standard_yielded)
        self._keeping = False
        try:
            if self.gpu is not None and order:
                warm, blocks = collector._sampling_copy(), []
                for index in order:
                    blocks.extend((block, 0, block_size(*block[:2])) for block in self._blocks(warm, (index,)))
                    if sum(count for _, _, count in blocks) >= self.capacity:
                        break
                self._warm_up(self._test_block(*block) for block in cycle(blocks))
            for index in order:
                stem, extension = self.entries[index]
                weight = sampler._candidate_estimate(stem, extension, self.suffixes, processed)
                began = perf_counter()
                for block in self._blocks(sampler, (index,)):
                    self._test_block(block, 0, block_size(*block[:2]))
                    self.sample_tested += block_size(*block[:2])
                self.sample_units += weight
                self.sample_seconds += perf_counter() - began
                if self.sample_seconds >= SAMPLE_SECONDS:
                    break
            began = perf_counter()
            self._flush()
            self.sample_seconds += perf_counter() - began
            if self.advanced:
                self._measure_templates(sampler, random.Random(0))
        finally:
            self._keeping = True
            self.standard_done, self.entry_weight, self.entry_start, self.standard_yielded = state

    def _measure_templates(self, sampler, rng):
        """Each depth is timed the second time (the run finds the caches warm)."""
        collector = self.collector
        maximum = collector.numeric_maximum
        groups = number_groups(collector, self.entries)
        rng.shuffle(groups)
        share = self.sample_tested / self.sample_units if self.sample_units else 1.0
        left = self.limit - min(self.standard_total * share, self.limit)

        def first_batches(group, depth):
            for task in numeric_tasks(sampler, group, maximum, depth, depth):
                self._test_batch(task, 0, min(BATCH_SIZE, task.count))
                yield task

        self._warm_up(chain.from_iterable(first_batches(group, 1) for group in reversed(groups)))
        large, rest = [], 0.0
        for depth in range(1, collector.numeric_depth + 1):
            if left <= 0 or not groups:
                break
            first, taken, seconds, kept = 0, 0, 0.0, []
            while taken < len(groups) and seconds < SAMPLE_SECONDS / 2:
                for task in first_batches(groups[taken], depth):
                    first += min(BATCH_SIZE, task.count)
                    if task.count > BATCH_SIZE:
                        kept.append(task)
                began = perf_counter()
                for _ in first_batches(groups[taken], depth):
                    pass
                seconds += perf_counter() - began
                taken += 1
            began = perf_counter()
            self._flush()
            seconds += perf_counter() - began
            scale = len(groups) / taken
            if first:
                self.advanced_plan.append((first * scale, seconds * scale))
                left -= first * scale
            rest += scale * sum(task.count - BATCH_SIZE for task in kept)
            large.extend(kept)
        if left > 0 and rest:
            rng.shuffle(large)
            tested, seconds = self._time_batches(list(islice(revisits(large), 4096)))
            self.advanced_plan.append((rest, rest * seconds / tested))
        self.advanced_total = sum(count for count, _ in self.advanced_plan)

    def _warm_up(self, steps):
        if self.gpu is None:
            return
        began = perf_counter()
        for _ in steps:
            if perf_counter() - began >= SAMPLE_SECONDS:
                break
        self._flush()

    def _time_batches(self, batches):
        """Timed over full dispatches as the run makes them, since a GPU dispatch always runs ``CAPACITY`` threads."""
        tested, began = 0, perf_counter()
        for task, first, count in cycle(batches):
            self._test_batch(task, first, count)
            tested += count
            elapsed = perf_counter() - began
            if elapsed >= SAMPLE_SECONDS / 4 and (tested >= 2 * self.capacity or elapsed >= SAMPLE_SECONDS):
                break
        self._flush()
        return tested, perf_counter() - began

    @property
    def estimate(self):
        tested_share = self.sample_tested / self.sample_units if self.sample_units else 1.0
        seconds_each = self.sample_seconds / self.sample_tested if self.sample_tested else 0.0
        standard = min(self.standard_total * tested_share, self.limit)
        advanced = max(0, min(self.advanced_total, self.limit - standard))
        candidates = standard + advanced
        return SearchEstimate(round(candidates), standard * seconds_each + self._advanced_seconds(advanced),
                              candidates * self.unfound / 2**64, self.unfound)

    def run(self, callback):
        collector = self.collector
        self._callback = callback
        self._started = self._reported = perf_counter()
        device, limit = self.device, self.limit
        advanced_batches = None
        truncated_batch = block_left = False
        try:
            block_iter = self._blocks()
            self.standard_began = perf_counter()
            for block in block_iter:
                if not self.hashes or self.stopped or self._progress(f"Validating candidates on {device}…"):
                    break
                total = block_size(*block[:2])
                count = min(total, limit - self.generated)
                self._test_block(block, 0, count)
                self.generated += count
                if count < total:
                    block_left = True
                    break
            self._flush()

            self.standard_ended = perf_counter()
            if self.advanced and self.hashes and not self.stopped and self.generated < limit:
                self.standard_done, self.entry_weight = self.standard_total, 0
                self.advanced_began = perf_counter()
                advanced_batches = iter(numeric_batches(numeric_tasks(
                    collector, self.entries, collector.numeric_maximum, collector.numeric_depth)))
                while self.hashes and not self.stopped and self.generated < limit:
                    if self._progress(f"Advanced numeric search on {device}…"):
                        break
                    batch = next(advanced_batches, None)
                    if batch is None:
                        break
                    task, first, count = batch
                    truncated_batch = count > limit - self.generated
                    count = min(count, limit - self.generated)
                    self._test_batch(task, first, count)
                    self.generated += count
                    self.advanced_done += count
                self._flush()

            if self.hashes and self.generated >= limit and not self.stopped:
                if advanced_batches is not None:
                    self.limited = truncated_batch or next(advanced_batches, None) is not None
                else:
                    self.limited = block_left or next(block_iter, None) is not None or (self.advanced and next(
                        numeric_tasks(collector, self.entries, collector.numeric_maximum, collector.numeric_depth),
                        None) is not None)
            stats = self._result()
            if not self.stopped:
                self._progress(f"Search {'budget reached' if self.limited else 'complete'} on {device}.",
                               complete=not self.limited, force=True)
                stats['stopped'] = self.stopped
            return True, None, stats, self.validated
        except Exception as error:
            return False, f"{collector.compute_backend.upper()} search failed: {error}. Try CPU mode or a smaller search.", None, set()
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.matcher is not None:
            self.matcher.close()
        collector = self.collector
        for name in ('_linked_dual_tail_values', '_linked_dual_tail_context_cache', '_linked_repeat_values',
                     '_numeric_structure_values', '_numeric_structure_context_cache', '_smart_number_values_cache',
                     '_numeric_matches_cache', '_numeric_layout_cache', '_numeric_plans', '_numeric_groups_cache',
                     '_linked_key_cache'):
            getattr(collector, name).clear()
        for name in ('_processed_numeric_replacement_families', '_processed_power_of_two_families',
                     '_processed_linked_dual_tail_families', '_processed_linked_repeat_families'):
            setattr(collector, name, None)


def run_search(collector, entries, hashes, progress_callback, unfound=None):
    search = Search(collector, entries, hashes, unfound)
    try:
        return search.prepare(progress_callback) or search.run(progress_callback)
    finally:
        search.close()
