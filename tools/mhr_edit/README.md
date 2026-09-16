# tools/mhr_edit

Small, verified MHR (Monster Hunter Rise) editing helpers for work the GUI does not cover:
motion-list payloads/CLIP sequences, BHVT hit references and RCOL attack judgments.

Every tool **reads a source file and writes a candidate into an output directory** – it never
touches the game. Each one re-parses its own output and asserts the engine-side contract it
depends on, so a candidate is either byte-verified or the tool fails loudly.

Run them with the repository virtual environment:

```bash
./.venv/Scripts/python.exe tools/mhr_edit/<tool>.py ...
```

## Engine conventions these tools rely on (all measured on native files)

| Rule | Value |
| --- | --- |
| MOT 495 payload layout | animation block at `payload+0x80`, then sequences, then the name block, `size` 16-aligned |
| Shared rig | `pointers[0] >= size` means "no skeleton of my own"; the single 001_Loop payload is the file's anchor (`size == 0`) |
| Slot row `+0x0C` | private per-motion data; a new motion must write **0** (inheriting e.g. 0x1A collapses the pose to a T-pose) |
| IK goals | goal world transform == driven bone world transform: `L_Hand_IK←L_Arm_03`, `R_Hand_IK←R_Arm_03`, `L_Foot_IK←L_Leg_02`, `R_Foot_IK←R_Leg_02` (T+R); `LookAt←Head_00` position only, rotation a single identity key |
| Sequence region | `table[align_up(count*8,16)]`, then per sequence `wrapper(64) + clip(align 16) + tracks(28*N, align 16)`; every stored pointer is **payload-relative** |
| CLIP key values | scalar types (`U8..U64`, `S8..S64`, `BOOL`, `F32/F64`) are stored **inline** in the key record at `+16`; string-valued keys use `payload` as a section offset |
| BHVT hit action | `snow.player.fsm.PlayerHitAction2._hitIndex` selects the RCOL request set **id** (not `field0`, not the table position) |
| RCOL request set | its own attack params live at `object_table[request table index]`; its shape window is `object_table[shape.user_data_index + request.shape_offset]` |
| RCOL `_HitStartDelay` / `_HitEndDelay` | window start frame and **duration** (window = start .. start+duration) |
| Userdata graph | every userdata instance owns a dedicated `via.physics.UserData` container (1 child each) |
| In-place patching | only the **last** payload may be rewritten (the tail swap); never repoint an existing slot to a later payload |

## Tools

### Effects (`.pfb.17`)

* `dump_pfb.py` – PFB/SCN/USR structure dump: type histogram, the `EPVStandardData.Elements` table
  (ID, `.efx` path, joint, offset/rotation, variant group) and a regex search over string fields.
* `add_pfb_element.py` – add one element by **deep-cloning** an existing one: the element itself plus
  the sub-structures that live in its Object arrays - `via.effect.script.EPVDataElement.GroupInfo`
  (`GroupInfoList`), `via.effect.script.GroupNameParameter` (`GroupNameParameters`),
  `via.effect.script.EffectCustomExternParameter` (`ExternParameters`) and
  `via.effect.script.EffectManager.LODInfo` (`LODLevels`).  Gives the clone a new `ID`, a fresh GUID
  and optionally a rotation tweak (`--rotate-delta 0,0,10` = roll about the front-back axis,
  `--rotate X,Y,Z` = absolute).  Verifies every pre-existing element (in array order, duplicates
  included) is untouched and that nothing but ID/rotation/GUID differs from the source.

  **Instance order matters.**  The RSZ instance table is written "referencee first": an instance
  follows everything it points at, so the container root (`via.effect.script.EPVStandardData`, which
  references every element through its `Elements` array) is always the **last** instance - true in
  every native PFB and in hand-made ones.  A new element appended *behind* the root is invisible to
  the game: `containerID=150, element=99` stays silent while the untouched element 15 plays normally,
  with no field differing between the two.  The tool therefore inserts the cloned subtree at the
  root's current index (children, then the element, then the root) and asserts the root is still last.

