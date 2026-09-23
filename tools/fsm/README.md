# REasy FSM CLI

FSM commands are also available through the shared [resource CLI](../cli/README.md): `python -m tools fsm ...`.

From the repository root, use `./.venv/Scripts/python.exe -m tools.fsm`.
`--help` lists the commands; each command also has `--help`.
The MHR editing operations use the existing MOTFSM model, RSZ registry, writer,
and native reference resolver. They produce candidates; they do not deploy to a game.

## Inspect, then edit

```powershell
$py = '.\.venv\Scripts\python.exe'
$fsm = 'path\to\LongSword.motfsm2.43'
& $py -m tools.fsm query $fsm --name atk --actions --limit 10 --json
& $py -m tools.fsm dump $fsm --node 'root.atk.atk_101' --json
& $py -m tools.fsm action-edit $fsm -o 'out\LongSword.motfsm2.43' --node 'atk_101' --class PlayerPlayMotion2 --exact --set v4_MotionID=620 --json
```

All node selectors accept a unique name, a full dotted path, `index:123`,
or `0x12345678:0` (hash plus extension ID). `0x12345678` also works when the hash
is unique. Use `name:` or `path:` to explicitly select the interpretation.
Ambiguous selections fail and list exact alternatives. Indexes refer to the current input file.

Scalar integers accept decimal or an explicit `0x` prefix; leading zeroes still mean decimal.
`--set FIELD=VALUE` is repeatable. Scalar edits validate against the original field type and width.
Enum fields accept native member names, such as `CmdType=AtkXR1`. Resolution uses
the field's registry type (`FsmCommandBase.CommandFsm` here), not Lua input-enum values.
`action-edit --position N` selects an Action slot; without it, every matching Action on that node
is edited. Shared Action instances remain shared. `dump` reports their users.
`condition-edit` refuses to change unselected users of a shared condition unless `--allow-shared` is given.

## Commands

| Command | Purpose |
| --- | --- |
| `query` | Filter by node name, motion, Action class, or outgoing target; optional Action/State detail |
| `dump` | Full node data, Action field values/types, relations, and incoming references |
| `action-edit` | Set scalar fields on existing Actions |
| `action-add` | Clone an Action template and append a fresh reference |
| `condition-edit` | Set fields on conditions selected with `--states 0-9` or `--states 0,2,5` |
| `state-target` | Change a state's target by `--state N --target NODE` |
| `state-add` | Clone a derivation with a new transition map; `--insert-before N` controls priority |
| `state-order` | Reorder existing states with a complete `--order 2,0,1` permutation |
| `state-condition` | Replace a condition using `--template-node` and `--template-state` |
| `state-own-map` | Give an existing state its own transition-map ID |
| `state-add-event` | Clone one event from `--from-node`, `--from-state`, and `--from-event` |
| `event-copy` | Clone a *shared* transition event into a private copy for one state (`--node --states --class [--set]`), so it can be edited alone |
| `event-edit` | Edit existing event scalars by `--states` or `--incoming`, with native enum names |
| `node-clone` | Clone a leaf with fresh transition maps sharing the template data; events stay shared |
| `attack-clone` | Attack recipe with independently copied transition maps/data |
| `node-chainsaw` | Charge Axe branch recipe using a native template |
| `hit-request` | Set `PlayerHitAction2._hitIndex` using `--field0`, or resolve `--id` through `--rcol` |
| `effect` | Set a SetEffect Action's element, container, or frame |
| `batch` | Apply several edits and publish one final candidate |

`node-clone` and `attack-clone` copy leaf-shaped nodes, not arbitrary subtrees.
Their native layout/graph verification remains mandatory. Every cloned state gets a
fresh map ID. Chainsaw `_HitId` and PlayerHitAction2 `_hitIndex` both reference RCOL
`field0`; authoring `id` and table position are separate. `--request` is an alias for
`--field0`. Supplying `--rcol` also validates a direct field0 reference.

`event-edit --incoming` selects states targeting the given node. It edits existing
events, and does not append duplicate turn events. Shared unselected event users
require `--allow-shared`; `state-add-event` can create an independent event first.

## Agent output and publication

`--json` emits exactly one JSON document on stdout; diagnostics go to stderr.
Query results contain `matched`, `returned`, and `nodes`. Action records include identity,
class, typed values, editable field names, and reference users. Numeric values remain JSON
numbers; node/Action selector identities are hex strings.

Every editing command requires `-o OUTPUT_FILE`. The source cannot be overwritten.
The candidate is verified before atomic replacement of the output file. The JSON result
includes source/output paths, sizes, SHA-256 hashes, and `changed`/`dry_run` flags.
`--dry-run` performs the edit and verification without writing the output.
Exit status is 0 for success (including unchanged values), 2 for invalid input or failed
verification. Run without Python `-O`, which would disable structural assertions.

## Batch edits

The plan is a JSON array of argument arrays. Each step starts with an editing command;
omit the source, `-o`, and `--dry-run` inside steps:

```json
[
  ["action-edit", "--node", "atk_101", "--class", "PlayerPlayMotion2", "--exact", "--set", "v4_MotionID=620"],
  ["hit-request", "--node", "atk_101", "--field0", "72"]
]
```

```powershell
& $py -m tools.fsm batch $fsm --plan edits.json -o 'out\LongSword.motfsm2.43' --json
```

Each step reads the previous verified candidate. Failure leaves the final output untouched;
temporary intermediates are removed. No automatic deployment or runtime verification is performed.

## Migration from `tools/mhr_edit`

The standalone FSM scripts have been folded into this package. Replace `SOURCE OUTDIR`
with `SOURCE -o OUTPUT_FILE`, and use the corresponding command:

| Previous script | Command |
| --- | --- |
| `fsm_query.py` / `fsm_node_dump.py` | `query` / `dump --node ...` |
| `edit_fsm_action_fields.py` / `add_fsm_action.py` | `action-edit` / `action-add` |
| `edit_fsm_condition_fields.py` | `condition-edit` |
| `set_fsm_state_target.py` / `add_fsm_derivation.py` | `state-target` / `state-add` |
| `set_fsm_condition.py` / `own_fsm_transition.py` | `state-condition` / `state-own-map` |
| `add_fsm_transition_event.py` | `state-add-event` |
| `clone_fsm_node.py` / `clone_attack_node.py` | `node-clone` / `attack-clone` |
| `add_fsm_chainsaw_branch.py` | `node-chainsaw` |
| `point_fsm_hit_request.py` / `point_fsm_effect.py` | `hit-request` / `effect` |

Attack cloning now requires explicit template, parent, name, motion, and hit-index arguments.
Use the common node selectors instead of an unprefixed hex template ID.
