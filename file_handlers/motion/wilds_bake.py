"""Bake Wilds ranges at 60 FPS through the native Rise retarget/import path."""
from dataclasses import replace

from .errors import MotionWriteError
from .evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from .evaluation.wilds_retarget import wilds_to_rise
from .mhr_bake import _slot
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from .mhr_import import (bake_evaluated_motion, default_motion_name, install_motion,
                         validate_import_target, verify_evaluated_motion, verify_native_contract)
from .mhr_storage import MhrMotion
from .motion_root import adjust_root_poses
from .motion_bake_timing import BAKE_FPS, sample_schedule
from .motion_bake_blend import blend_poses


def segment_timeline(document, segments, *, root_transform='relative'):
    segments = list(segments)
    motions = [_slot(document, segment.motion_id).payload.value for segment in segments]
    samples, ranges = sample_schedule(motions, segments, root_transform=root_transform)
    return BAKE_FPS, [(motions[index], frame) for index, frame in samples], ranges


def bake_wilds_segments(document, donor, segments, rig, hold_document, motion_id, *,
                        replace_existing=False, name=None, family=None, root_transform='relative',
                        align_waist=True, blend_frames=3):
    validate_import_target(document, motion_id, replace_existing=replace_existing)
    fps, samples, ranges = segment_timeline(donor, segments, root_transform=root_transform)
    retarget = wilds_to_rise(family)
    evaluators = {}
    poses = []
    for frame, (source_motion, source_frame) in enumerate(samples):
        key = id(source_motion)
        if key not in evaluators:
            evaluators[key] = retarget.evaluator(retarget.bind(source_motion, rig), PROFILE.sampling_policy,
                                                PROFILE.pose_composition_policy, PROFILE.joint_binding)
        poses.append(replace(evaluators[key].sample_frame(source_frame, wrap_looping=False), frame=frame))
    poses = adjust_root_poses(poses, rig, ranges, root_transform)
    poses, windows = blend_poses(poses, rig, ranges, align_waist=align_waist, blend_frames=blend_frames)
    motion = MhrMotion(name=default_motion_name(document, motion_id) if name is None else name,
                       end_frame=len(samples)-1, raw_start_frame=0, raw_end_frame=len(samples)-1,
                       frames_per_second=fps, looping=False)
    payload = bake_evaluated_motion(motion, poses, rig, hold_document)
    result = install_motion(document, payload, hold_document, motion_id, replace_existing=replace_existing)
    raw = RISE.write(result)
    reopened = RISE.parse(raw, label='verified segmented Wilds import')
    if RISE.write(reopened) != raw:
        raise MotionWriteError('Baked MOTLIST did not roundtrip stably')
    actual = _slot(reopened, motion_id).payload.value
    error = verify_evaluated_motion(motion, poses, actual, rig)
    goals = verify_native_contract(reopened, motion_id, actual, rig)
    return reopened, {'motion_id': motion_id, 'name': actual.name, 'end_frame': actual.end_frame,
                      'fps': fps, 'duration_seconds': actual.end_frame/fps, 'segments': ranges, 'root_transform': root_transform,
                      'waist_alignment': 'first_start_rotation' if align_waist else 'authored',
                      'blend_frames': blend_frames, 'blend_windows': windows,
                      'max_matrix_error': error, 'max_ik_goal_error': goals,
                      'events': 'Only native 001_Loop WeaponHold; source CLIP 85 is not converted'}
