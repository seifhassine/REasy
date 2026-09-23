"""Rotation-only waist alignment and bounded seam interpolation."""
from dataclasses import replace

from .errors import MotionWriteError
from .evaluation.composition import compose_evaluated_pose
from .evaluation.math3d import inverse_quaternion, multiply_quaternions
from .evaluation.sampling import interpolate_quaternion, RotationInterpolation


def align_waist_rotations(rotations, ranges):
    """Match each cut's initial rotation to the first cut, retaining its delta."""
    result = list(rotations)
    reference = rotations[0]
    correction = (0., 0., 0., 1.)
    for interval in ranges:
        start, end = interval['output_start'], interval['output_end']
        if interval['join'] != 'continuous':
            correction = multiply_quaternions(reference, inverse_quaternion(rotations[start]))
        if start == 0:
            continue
        for frame in range(start, end+1):
            result[frame] = multiply_quaternions(correction, rotations[frame])
    return result


def seam_windows(ranges, blend_frames):
    if isinstance(blend_frames, bool) or not isinstance(blend_frames, int) or blend_frames < 0:
        raise MotionWriteError('Blend frames must be a nonnegative integer')
    if blend_frames == 0:
        return []
    starts = [row['output_start'] for row in ranges if row['join'] != 'continuous']
    ends = [value-1 for value in starts[1:]] + [ranges[-1]['output_end']]
    windows = []
    for i, boundary in enumerate(starts[1:], 1):
        left = max(starts[i-1], boundary-blend_frames)
        right = min(ends[i], boundary+blend_frames-1)
        if windows and left < windows[-1]['end']:
            raise MotionWriteError('Blend windows overlap; reduce the blend frame count')
        windows.append({'seam': boundary, 'start': left, 'end': right})
    return windows


def blend_values(values, windows, *, rotation=False):
    output = list(values)
    for window in windows:
        start, end = window['start'], window['end']
        for frame in range(start+1, end):
            t = (frame-start)/(end-start)
            amount = t*t*(3-2*t)
            if rotation:
                output[frame] = interpolate_quaternion(values[start], values[end], amount,
                                                       RotationInterpolation.SHORTEST_SLERP)
            else:
                output[frame] = tuple(a+(b-a)*amount for a, b in zip(values[start], values[end], strict=True))
    return output


def blend_poses(poses, rig, ranges, *, align_waist=True, blend_frames=3):
    windows = seam_windows(ranges, blend_frames)
    waist = [i for i, joint in enumerate(rig.joints) if joint.name == 'Waist_00']
    if align_waist and len(waist) != 1:
        raise MotionWriteError('Waist alignment requires one bone named Waist_00')
    local = [list(pose.local_transforms) for pose in poses]
    for index, joint in enumerate(rig.joints):
        if joint.name.casefold() == 'root':
            continue
        rotations = [pose.local_transforms[index].rotation for pose in poses]
        if align_waist and index == waist[0]:
            rotations = align_waist_rotations(rotations, ranges)
        rotations = blend_values(rotations, windows, rotation=True)
        # Waist alignment/blending changes rotation only, including in the seam.
        translations = [pose.local_transforms[index].translation for pose in poses]
        scales = [pose.local_transforms[index].scale for pose in poses]
        if joint.name != 'Waist_00':
            translations = blend_values(translations, windows)
            scales = blend_values(scales, windows)
        for frame in range(len(poses)):
            local[frame][index] = replace(local[frame][index], translation=translations[frame],
                                          rotation=rotations[frame], scale=scales[frame])
    return [compose_evaluated_pose(rig, pose.frame, tuple(values), pose.node_weights)
            for pose, values in zip(poses, local, strict=True)], windows
