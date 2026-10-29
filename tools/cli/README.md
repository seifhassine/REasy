# REasy resource CLI

Run from the repository root with its Python environment:

```powershell
.\.venv\Scripts\python.exe -m tools --help
.\.venv\Scripts\python.exe -m tools clip --help
.\.venv\Scripts\python.exe -m tools clip event-move --help
```

## Command groups

| Group | Commands |
| --- | --- |
| `fsm` | Query/dump, node/Action/Condition/State edits, cloning, derivations, events; [detailed guide](../fsm/README.md) |
| `motion` | `query`, `duplicate`, `bake`, `import-wilds`, `build` (native resource plan) |
| `audio` | `build` (bound playback graph jobs) |
| `clip` | `query`, `dump`, `layout`, `sequence-copy/delete`, `node-copy/delete`, `property-copy/delete`, `key-copy/delete`, `track-add`, `event-move`, `events-drop`, `effect-set`, `property-range`, `bool-window` |
| `pfb` | `query`, `elements`, `element-add`, `element-edit` |
| `rcol` | `query`, `request-add`, `request-edit` |
| `rsz` | `query`, `scan`, `references`, `field-edit` for PFB/SCN/USER containers |
| `efx` | `query`, `references`, `new`, `set`, `insert`, `copy`, `delete`, `move`, `replace`, `import`, `preset-export/import`, `parameter-value`, `material-value`, `curve-set/delete`, `expression-set/delete`, `sequence`, `uvs-edit`, `apply`, `batch`; RE Engine editing, World 711800 inspection |
| `batch` | Chain editing commands against one candidate file |
| `move` | `check`, `retime`: validate or retime an explicit MOTLIST/FSM/RCOL bundle |

Motion/CLIP and effect/attack recipes target MHR MOTLIST 528, MOT 495, CLIP 43,
PFB 17, and RCOL 20. Wilds import reads MOTLIST 992 and writes a Rise target.
RSZ commands accept `--registry PATH`; the default is the bundled MHRise registry.
They use the existing format handlers and reject registry mismatches.
Native edit APIs live under `file_handlers`; MHR composition and audio toolchain
APIs live under `services/mhr` and `services/audio`. CLI modules adapt those APIs.
Offline format-validation generators and manual Lua checks live under `tests/fixtures`.

## Agent contract

- Pass `--json` for one JSON document on stdout. Loader diagnostics and verification logs go to stderr.
- Query results carry typed field values and native identifiers; normal numbers remain JSON numbers.
- Editing commands take `SOURCE -o OUTPUT_FILE`. Every input is protected from overwrite, including import donors and registries.
- Edits return verified bytes to one common publisher. The candidate is replaced atomically after verification succeeds.
- `--dry-run` performs the complete edit and verification without publishing. It also works for batches.
- Single-file edit results include source/output paths, byte sizes, SHA-256 hashes, `changed`, and operation-specific `details`.
- Success exits 0; input or verification errors exit 2 and contain `status: "error"` in JSON mode.
- Run without Python `-O`: structural verification uses assertions.

No command deploys a candidate to a game automatically. Read-back verification and game behavior are separate checks.

### Native motion and audio builds

```powershell
.\.venv\Scripts\python.exe -m tools motion build batch_plan.json --report out\motion-report.json --dry-run --json
.\.venv\Scripts\python.exe -m tools audio build jobs.json -o out\audio --dry-run --json
```

`motion build` takes the [schema 1 native motion plan](../../docs/mhr_motion_writer.md).
Each document/bank names its own output file; `--report` is optional. This differs
from the argument-array `motion batch`, which chains commands against one file.

`audio build` takes `{"schema":"reasy.playback-batch/1","jobs":[...]}`. Prepare
jobs with `python -m tools.wwise_graph_importer prepare` or `prepare-music`, then
resolve the reported media, object and external bindings. A build with unresolved
bindings returns exit 2 and keeps the full `unbound` report; no banks are published.
Without `--dry-run`, that report is saved as `build-report.json` even if unbound.

