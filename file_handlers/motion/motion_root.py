"""Root transform policies shared by native and retargeted segment baking."""
import numpy as np
from dataclasses import replace

from .errors import MotionWriteError
from .evaluation.composition import compose_evaluated_pose
from .evaluation.math3d import transform_matrix, decompose_row_srt
from .evaluation.model import Transform


ROOT_TRANSFORM_MODES = ('relative', 'identity', 'authored')


def root_transforms(transforms, ranges, mode):
    """Use row-vector local transforms: relative = current @ inverse(start)."""
    if mode not in ROOT_TRANSFORM_MODES:
        raise MotionWriteError(f'Unknown root transform mode: {mode}')
    output = list(transforms)
    previous = None
    previous_mode = None
    for interval in ranges:
        current_mode = interval.get('root_transform') or mode
        if current_mode not in ROOT_TRANSFORM_MODES:
            raise MotionWriteError(f'Unknown segment root transform mode: {current_mode}')
        start, end = interval['output_start'], interval['output_end']
        continuous = (previous is not None and previous['motion_id'] == interval['motion_id']
                      and previous['end'] == interval['start'] and previous_mode == current_mode)
        if current_mode == 'relative' and not continuous:
            origin = np.asarray(transform_matrix(transforms[start])).reshape(4, 4)
            try:
                inverse_origin = np.linalg.inv(origin)
            except np.linalg.LinAlgError as exc:
                raise MotionWriteError('Cannot normalize a singular Root transform at a segment start') from exc
            destination = np.asarray(transform_matrix(output[start-1] if start else Transform())).reshape(4, 4)
        for frame in range(start, end+1):
            if current_mode == 'identity':
                output[frame] = Transform()
            elif current_mode == 'relative':
                current = np.asarray(transform_matrix(transforms[frame])).reshape(4, 4)
                output[frame] = decompose_row_srt((current @ inverse_origin @ destination).ravel())
            else:
                output[frame] = transforms[frame]
        previous = interval
        previous_mode = current_mode
    return scale_root_translation(output, ranges, mode)


def scale_root_translation(transforms, ranges, mode):
    """Scale each interval's displacement and carry the accumulated position on."""
    if all(row.get('root_translation_scale', 1.0) == 1 for row in ranges):
        return transforms
    output = list(transforms)
    previous = None
    for row in ranges:
        start, end = row['output_start'], row['output_end']
        current_mode = row.get('root_transform') or mode
        origin = transforms[start].translation
        if previous is not None and start == previous['output_end']:
            destination = output[start].translation
        elif previous is not None and current_mode == 'relative':
            destination = output[start-1].translation
        else:
            destination = origin
        factor = row.get('root_translation_scale', 1.0)
        for frame in range(start, end+1):
            translation = tuple(offset+(value-base)*factor for value, base, offset
                                in zip(transforms[frame].translation, origin, destination, strict=True))
            output[frame] = replace(transforms[frame], translation=translation)
        previous = row
    return output


def root_index(joints):
    matches = [i for i, joint in enumerate(joints) if joint.name.casefold() == 'root']
    if len(matches) != 1:
        raise MotionWriteError('Root transform processing requires one bone named Root')
    return matches[0]


def adjust_root_poses(poses, rig, ranges, mode):
    if mode == 'authored' and all(row.get('root_transform') in (None, 'authored')
                                and row.get('root_translation_scale', 1.0) == 1 for row in ranges):
        return poses
    index = root_index(rig.joints)
    transforms = root_transforms([pose.local_transforms[index] for pose in poses], ranges, mode)
    output = []
    for pose, transform in zip(poses, transforms, strict=True):
        local = list(pose.local_transforms)
        local[index] = transform
        output.append(compose_evaluated_pose(rig, pose.frame, tuple(local), pose.node_weights))
    return output
