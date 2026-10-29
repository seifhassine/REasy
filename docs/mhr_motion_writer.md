# Rise native motion batch writer

`services/mhr/motion_batch.py` provides the headless composition API for native
MOTLIST 528, MOT 495, MTRE 14, CLIP 43 and MOTBANK 3. The resource CLI exposes it as
`motion build`; `tools/mhr_motion_writer.py` retains its script and import entrypoints.
It writes offline candidates;
it does not deploy them or perform World-to-Rise retargeting.

```powershell
$env:REASY_MHR_NATIVE_CORPUS='absolute/path/to/extracted/em100/00/mot'
& ./.venv/Scripts/python.exe -m unittest discover -s tests -p test_mhr_native_writer.py -v
& ./.venv/Scripts/python.exe tools/mhr_motion_writer.py batch_plan.json --report report.json
& ./.venv/Scripts/python.exe -m tools motion build batch_plan.json --report report.json --dry-run --json
```

## Python API

* `services.mhr.motion_batch.execute_plan(plan)` returns verified output bytes by
  destination path plus a report, without publishing. `build_plan(plan,
  plan_path=None, report_path=None, dry_run=False)` additionally protects inputs
  and stages all output files before publication. Relative paths in the plan are
  relative to the working directory; they are not rebased against the plan file.
* `MHR_MOTION_FORMAT_CODEC.parse(bytes)` / `.write(document)` read, edit, reopen
  and rebuild source-backed native documents. Unchanged documents retain exact
  source bytes. Supported scalar, string, track and MotionAppend edits relocate
  their dependent pointers. Unsupported structural model changes raise errors.
* `neutral_motion(spec, skeleton_spec, contract)` and `build_motion(motion,
  translation_w=..., joint_unknown=..., sequence_templates=...)` construct an
  independent MOT with its own rig, channels and optional native CLIP templates.
  Joint names/order/parents can be checked against the imported mesh contract.
  Rest transforms, translation W, reserved words and Rise local coordinates must
  be supplied. The contract alone does not contain rest transforms.
* `install_payload(document, payload, motion_id, template_id=..., replace=False)`,
  `delete_slot`, and `bind_slot` edit slot tables and payload ownership. Deleting
  a rig anchor preserves the retained motions' skeleton. Alias slots retain
  independent row metadata and override sequences.
* `copy_sequences`, `delete_sequences`, `edit_clip` and `copy_event` isolate
  the selected motion/override wrapper, CLIP, metadata and pointer table before
  structural edits. `ClipGraph` supports native-template node/property/key
  copying and deletion, timing/value/flag edits and dependent curve/range rebuilds.
  All seven metadata DWORDs are exposed: authored ID and filter pages 0–5.
* `RiseTree`, `build_tree`, `set_leaf`, `tree_from_template` expose the graph,
  links, root and leaf bank/motion references. Known native classes are MotionNode,
  SyncBlendNode, OctSyncBlendNode and VariablesNode. Leaf triples and node parameters
  must agree; authored IDs must be unique; ownership and cycles are checked.
* `validate_references(bank, documents, uvar, paths)` checks every MOTBANK entry,
  resource path, MTRE leaf and parameter GUID/group against the supplied bank UVAR.
  The em100 UVAR embeds commonEnemyVariable/BlendRatio; its native UUID bytes are
  compared directly with MTRE link bytes.
* `audit_shared_payloads` and `audit_shared_sequences` expose physical sharing.
  `RiseEditSession.apply(...)` / `.undo()` retain byte snapshots for exact undo,
  including native padding and sharing. Undo is an in-process edit-session API.

## Batch input, schema 1

`tests/fixtures/mhr_motion_writer.py` generates `batch_plan.json` and verified
candidates from an explicit em100 corpus and SHA-256 source manifest. It supplies
all seven native lists and an eighth offline tree list, adds an independently
generated two-joint motion, a tree branch, sound/hit-event templates and a MOTBANK
entry. Its IDs and event values are test fixtures, not game-ready bindings.

```powershell
& ./.venv/Scripts/python.exe tests/fixtures/mhr_motion_writer.py --source 'PATH/to/source' --manifest 'PATH/to/source_manifest.json' --output 'PATH/to/new-fixture-output'
```

The source root contains `natives/stm/enemy/em100/00/mot` and the sibling
`user_variables` directory. Manifest `resources` contain relative `path` and
`sha256` pairs, plus top-level `archives` and `precedence` provenance. The output
directory must be new. Existing corpus files, reports and logs are not overwritten.