Both builds interpret paths inside their plans relative to the working directory,
preserving the standalone tools' contract. They verify all resources before
publication and protect input files. Each output replacement is atomic; a later
replacement failure restores previously replaced outputs. This is not a transaction
across a process or machine crash. `--dry-run` writes neither candidates nor reports.
The original `tools/mhr_motion_writer.py` and `tools.wwise_graph_importer` commands
remain available.

### EFX structural editing

EFX paths use **zero-based list positions**, not native entry IDs. Attribute fields
are addressed as `entries/0/attributes/1/fields/LocalPosition`; append `/1` for one
vector component. Actions, bones, groups and embedded `PlayEmitter` files use the
same path vocabulary. Numeric file versions are taken from the source filename.

```powershell
.\.venv\Scripts\python.exe -m tools efx copy effect.efx.2818730 --from entries/1 --path entries --name FlameCopy -o candidate.efx.2818730 --dry-run --json
.\.venv\Scripts\python.exe -m tools efx set effect.efx.2818730 --path entries/1/attributes/2/fields/LocalPosition --value '[0, 1, 2]' -o candidate.efx.2818730 --json
.\.venv\Scripts\python.exe -m tools efx apply effect.efx.2818730 --plan edits.json -o candidate.efx.2818730 --json
```

`apply` accepts an array of operation objects and publishes only after every edit
succeeds. `insert` targets a collection; `delete` and `move` target an element.
Attribute insertion takes `{"type": "Transform3D", "fields": {...}}` with explicit
field values. Missing game defaults are not guessed. `new SOURCE -o OUTPUT` creates
an empty EFX using SOURCE's version, optionally with `--dimension 0` for 2D.

```json
[
  {"op": "copy", "source": "entries/1", "path": "entries", "name": "FlameCopy"},
  {"op": "insert", "path": "groups", "value": {"name": "Flames", "indices": [1]}},
  {"op": "move", "path": "entries/1", "index": 0}
]
```

Entry moves update group membership; action moves update particle action
references. Referenced actions and bones must be retargeted before deletion.
Attribute IDs are allocated on copy, and attribute insertion retains type order.
Variable-length arrays keep their per-element binary metadata during edits.
The GUI exposes these operations plus undo/redo through the EFX toolbar and
context menu. `Copy to…` copies an attribute to another entry in the same scope.

`import --donor FILE --from PATH --path COLLECTION` copies across files/scopes with
reachable actions, bones, expression/field parameters and group membership.
Conflicting parameter names are renamed and hashes updated; `--conflicts error`
rejects collisions instead. Incompatible UVAR bindings are rejected. Donor files
are input protected. `preset-export --from PATH` writes a portable native snapshot;
`preset-import --donor PRESET --path COLLECTION` restores its selected node and
dependencies. The GUI exposes the same format through Import, Copy/Paste and Save preset.

`replace --path RECORD --value JSON` rebuilds a structure, including behavior
variants and embedded EFX. `parameter-value` edits logical expression parameters
(Float, packed RGBA Color, three-component Range, Float2). `material-value` edits
an MDF property's four floats or texture slot. Texture table moves remap slots;
path edits update native UTF-16 lengths. Referenced slots cannot be deleted.
`dimension`, `header_flags` (raw header word 11), entry `index` and attribute
`unique_id` are also addressable. Derived counts/hashes are managed by the writer.

`curve-set` / `curve-delete` take an attribute `--path` and zero-based `--channel`.
A set value contains `value_type` (Float=5, raw Int=3) and ordered `keys` with
`time`, `value`, and the native `interpolation` code. Code 5 also requires
`tangents: {out_x,out_y,in_x,in_y}`. Channel edits update bit sets, packed frame
tables, tangent tables, durations and byte sizes together. Interpolation codes
whose behavior is unresolved retain their native numeric names. The key graph
connects control points; it does not claim to reproduce native interpolation.

