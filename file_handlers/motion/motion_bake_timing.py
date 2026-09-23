"""Sample source ranges onto the fixed 60 Hz Rise animation time axis."""
from dataclasses import replace
from fractions import Fraction
import math

from .errors import MotionWriteError
from .motion_root import ROOT_TRANSFORM_MODES


BAKE_FPS = 60


def sample_schedule(motions, segments, *, root_transform='relative'):
    if not segments or len(motions) != len(segments):
        raise MotionWriteError('At least one source segment is required')
    if root_transform not in ROOT_TRANSFORM_MODES:
        raise MotionWriteError(f'Unknown root transform mode: {root_transform}')
    samples, ranges = [], []
    previous = None
    for index, (motion, segment) in enumerate(zip(motions, segments, strict=True)):
        if segment.end is None:
            segment = replace(segment, end=motion.end_frame)
        if segment.root_transform is not None and segment.root_transform not in ROOT_TRANSFORM_MODES:
            raise MotionWriteError(f'Unknown segment root transform mode: {segment.root_transform}')
        factor = segment.root_translation_scale
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor < 0:
            raise MotionWriteError('Root translation scale must be a finite nonnegative number')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (segment.start, segment.end, segment.speed)):
            raise MotionWriteError('Segment frames and speed must be finite numbers')
        if not 0 <= segment.start <= segment.end <= motion.end_frame or segment.speed <= 0:
            raise MotionWriteError('Segment must satisfy 0 <= start <= end <= source end, with positive speed')
        start, end, speed = (Fraction(str(v)) for v in (segment.start, segment.end, segment.speed))
        step = speed * motion.frames_per_second / BAKE_FPS
        exact_duration = (end-start)/step
        duration = math.ceil(exact_duration)
        continuous = (previous is not None and previous.motion_id == segment.motion_id and previous.end == segment.start
                      and (previous.root_transform or root_transform) == (segment.root_transform or root_transform))
        output_start = len(samples)-int(continuous)
        output_end = output_start+duration
        if output_end > 0xFFFFFFFF:
            raise MotionWriteError('Baked duration exceeds the native frame range')
        samples.extend((index, float(min(start+i*step, end))) for i in range(int(continuous), duration+1))
        ranges.append({'motion_id': segment.motion_id, 'start': segment.start, 'end': segment.end,
                       'speed': segment.speed, 'output_start': output_start, 'output_end': output_end,
                       'join': 'continuous' if continuous else 'adjacent', 'source_step': float(step),
                       'duration_rounding_frames': float(duration-exact_duration),
                       'root_transform': segment.root_transform, 'root_translation_scale': factor})
        previous = segment
    return samples, ranges
