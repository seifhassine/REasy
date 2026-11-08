"""Append one more Action to an existing BHVT node, cloned from a native template instance.

The judgement-window actions (`PlayerFsm2ActionDamageReflex`, `...GuardPoint`, `...SetGuardFrame`)
are all `_Type/_StartFrame/_EndFrame` windows, so "give node X a reflex window" is a single action
appended to that node - no new node, no new state.

"""
from dataclasses import dataclass

from file_handlers.motfsm.motfsm_file import MotfsmFile, Action
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.authoring import block_ids, block_splice, clone_block_object, verify_block
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from file_handlers.motfsm.selection import resolve_node
from file_handlers.motfsm.values import assignments


@dataclass(frozen=True, kw_only=True)
class ActionAddPlan:
    node: str
    klass: str
    source_node: str | None = None
    exact: bool = False
    set: tuple[str, ...] = ()


def build(source: bytes, plan: ActionAddPlan) -> bytes:
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes


    def node_index(text):
        return resolve_node(doc, text)


    target = nodes[node_index(plan.node)]

    # ---------------------------------------------------------------- pick the template instance
    template = None
    owners = [nodes[node_index(plan.source_node)]] if plan.source_node else nodes
    for owner in owners:
        for reference in owner.actions:
            try:
                instance = doc.references.action(reference.id_hash, reference.ex_id)
            except (IndexError, ValueError):
                continue
            name = instance.class_name.split('.')[-1] if instance is not None else ''
            if instance is not None and (name == plan.klass if plan.exact else plan.klass.lower() in name.lower()):
                template, template_owner, template_ref = instance, owner, reference
                break
        if template is not None:
            break
    if template is None:
        raise ValueError(f'no action containing {plan.klass!r} found')

    # `v1_ID` is a per-block namespace, so the owning block must come from the reference resolver.
    resolved = doc.references.action_identities().get((template_ref.id_hash, template_ref.ex_id))
    assert resolved is not None, f'action 0x{template_ref.id_hash:08X} is not in any block'
    block_name, template_index = resolved
    template_block = doc.rsz_blocks.get_block(block_name)
    print(f'  target {target.name}  template {template.class_name} ({template_owner.name})')

    # ---------------------------------------------------------------- new Action instance
    actions_block = doc.rsz_blocks.get_block('actions')
    old_objects = list(actions_block.object_table)


    used_ids = block_ids(doc, ('actions', 'static_actions'), 1)
    new_action_id = 1
    while new_action_id != 0xFFFFFFFF and new_action_id in used_ids:
        new_action_id += 1
    overrides = {'v1_ID': new_action_id, **assignments(template, plan.set)}
    clone_block_object(actions_block, template_index, overrides, source_block=template_block)
    print(f'  new action v1_ID={new_action_id} on {target.name}: '
          + ', '.join(f'{k}={v}' for k, v in overrides.items() if k != 'v1_ID'))


    action_splice = block_splice(actions_block)
    target.actions.append(Action(new_action_id, 0))
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0]


    # ---------------------------------------------------------------- node table


    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    target.actions.pop()
    doc.bhvt.action_ex_ids = doc.bhvt.action_ex_ids[:-1]
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    target.actions.append(Action(new_action_id, 0))
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0]

    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = sorted([(nodes_start, node_tail, nodes_data), action_splice])
    output = splice_document(doc, splices)
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids + [0]
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    for name in BLOCK_NAMES:
        if name == 'actions':
            continue
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
    new_block = verified.rsz_blocks.get_block('actions')
    assert new_block.object_table[:len(old_objects)] == old_objects, 'actions object table reordered'
    assert len(new_block.object_table) == len(old_objects) + 1
    assert len(new_block.file.instance_infos) == len(actions_block.file.instance_infos)
    verify_block(doc, verified, 'actions')
    assert len(verified.bhvt.nodes) == len(original.bhvt.nodes), 'node count changed'
    check = verified.bhvt.nodes[node_index(plan.node)]
    assert len(check.actions) == len(original.bhvt.nodes[node_index(plan.node)].actions) + 1

    return bytes(output)