`expression-set` compiles a `formula` into native postfix tokens or accepts explicit
`parameters` / `components`. Formulas support `+ - * / % **`, unary signs and the
native functions (`sin`, `cos`, `lerp`, `clamp`, `smoothstep`, `remap`, etc.). A bare
name or `p("name")` resolves a global parameter; `ext("TIMER")` declares an external
parameter; `const("name", 1.0)` declares a constant. Explicit unsigned hashes are
also accepted. `a | b` preserves the native two-root representation, whose runtime
selection semantics are unresolved. Expressions are parsed, never evaluated as Python.

Example `apply` plan (replace the attribute positions with those from `query`):

```json
[
  {"op":"curve-set","path":"entries/1/attributes/3","channel":0,
   "value":{"value_type":5,"keys":[
     {"time":0,"value":0,"interpolation":2},
     {"time":30,"value":1,"interpolation":2}]}},
  {"op":"expression-set","path":"entries/1/attributes/4","channel":0,
   "value":{"formula":"sin(ext(\"TIMER\")) * 0.5 + 0.5"}}
]
```

Material curves use slot positions and a `material` metadata object. Material
expressions use `--material` and explicit `unkn1`, `unkn2`, `mdfPropertyHash`, and
`propertyComponentIndex` in their value. Adding/deleting material channels requires
the complete `--indices` JSON table; this table's shader-specific meaning is not guessed.
Material curve flag words can be supplied with `--bits`.
Attributes with a separate `materialExpressionCount` / `materialPropertyCount`
also require explicit `--headers` values when adding/deleting material expressions.

The GUI's **Parameters** opens typed attribute forms; **Curves** and **Expressions**
edit channels and stage one undoable transaction. **Preview** supports local/loaded-PAK
UVS textures, sequence playback, a fixed random seed, timeline seeking and basic
point/box/sphere/cylinder particle approximation. Native shaders, mesh/trail rendering,
collision, sub-effect simulation, curves, expressions and bone attachment are outside
this preview renderer. Existing data for those features remains editable. A stable
serialization or editor preview does not certify an arbitrary effect's game behavior.

### EFX sequence asset bundles (Rise/Sunbreak)

`sequence` generates TEX 28, UVS 7 and EFX 2818730 from an existing donor emitter
that has `UVSequence`. It retains the donor document and its shader/behavior setup,
and changes the selected emitter's UVS reference and playback range/speed.

```powershell
.\.venv\Scripts\python.exe -m tools efx sequence donor.efx.2818730 --entry entries/1 --frames frame_00.png frame_01.png frame_02.png --resource vfx/custom/fire --fps 30 --padding 4 --compression bc7 -o output/fire_bundle --dry-run --json
.\.venv\Scripts\python.exe -m tools efx sequence donor.efx.2818730 --entry entries/1 --atlas atlas.png --rectangles '[[0,0,128,128],[128,0,128,128]]' --resource vfx/custom/fire -o output/fire_bundle --json
```

PNG arguments and rectangle lists preserve their supplied order. GUI **Sequence**
natural-sorts selected PNG names and supports explicit reordering, removal and atlas
grid slicing. Frames share a centered canvas. Power-of-two atlas cells retain transparent
padding; alpha-weighted mipmaps are filtered in linear light for sRGB inputs and stop
before crossing cell boundaries. BC7 uses `ispc_texcomp`; `--compression rgba8` is
lossless. `--linear` declares linear input; `--no-mipmaps` disables mip generation.
Playback uses the Rise authoring convention `PlaySpeed = fps / 60` and selects the
single generated sequence; runtime playback remains subject to the donor's flags.

Output must be a **new directory**. All native outputs are verified before publication.
`manifest.json` records generated resources and external references.
`--dependency-root UNPACKED_ROOT` copies resolvable dependencies, recursively following
EFX and UVS references. The GUI can resolve these from its current resource context.
Missing resources are recorded as `unresolved_dependencies`; opaque shader/material
containers are copied unchanged and their internal dependencies are not crawled.