### Motions (`.motlist.528`)

* `copy_clip_sequences.py` – copy whole CLIP sequences (e.g. `SOUND`, `VFX`) from one motion into
  another, retiming property ranges and key frames. Rebases pointers from the model's own
  relocation table and then compares the decoded clip graph with the source.
  `--shift N` moves every copied event by N frames, `--total-frame target` sets the clip duration.
* `drop_clip_events.py` – silence events from a frame on (or exactly `--frames 109,123`) by zeroing
  the inline trigger id. Nothing moves, so the file keeps its size. **Never delete a loop's stop
  event**: skip ids that the culling (footstep/movement) tracks also use.
* `dump_clip.py` – decoded CLIP tree per motion (nodes, properties, frames, key values).
* `set_clip_effect_id.py` – retarget a string-valued CLIP key in place (e.g. a `VFXRangeTrack`
  `EffectId`): the CLIP keeps string values in its own pool and a key's `payload` indexes it, so a
  same-length replacement is a few bytes of surgery.  Shared storage is detected and refused.
  Effect IDs are `"<provider>-<elementID>"`: prefix 0 = `epvs-prg` (program/FSM effects, where
  `PlayerFsm2ActionSetEffect._ElementID` also resolves), 50 = `epvs-mot`, 100 = another provider.
* `dump_sequence_layout.py` – byte layout of a motion's sequence region (wrapper/clip/tracks spans).

### FSM (`.motfsm2.43`)

* `clone_attack_node.py` – clone an attack node with brand-new actions/conditions/identities.
* `fsm_node_dump.py` – dump one node completely (actions, shared references, states, transitions).
* `point_fsm_hit_request.py` – point a node's `PlayerHitAction2` at an RCOL request set id; asserts
  that exactly the bound field changed.

### RCOL (`.rcol.20`)

* `add_rcol_request.py` – add a request set that **reuses an existing 中央/根元/先端 composite group**
  (no new shapes) and copies every attack parameter from a template request set (default
  `気刃斬りフィニッシュ`). Creates the missing `via.physics.UserData` containers and verifies that all
  pre-existing request graphs are unchanged.
* `edit_rcol_request.py` – set scalar parameters of one request set
  (`--set _HitStartDelay=40 --set _HitEndDelay=7`), then verify that only those fields moved.

## Typical workflows

```bash
GAME="C:/Program Files (x86)/Steam/steamapps/common/MonsterHunterRise"

# 1) import a Wilds motion into a Rise hunter motlist (native layout, IK goals, WeaponHold, contract checks)
./.venv/Scripts/python.exe -m tools.import_wilds_motion \
    --source <src.motlist.992> --target <plw_*.motlist.528> \
    --hold-template <plw_*.motlist.528> --motion 293:620 --output out/

# 2) give the new FSM node an attack judgment
./.venv/Scripts/python.exe tools/mhr_edit/add_rcol_request.py "$GAME/natives/STM/player/hit/LongSword.rcol.20" out/ --name atk_620
./.venv/Scripts/python.exe tools/mhr_edit/point_fsm_hit_request.py "$GAME/natives/STM/player/Fsm/LongSword/LongSword.motfsm2.43" out/ --node atk_620 --request 72

# 3) retime the judgment
./.venv/Scripts/python.exe tools/mhr_edit/edit_rcol_request.py <rcol> out/ --id 72 --set _HitStartDelay=40 --set _HitEndDelay=7

# 4) give the new motion another move's audio/effect
./.venv/Scripts/python.exe tools/mhr_edit/copy_clip_sequences.py <motlist> out/ --from 109 --to 620 --shift 0
./.venv/Scripts/python.exe tools/mhr_edit/drop_clip_events.py <motlist> out/ --motion 620 --frames 109
```

Afterwards copy the candidates over the files under `<GAME>/natives/...` (keep a `.bak-*` copy of
every file you replace) and test in game; MOTLIST/RCOL changes need a game restart, the FSM is
reloaded by the plugin.
