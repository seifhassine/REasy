"""Bake Wilds ranges at 60 FPS through the native Rise retarget/import path."""
from dataclasses import replace
import math

from .errors import MotionWriteError
from .evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from .mhr_bake import _slot
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from .mhr_import import (bake_evaluated_motion, default_motion_name, install_motion,
                         validate_import_target, verify_evaluated_motion, verify_native_contract, source_retarget)
from .mhr_storage import MhrMotion
from .motion_root import adjust_root_poses
from .motion_bake_timing import BAKE_FPS, sample_schedule
from .motion_bake_blend import blend_poses, append_pose_transition


def _source_document(document, source, sources):
    if source is None:
        return document
    if sources is None or source not in sources:
        raise MotionWriteError(f'Unknown segment source: {source}')
    return sources[source]


def segment_timeline(document, segments, *, root_transform='relative', sources=None):
    segments = list(segments)
    motions = []
    for segment in segments:
        source = _source_document(document, segment.source, sources)
        motions.append(_slot(source, segment.motion_id).payload.value)
    samples, ranges = sample_schedule(motions, segments, root_transform=root_transform)
    return BAKE_FPS, [(motions[index], frame) for index, frame in samples], ranges


def bake_wilds_segments(document, donor, segments, rig, hold_document, motion_id, *,
                        replace_existing=False, name=None, family=None, root_transform='relative',
                        align_waist=True, blend_frames=3, sources=None, transition=None):
    validate_import_target(document, motion_id, replace_existing=replace_existing)
    fps, samples, ranges = segment_timeline(donor, segments, root_transform=root_transform, sources=sources)
    evaluators = {}
    poses = []
    def evaluator(source_motion):
        key = id(source_motion)
        if key not in evaluators:
            retarget = source_retarget(source_motion, hold_document, family)
            evaluators[key] = retarget.evaluator(retarget.bind(source_motion, rig), PROFILE.sampling_policy,
                                                PROFILE.pose_composition_policy, PROFILE.joint_binding)
        return evaluators[key]
    transition_motion = None
    if transition is not None:
        transition_motion = _slot(_source_document(donor, transition.source, sources), transition.motion_id).payload.value
        if not math.isfinite(transition.frame) or not 0 <= transition.frame <= transition_motion.end_frame:
            raise MotionWriteError('Transition reference frame must be within its source motion')
        if isinstance(transition.frames, bool) or not isinstance(transition.frames, int) or transition.frames <= 0:
            raise MotionWriteError('Transition frames must be a positive integer')
    for frame, (source_motion, source_frame) in enumerate(samples):
        poses.append(replace(evaluator(source_motion).sample_frame(source_frame, wrap_looping=False), frame=frame))
    poses = adjust_root_poses(poses, rig, ranges, root_transform)
    poses, windows = blend_poses(poses, rig, ranges, align_waist=align_waist, blend_frames=blend_frames)
    transition_report = None
    if transition is not None:
        reference = evaluator(transition_motion).sample_frame(transition.frame, wrap_looping=False)
        first = len(poses)
        poses = append_pose_transition(poses, rig, reference, transition.frames)
        # Hold the outgoing attachment during the synthetic frames, then adopt
        # the target pose's discrete attachment state at the final frame.
        samples.extend([samples[-1]]*(transition.frames-1) + [(transition_motion, transition.frame)])
        transition_report = dict(motion_id=transition.motion_id, source=transition.source,
                                 reference_frame=transition.frame, frames=transition.frames,
                                 output_start=first, output_end=len(poses)-1, root_transform='hold',
                                 interpolation='smoothstep_slerp')
    motion = MhrMotion(name=default_motion_name(document, motion_id) if name is None else name,
                       end_frame=len(samples)-1, raw_start_frame=0, raw_end_frame=len(samples)-1,
                       frames_per_second=fps, looping=False)
    hold_keys = None
    sequence_source = None
    from .lmt_codec import LmtMotion
    if isinstance(samples[0][0], LmtMotion):
        from .lmt_events import sampled_weapon_hold_keys
        from .preview.mhr_attachments import weapon_hold_properties
        from .mhr_import import idle_template
        from .lmt_import import rewrite_hold_clips
        hold = hold_document.slots[idle_template(hold_document)].payload.value
        hold_keys = sampled_weapon_hold_keys(samples, weapon_hold_properties(hold))
        if replace_existing:
            document = rewrite_hold_clips(document, motion_id, hold_keys, motion.end_frame)
            sequence_source = document, motion_id
    payload = bake_evaluated_motion(motion, poses, rig, hold_document, sequence_source=sequence_source, hold_keys=hold_keys)
    result = install_motion(document, payload, hold_document, motion_id, replace_existing=replace_existing,
                            preserve_overrides=sequence_source is not None)
    raw = RISE.write(result)
    reopened = RISE.parse(raw, label='verified segmented Wilds import')
    if RISE.write(reopened) != raw:
        raise MotionWriteError('Baked MOTLIST did not roundtrip stably')
    actual = _slot(reopened, motion_id).payload.value
    if hold_keys is not None:
        from .lmt_import import verify_hold_keys
        verify_hold_keys(actual, hold_keys)
    error = verify_evaluated_motion(motion, poses, actual, rig)
    goals = verify_native_contract(reopened, motion_id, actual, rig)
    details = {'motion_id': motion_id, 'name': actual.name, 'end_frame': actual.end_frame,
                      'fps': fps, 'duration_seconds': actual.end_frame/fps, 'segments': ranges, 'root_transform': root_transform,
                      'waist_alignment': 'first_start_rotation' if align_waist else 'authored',
                      'blend_frames': blend_frames, 'blend_windows': windows,
                      'max_matrix_error': error, 'max_ik_goal_error': goals,
                      'events': ('LMT main-weapon events converted to WeaponHold; other target CLIP tracks preserved on replacement'
                                 if hold_keys is not None else 'Only native 001_Loop WeaponHold; source events are not converted')}
    if transition_report is not None:
        details['transition'] = transition_report
    return reopened, details


def bake_lmt_segments(document, sources, segments, rig, hold_document, motion_id, **options):
    from .lmt_codec import LmtDocument
    if not sources or any(not isinstance(source, LmtDocument) for source in sources.values()):
        raise MotionWriteError('LMT segment baking requires one or more LMT source documents')
    return bake_wilds_segments(document, next(iter(sources.values())), segments, rig, hold_document,
                               motion_id, sources=sources, **options)