**Linked UVS** edits an existing UVS with EFX references loaded. CLI equivalent:

```powershell
.\.venv\Scripts\python.exe -m tools efx uvs-edit donor.efx.2818730 --uvs original.uvs.7 --resource vfx/original.uvs --effect-resource vfx/custom/effect --plan uvs_edits.json -o output/linked_bundle --json
```

Plans use `sequence-`, `frame-`, or `texture-` plus `insert/set/copy/delete/move`.
`index` is the selected position, `to` is a copy/move destination, and frame operations
also specify `sequence`. Insert/set take `value` objects (UV rectangles, texture paths,
or `patterns` for a sequence). An `efx` operation wraps a normal EFX operation under
`operation` for explicit retargeting. Sequence/frame selections are remapped as
half-open ranges (equal endpoints retain a fixed selection). Deleting referenced slots
or splitting a random range into non-contiguous positions is rejected. Linked edits
save EFX and UVS together, with optional dependency copying.

For corpus verification, `tools/audit_efx.py --exercise-edits` clones/removes an emitter
and undoes back to the original bytes. `--exercise-construction` reconstructs an example
of every encountered attribute type and validates curve/expression structures;
`--exercise-imports` imports representative native entries with their dependencies.

### RSZ field metadata

`rsz query/scan`, `pfb elements`, and `rcol query` retain numeric `value`, storage
`type`, and `reference`, and include `native_type`. Enum fields also report
`enum_name`, all matching aliases in `enum_names`, `enum_value`, and `enum_matched`.
Unknown values retain their raw number with a null name and `enum_matched: false`;
combined bit flags are not guessed. As in the client, signed/unsigned 32-bit enum
matches are recognized without changing the stored `value`.

The top-level `enums` map contains the used types' member definitions, selected
from the document registry rather than the GUI's active game. Arrays and embedded
structs expose recursive `items` records and `count` alongside their raw values.
FSM queries/dumps expose the same metadata in each instance's `field_details`,
while retaining the existing `fields`, `field_types`, and `native_types` maps.

## Examples

### Bake animation ranges

```powershell
.\.venv\Scripts\python.exe -m tools motion bake path\to\plw_LongSword_100.motlist.528 --target 620 --replace --segment 101 10 40 2 --segment 102 20 80 0.5 -o out\baked.motlist.528 --json
```