Top-level fields:

* `schema_version: 1`; `documents` maps caller names to `template`, `output`,
  `bank_id` and `resource_path`.
* `skeleton` contains `coordinates: "rise_local"`, `asset`, and ordered `joints`.
  Each joint requires `name`, `parent` index/null, `translation`, `rotation`,
  `translation_w` and `reserved_word`. Optional `skeleton_contract` is a JSON path.
* `operations` is an ordered list, each with `document` and `operation`.
  Operations are `motion`, `tree`, `event`, `copy_sequences`, `delete_sequences`,
  `clip_edit`, `delete_slot` and `bind_slot`. Optional `slot_options` permits
  `flags` and `tag_hash` only.
* `bank` contains `template`, `output`, optional `uvar_template`, `items`
  (`bank_id`, `path`, `bank_type`, `bank_type_mask_bits`) and optional `delete_ids`.
  When supplied, the complete bank must match the supplied documents.

Motion operations require `motion_id`, `template_id`, and `motion`: name, FPS,
end frame, raw start/end frames, looping and per-joint translation/rotation/scale
channels, each with matching frame/value arrays. Optional sync grids expose raw
block/point counts, start/end bytes and frame arrays; `frame_curves` expose native
frame/scalar pairs. Optional sequence/append templates select existing native
resources. Loop intros are rejected; represent them as separate motions.

Tree operations select an existing tree template and supply name, root authored
ID, nodes (template node index, authored ID, name, parameters by hash/name), links
(input/output authored IDs, pins, type, optional output GUID), and motion references.

Event operations require `clock: "target_motion_frames"`, target motion ID and
scope (`motion` or `override`), sequence index/category, explicit authored ID and
`source` selecting document, motion ID, scope, sequence and root-child node index.
Optional `properties` selects unique leaf names and replaces template key frames,
values and start/end boundaries. Key counts must match; use ClipGraph for count
changes. `shift`, all six `filter_pages`, and `clip_total_frame` are explicit.
The tool does not equate a CLIP's total frame with its MOT duration.

All candidates are validated and reopened before any output is written. Outputs
must have distinct paths and cannot overwrite input templates, the plan, or its
skeleton/UVAR dependencies. The optional report cannot replace a resource output.
`--dry-run` produces the result without writing resources or a report file.
Publication stages all bytes first, replaces each file atomically, and restores
replaced files if a later replacement fails; it is not atomic across a system crash.
Reports include hashes and payload/sequence sharing. Rebuilding a reopened
candidate must produce identical bytes.

## Proven scope and remaining semantics

The em100 native corpus from base 000 plus patch 001 (001 wins) contains seven
lists, 524 slots, 311 unique MOTs, 80 trees and 220 tree leaf references. Tests
exercise full byte-preserving rebuilds, native auxiliary edits, relocated tracks,
slot add/replace/delete/alias, shared CLIP isolation, sound and hit-event template
edits, graph branches, bank edits, independent rig construction and exact undo.

The following remain caller/compiler obligations or explicitly unsupported:

* Nonempty MOT property/custom sections at header pointers +0x20/+0x28 are absent
  from this corpus and rejected. Unknown MTRE classes, tags, header resources,
  input GUIDs and unrecognized nonzero owned bytes are rejected.
* Sync-grid phase bytes and adjacent frame/scalar curves have explicit native
  structure but unresolved consumer meaning. The scalar is not labeled playback
  speed. MotionAppend hashes/values are writable; game-side meanings remain unknown.
* The serialization and UVAR GUID closure do not establish blend algorithms,
  sync behavior or gameplay correctness. Known tree parameters retain native types.
* Convert source clip/action clocks into target MOT frames before writing. Resolve
  canonical rest transforms, axis/unit conversion and retargeting outside this tool.
* Audio trigger IDs require external WEL/HIRC closure. No native start/stop loop
  bit has been established here. The observed EnemyHitAttackTrack contains hit-data
  IDs, hit-group enums and loop/continuation flags, with no collision geometry or
  judging algorithm in those fields. Their external bindings require consumer
  evidence; a specific RCOL association is not established here. World IDs are not inferred.
* JMAP is preserved as a path/dependency, not rebuilt. No in-game runtime validation
  or Glavenus content reconstruction is claimed.