Each repeatable `--segment MOTION START END SPEED` selects an inclusive frame range
from the input MOTLIST (`END` can be `end` for the source's final frame).
`2` means twice the playback speed; `0.5` means half speed.
One segment performs a crop; multiple segments are baked in argument order. Omit
`--replace` to create an unused target MotionID, optionally with `--name`.
An existing target may also be a source. `--dry-run` verifies without writing.

The output uses **60 FPS** and integer key frames. Source sampling advances at the
requested speed; if the end falls between output frames, the endpoint is sampled
on the next integer frame. Only that final interval is shortened in source time.
In this example A occupies frames 0–15 and B occupies 16–136. Continuous ranges
of the same motion and Root policy share their boundary key; speed changes add no
duplicate frame. A single-frame range contributes one pose.

`--root-transform relative` (CLI default) normalizes each cut's initial Root
translation, rotation and scale, then continues from the preceding cut's final
transform. The row-vector rule is `current × inverse(start) × previous_end`.
`identity` clears the Root transform throughout (T=0, R=identity, S=1); `authored`
keeps the original transforms. `--segment-root-transform` supplies one policy per
segment, in the same order, overriding the global setting. A speed change within
the same continuous clip/policy retains its original normalization reference.

`--segment-root-translation-scale 1 0.3 1` scales only the second segment's Root
displacement to 30%. Supply one nonnegative multiplier per segment (default 1).
Rotation and scale are unchanged. The next segment continues from the resulting
position; returning to 1 restores the original displacement increments without
snapping back to the unscaled trajectory. The editor exposes this as **Root travel ×**.

`--waist-rotation align` (default) aligns each cut's starting `Waist_00` rotation to
the first cut's starting rotation, preserving its subsequent relative rotation.
Use `authored` to disable alignment. Waist translation and scale are unchanged.
`--blend-frames 3` (default) uses three existing frames before and three after each
cut. The outer poses anchor a smoothstep interpolation of translation/scale and a
shortest-path quaternion SLERP of rotation. No frames are inserted or removed.
Root retains its segment policy; Waist_00 blends **rotation only**. IK goals are
rebuilt from the final pose. `--blend-frames 0` disables blending. Continuous speed
changes do not create blend windows. Overlapping windows require a smaller count.

Sources and target must share a skeleton/rest pose. Channels are combined by bone
hash; a channel absent in one segment uses its known rest value. Missing external
bone defaults and differing per-bone weights are rejected. Shared target payloads
are detached before editing. Output is nonlooping, with playback range 0–end.

Only animation channels and duration are baked. Existing target CLIP events,
overrides, and slot metadata stay unchanged; a new slot inherits the first source's
events. Donor events, weapon attachment timing, FSM and RCOL timing are not merged
or retimed. Use the CLIP/FSM/RCOL commands separately when needed.

In the Rise editor, select the target slot and choose **Bake segments…**. Select
source animations, start/end frames, speed and Root transform policy in row order;
choose waist alignment and blend frames per side. Accepting updates the preview and marks the document modified for normal
Save. Duplicate the target first if a new slot is desired.

### Bake Wilds ranges directly into Rise

Pass a 992 `--donor` to `motion bake` to retarget selected source frames directly
into one 528 slot. No intermediate imported animation slots are created. It uses
the same hunter rig, weapon-family carriers, IK goal baking and native payload
verification as `motion import-wilds`. `--game-dir` and `--hold-template` select
the same resources as that command.

This example appends Gunlance 620: 295 frames 38–67 at 1.6× with Root cleared, then
296 frames 30–80 at 1.6× and 80–end at 1×, preserving relative Root transforms,
aligning waist rotations and blending three frames on each side of the cut:

```powershell
.\.venv\Scripts\python.exe -m tools motion bake C:/myproj/Assets/ModSources/natives/STM/player/mot/plw_GunLance_100.motlist.528 --donor C:/myproj/Assets/ModSources/Weapon/Wp07/wp07_00/wp07_00.motlist.992 --hold-template C:/myproj/Assets/ModSources/natives/STM/player/mot/plw_GunLance_100.motlist.528 --target 620 --segment 295 38 67 1.6 --segment 296 30 80 1.6 --segment 296 80 end 1 --segment-root-transform identity relative relative --waist-rotation align --blend-frames 3 -o out/plw_GunLance_100.motlist.528 --json
```

The output ranges are 0–19, 20–52 and 52–169 at 60 FPS (2.816667 seconds). The first
segment samples 38, 39.6, …, 66.8, 67; the second samples 30, 31.6, …, 79.6, 80.
The endpoint rounding adds 0.875 and 0.75 output-frame intervals respectively.
The blend window is 17–22; frame 52 changes speed without resetting alignment,
Root normalization, or introducing another blend. All resulting keys use the
60 FPS integer grid.

Root transforms are processed after retargeting; world matrices and native IK goals
are rebuilt from the adjusted pose. Only the native WeaponHold sequence is installed;
source CLIP 85 events and existing target events are not carried into a donor import.
This command does not change FSM/RCOL, deploy to the game, or create a playback link.

### Bake LMT segments from multiple files

Use repeated `--lmt-source NAME PATH` declarations and `--segment NAME:MOTION START END SPEED`.
Sources must be XX LMT v67; they cannot be combined with `--donor` or other formats.
Names are case-sensitive and start with a letter, followed by letters, digits, `_` or `-`.
Each segment uses its own source file's MotionID; equal IDs in different files remain distinct.
Frame ranges are inclusive; `end` selects the source animation's final frame.

From the REasy project directory, this appends a short test composite of regular and hunter-art clips:

```powershell
$py = '.\.venv\Scripts\python.exe'
$target = 'C:/myproj/REasy/tests/TESTFILE/natives/STM/player/mot/plw_SlashAxe_100.motlist.528'
& $py -m tools motion query 'C:/myproj/REasy/tests/TESTFILE/xx/slashaxe.lmt' --motion 122 --json
& $py -m tools motion bake $target `
  --lmt-source base 'C:/myproj/REasy/tests/TESTFILE/xx/slashaxe.lmt' `
  --lmt-source arts 'C:/myproj/REasy/tests/TESTFILE/xx/slashaxe_hunterart.lmt' `
  --segment base:122 32 36 1 --segment arts:1 0 4 2 `
  --target next --hold-template $target --blend-frames 1 `
  -o 'C:/myproj/REasy/.cache/lmt-composite/plw_SlashAxe_100.motlist.528' --dry-run --json
```

`--target next` allocates a free ID. To replace an existing animation, use its numeric
`--target ID --replace`; `next` and `--replace` cannot be combined. Remove `--dry-run`
to publish the verified output. Inputs are never overwritten, including every named LMT source.
The JSON report includes resolved source aliases, per-segment source/output frame ranges,
resulting MotionID, duration, blend windows, and pose/IK verification errors.
The command also works inside a `motion batch` argument-array plan.

The XX hunter rig is bundled; `--skeleton` optionally overrides it for all LMT sources.
The target's weapon family selects its native `001_Loop` template unless `--hold-template`
is supplied. Known main-weapon events become WeaponHold keys at the sampled output times;
sub-weapon bindings keep their native template states. Replacement preserves the target's
other CLIP tracks and overrides with their existing timing. New slots do not inherit extra
sound/effect/camera tracks. `--segment-root-transform`, `--segment-root-translation-scale`,
`--waist-rotation` and `--blend-frames` use the same policies as the GUI LMT baker.

To append generated transition frames after all source segments, add
`--transition-to base:1 0 10`. This adds exactly 10 new frames leading to source
`base` MotionID 1 frame 0. Existing frames are not trimmed or overlapped. Local
translation/scale use smoothstep interpolation and rotations use shortest-path
SLERP; the final Root transform stays fixed, so the character settles at its
current position. The last added frame reaches the target pose for every non-Root
joint. WeaponHold retains the outgoing state during the transition and takes the
reference pose's state on its last frame. The JSON `transition` record reports
the reference and generated frame range. With a single `--donor`, use a plain
MotionID instead of `NAME:MOTION`. This is independent of `--blend-frames` and
`--waist-rotation`; use `--blend-frames 0 --waist-rotation authored` to leave the
source segments' body rotations unaltered.

### Other resource operations

```powershell
$py = '.\.venv\Scripts\python.exe'
$mot = 'path\to\plw_LongSword_100.motlist.528'
$pfb = 'path\to\effects.pfb.17'
$rcol = 'path\to\LongSword.rcol.20'

& $py -m tools motion query $mot --json
& $py -m tools clip dump $mot --motion 101 --json
& $py -m tools motion duplicate $mot --motion 101 --new-id 620 -o out\motion.motlist.528 --json
& $py -m tools clip event-move $mot --motion 101 --track-index 0 --delta 3 -o out\retimed.motlist.528 --json

& $py -m tools pfb elements $pfb --json
& $py -m tools pfb element-edit $pfb --id 15 --rotate-delta 0,0,10 -o out\effects.pfb.17 --json
& $py -m tools pfb element-add $pfb --clone 15 --new-id 99 -o out\cloned.pfb.17 --json

& $py -m tools rcol query $rcol --json
& $py -m tools rcol request-edit $rcol --id 32 --set _HitEndDelay=7 -o out\LongSword.rcol.20 --json
& $py -m tools rcol request-add $rcol --template-id 32 --name new_attack -o out\added.rcol.20 --json

& $py -m tools rsz query $pfb --class EPVStandardData.Element --limit 10 --json
& $py -m tools rsz field-edit $pfb --instance 5 --set PlaySpeed=1.25 -o out\speed.pfb.17 --json
& $py -m tools rsz scan path\to\resources --recursive --class EPVStandardData.Element --field ID --json
```

Inspect the actual input to choose IDs and instances; example IDs are not universal defaults.
PFB element IDs can repeat: cloning an ambiguous ID requires `--clone-position`, as reported by `pfb elements`.
PFB cloning preserves rotation unless a rotation option is given.
RSZ `field-edit` edits scalar values; arrays and object references require their native structural operations.

Wilds import takes the Rise target as its primary source and the Wilds list as `--donor`:

```powershell
& $py -m tools motion import-wilds $mot --donor source.motlist.992 --motion 203:620 --hold-template $mot -o out\imported.motlist.528 --json
```

It retains native rig, IK, payload-layout, and WeaponHold verification. Source CLIP 85 events are not converted.

## Structural CLIP edits

`clip dump` reports sequence positions, parsed `node_index` and `property_index`, and
`key_index` within each property. It also reports slot `overrides`. Indices describe
the current input: inspect again after structural edits before reusing indices.

```powershell
& $py -m tools clip sequence-copy $mot --from 101 --to 620 --categories SOUND,VFX --shift 3 -o out\copied.motlist.528
& $py -m tools clip sequence-delete $mot --motion 620 --sequence 1 -o out\deleted.motlist.528
& $py -m tools clip node-copy $mot --from 101 --to 620 --source-category VFX --category VFX --node-index 1 --target-node-index 0 -o out\node.motlist.528
& $py -m tools clip property-delete $mot --motion 620 --category VFX --property-index 2 -o out\property.motlist.528
& $py -m tools clip key-copy $mot --from 101 --to 620 --source-category SOUND --category SOUND --property-index 0 --key-index 1 --target-property-index 0 --frame 15 -o out\key.motlist.528
```

Sequence copying defaults to `SOUND,VFX`; use repeatable `--sequence` instead to
select exact source positions. Sequence deletion requires an explicit selector.
Category selectors must resolve uniquely. Copy commands accept `--donor PATH`
for another MOTLIST. `--scope override` selects target slot overrides;
`--source-scope override` selects source slot overrides. Graph copy indices select
the source object; `--target-node-index` or `--target-property-index` selects its
new owner. Graph delete indices select the target object. Node operations include
the subtree, and property operations include keys. Property copies can target a
node or a container property. Root-child node edits also update their native track
metadata. A nested source node has no root track metadata and therefore cannot be
copied directly to the root. Shared MOT payloads are separated for the selected slot.
`key-delete` removes the key record; `events-drop` retains its existing value-zeroing behavior.

## Batch plans

A top-level plan contains argument arrays beginning with group and operation:

```json
[
  ["motion", "duplicate", "--motion", "101", "--new-id", "620"],
  ["clip", "events-drop", "--motion", "620", "--from-frame", "100"]
]
```

```powershell
& $py -m tools batch $mot --plan edits.json -o out\edited.motlist.528 --json
```

Omit the source, output, and `--dry-run` from individual steps. Each step reads the previous verified candidate.
Failure preserves an existing final output and removes intermediates. Paths in options are relative to the
working directory; absolute input paths are convenient for agents.
Group-local batches such as `tools clip batch` and `tools fsm batch` omit the group name in each step.

## Format-specific contracts

- Structural CLIP edits rebuild native tables and relocate recorded pointers, including slot overrides;
  the target may be any physical payload. Candidates are reopened and verified before publication.
- `track-add` replaces both old track scripts. It inserts a four-key BOOL leaf into a flat compact CLIP root,
  preserves native curve/speed/terminal-key sections and extra ranges, and relocates only recorded pointers.
- `effect-set` requires the replacement string to occupy the same encoded length and refuses shared storage.
- `property-range` extends a selected property's end and last key; it does not shorten the range.
- `bool-window` retimes a four-key BOOL track in place: keys become `(0,False) (on,True) (off,True) (off+1,False)`
  and the property range becomes `0 .. off+1`; the file size does not change.
- Fixed-size event edits use `file_handlers.motion.mhr_events` and reject changes
  that would also change another motion slot sharing the same storage. Structural
  CLIP operations can detach shared payloads before such edits.
- RCOL request `id`, table position, and `field0` are distinct. Inspect `rcol query` before editing.
- RCOL query/edit selectors are `--id`, `--index`, or `--field0`. Request cloning allocates a fresh field0 independently of its authoring ID.
- Motion duplication/import keeps slot rows and pointers sorted by MotionID; physical payload order stays independent of slot order.
- EFX queries expose header words and printable strings; they do not infer semantic effect IDs from arbitrary byte patterns.

## Move bundles

A move plan names authored FSM nodes explicitly. Paths are relative to the plan file.
Choose the actual IDs and bank from the source; the values below are examples:

```json
{
  "motion": {"path": "plw_LongSword_100.motlist.528", "id": 620, "bank": 100},
  "fsm": {"path": "LongSword.motfsm2.43", "nodes": ["atk_620"]},
  "rcol": {"path": "LongSword.rcol.20", "field0": [72]}
}
```

```powershell
& $py -m tools move check move.json --json
& $py -m tools move retime move.json --frames 79 -o out\candidate --dry-run --json
& $py -m tools move retime move.json --frames 79 -o out\candidate --json
```

`check` reports authored motion/bank references, state-map ownership, field0 hit
references, CLIP timing, derivation windows, and hit/just windows. A window starting
after the animation ends is reported as a warning; invalid references fail with exit 2.
It does not infer inherited runtime branches or prove game behavior.

`retime` resamples animation channels to the requested end frame and scales CLIP
keys/ranges/extra spans, PlayerFsm2Command StartFrame/PreFrame/EndFrame, SetEffect
_Frame, and RCOL
Hit/Just StartDelay plus EndDelay **duration**. EndFrame=0 remains unbounded. Integer
windows round their start and endpoint separately; hitstop, armor/invulnerability
timers, IDs, and other parameters remain unchanged. Shared motion payloads, command
conditions, and hit requests must be separated before changing their timing.
CLIP curve/speed/relative-key retiming is currently rejected with an explicit error.

The output directory must be new. All three resources and a reusable `move.json`
are staged, reopened, checked together, then published by directory rename. Failure
leaves no partial candidate; `--dry-run` leaves no candidate directory. Bundle
results contain before/after checks and the applied frame factor.

## Script migration

Standalone resource scripts expose commands under `tools/cli/formats`, with shared
native operations in `file_handlers` and composed workflows in `services`.
Replace positional `SOURCE OUTDIR` with `SOURCE -o OUTPUT_FILE`.

| Former script | Command |
| --- | --- |
| `dump_clip.py`, `dump_sequence_layout.py` | `clip dump`, `clip layout` |
| `copy_clip_sequences.py` | `clip sequence-copy` |
| `add_clip_track.py`, `insert_clip_track.py` | `clip track-add` |
| `drop_clip_events.py`, `move_clip_effect.py` | `clip events-drop`, `clip event-move` |
| `set_clip_effect_id.py`, `set_clip_property_range.py` | `clip effect-set`, `clip property-range` |
| `dump_pfb.py` | `rsz query` / `pfb elements` |
| `add_pfb_element.py`, `edit_pfb_element.py` | `pfb element-add`, `pfb element-edit` |
| `add_rcol_request.py`, `edit_rcol_request.py` | `rcol request-add`, `rcol request-edit` |
| `dump_efx.py` | `efx query` |
| `import_wilds_motion.py` | `motion import-wilds` (Rise target first; Wilds source becomes `--donor`) |

`python -m tools.fsm` remains available and uses the same runtime as `python -m tools fsm`.
Historical engine conventions and combined workflows are preserved in [MHR notes](MHR.md).
