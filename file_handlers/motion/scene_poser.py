"""See docs/internals/motion-editor.md, Preview."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial

lazy import numpy as np

lazy from file_handlers.engine_math import mathex_rotation, transform_product
lazy from file_handlers.jcns.evaluate import Evaluation, MeshMaterials, Results
lazy from file_handlers.jcns.rig import Rig as ConstraintRig
lazy from file_handlers.jcns.rules import Enums, evaluated
lazy from file_handlers.rsz.scn_scene_graph import (
    compose_parented_transform, instance_world, joint_constraint_phases, mesh_joints,
)
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import resource_key

from .banks import bank_joint_map, bank_references, entry_mirrored, entry_motion, entry_reversed, list_paths, read_list
from .controllers import MaterialController, material_controllers
from .layers import (
    MotionJoints, MotionLayer, joined_masks, joint_map_extras, joint_map_mirror, joint_mask_groups, layer_joint_masks,
    mask_weights, motion_component, property_joints,
)
from .model import Motion, MotionTree
from .pose import MotionSampler, Rig
from .rules import joint_attributes, layer_enums
from .tree_pose import TreePose
from .tree_sampler import TreeSampler, change_tree, motion_tree, tree_variables


@dataclass(slots=True)
class _Constraints:
    holder: object
    rig: ConstraintRig
    layers: list
    weights: dict[int, float]
    materials: MeshMaterials | None = None


@dataclass(slots=True)
class _Rig:
    rig: Rig
    owner: int | None
    follows: tuple[np.ndarray, np.ndarray]
    binding: tuple[np.ndarray, np.ndarray] | None = None
    moves: bool = False
    rest: np.ndarray | None = None
    constraints: _Constraints | None = None
    layer: MotionLayer = MotionLayer()
    joints: MotionJoints | None = None
    stack: tuple = ()
    deforms: tuple[int, ...] = ()
    hung: tuple[int, int, np.ndarray, np.ndarray, object] | None = None
    copies: tuple[tuple[int, np.ndarray, np.ndarray], ...] = ()
    holder: object = None


@dataclass(slots=True)
class _Skinned:
    key: str
    object_id: object
    rig: int
    joints: np.ndarray
    valid: np.ndarray
    inverse_binds: np.ndarray
    morphs: tuple[tuple[str, int], ...]
    start: np.ndarray | None
    moves: bool = False
    slots: tuple[int, ...] = ()


class _RigBuild:
    def __init__(self, graphs, loaded: dict, documents: dict):
        self.loaded, self.documents = loaded, documents
        self.objects: dict[object, int] = {}
        self.meshes = {}
        for graph in graphs:
            for renderable in graph.renderables:
                if renderable.key in loaded:
                    self.meshes.setdefault(renderable.source_object_id,
                                           getattr(loaded[renderable.key].handler, "mesh", None))
        self.built: dict[int, object] = {}
        self.rows: dict[tuple[int, int | None], int] = {}
        self.items: dict[int, object] = {}
        self.places: dict[int, str | None] = {}
        self.rigs_of: dict[object, list[int]] = {}
        self.rigs: list[_Rig] = []
        self.skinned: list[_Skinned] = []
        self.hosts, self.held_by, self.hangers, self.named, self.forced, self.lifted = {}, {}, {}, {}, set(), set()
        for graph in graphs:
            for instance in graph.document_instances.values():
                if (hung := graph.hung_spawns.get(instance.instance_id)) is not None:
                    host_instance, host_id, root_id = hung
                    hosting = graph.documents[host_id.document_id]
                    self.hangers[instance.instance_id] = (root_id, hosting, hosting.objects[host_id], host_instance,
                                                          instance_world(graph.document_instances[host_instance],
                                                                         hosting.objects[host_id]),
                                                          instance.base_world_matrix)
                link = None if instance.source_link_index is None else graph.links[instance.source_link_index]
                owned = graph.spawned_owners.get(instance.instance_id)
                held = owned or (link and link.source_folder_id)
                document = graph.documents.get(held.document_id) if held else None
                if document is not None and (item := document.objects.get(held)) is not None:
                    self.hosts[instance.document_id] = (document, item)
                    self.held_by[instance.instance_id] = (document, item,
                                                          None if owned else link.source_document_instance_id)
                if instance.instance_id in graph.followed_joints:
                    self.named[instance.document_id] = {murmur3_hash_utf16le(name)
                                                        for name in graph.followed_joints[instance.instance_id]}
                self.forced.update((instance.document_id, item)
                                   for item in graph.same_joints.get(instance.instance_id, ()))
                self.lifted.update((instance.document_id, item)
                                   for item in graph.reparented.get(instance.instance_id, ()))

    def joints_of(self, document, item):
        if (found := document.joints.get(item.id)) is not None or (mesh := self.meshes.get(item.id)) is None:
            return found
        if id(mesh) not in self.built:
            self.built[id(mesh)] = mesh_joints(mesh)
        return self.built[id(mesh)]

    def rig_of(self, document, item, instance: str | None = None) -> int | None:
        joints = self.joints_of(document, item)
        if joints is None:
            return None
        owner = None
        parent = None if (document.document_id, item.id) in self.lifted else document.objects.get(
            document.object_by_local_id.get(item.parent_id))
        listed = self.named.get(document.document_id) if parent is None else None
        if item.transform is not None and (item.transform.same_joints_constraint or listed
                                           or (document.document_id, item.id) in self.forced):
            holder = (self.held_by.get(instance) or self.hosts.get(document.document_id)) if parent is None else (
                document, parent, instance)
            owner = None if holder is None else self.rig_of(*holder)
        if (key := (id(joints), owner)) not in self.rows:
            rig = Rig.of_joints(joints)
            followed = {} if owner is None else {
                value: row for row, value in enumerate(self.rigs[owner].rig.hashes)}
            pairs = [(row, followed[value]) for row, value in enumerate(rig.hashes)
                     if value in followed and (listed is None or value in listed)]
            self.rows[key], self.items[len(self.rigs)], self.places[len(self.rigs)] = len(self.rigs), item, instance
            self.rigs.append(_Rig(rig, owner, (np.array([row for row, _ in pairs], np.int64),
                                               np.array([row for _, row in pairs], np.int64))))
        if self.rows[key] not in (found := self.rigs_of.setdefault(item.id, [])):
            found.append(self.rows[key])
        self.objects[item.id] = self.rows[key]
        return self.rows[key]

    def add_skinned(self, graphs) -> None:
        for graph in graphs:
            for renderable in graph.renderables:
                document = graph.documents.get(renderable.source_object_id.document_id)
                item = None if document is None else document.objects.get(renderable.source_object_id)
                if item is None or renderable.key not in self.loaded:
                    continue
                joints = self.joints_of(document, item)
                if joints is None or joints.inverse_binds is None or not joints.remap:
                    continue
                index = self.rig_of(document, item, renderable.document_instance_id)
                remap = np.array(joints.remap, np.int64)
                valid = (remap >= 0) & (remap < len(joints.names))
                remap = np.where(valid, remap, 0)
                inverse = np.asarray(joints.inverse_binds, np.float32)[remap].transpose(0, 2, 1)
                mesh = getattr(self.loaded[renderable.key].handler, "mesh", None)
                self.skinned.append(_Skinned(renderable.key, renderable.source_object_id, index, remap, valid, inverse,
                                             _morphs(self.loaded[renderable.key].handler), renderable.start_pose,
                                             slots=tuple(murmur3_hash_utf16le(mesh.names[at]) for at in getattr(
                                                 mesh, "blend_shape_indices", None) or ())))

    def hang_of(self, document, item, instance):
        parent = document.objects.get(document.object_by_local_id.get(item.parent_id))
        transform = item.transform
        if transform is None or transform.same_joints_constraint and any(
                len(self.rigs[at].follows[0]) for at in self.rigs_of.get(item.id, ())):
            return None
        if not transform.parent_joint:
            above = None if parent is None else self.hang_of(document, parent, instance)
            return None if above is None or transform.absolute_scaling or not isinstance(above[3], np.ndarray) \
                else (*above[:3], transform_product(above[3], transform.local_matrix), above[4])
        if parent is not None:
            hosting, host, held, world, base = document, parent, instance, parent.document_world_matrix, None
        elif (hung := self.hangers.get(instance)) is not None and hung[0] == item.id:
            _root, hosting, host, held, world, base = hung
        else:
            return None
        if (owner := self.rig_of(hosting, host, held)) is None or (row := next(
                (row for row, value in enumerate(self.rigs[owner].rig.hashes)
                 if value == murmur3_hash_utf16le(transform.parent_joint)), None)) is None:
            return None
        return owner, row, world, (transform if transform.absolute_scaling else transform.local_matrix), base

    def add_hangs(self) -> None:
        for index, item in list(self.items.items()):
            if (hang := self.hang_of(self.documents[item.id.document_id], item, self.places[index])) is not None:
                owner, row, world, placed, base = hang
                self.rigs[index].hung = owner, row, np.linalg.inv(
                    item.document_world_matrix if base is None else transform_product(
                        base, item.document_world_matrix)), world, placed

    def add_copies(self) -> None:
        for index, item in list(self.items.items()):
            document = self.documents[item.id.document_id]
            target = document.objects.get(document.copies.get(item.id))
            if (target is None or target is item
                    or (source := self.rig_of(document, target, self.places[index])) is None):
                continue
            found, pairs = self.rigs[source], {}
            owned = dict(zip(*(column.tolist() for column in found.follows)))
            by_hash = {value: row for row, value in enumerate(found.rig.hashes)}
            for row, value in enumerate(self.rigs[index].rig.hashes):
                if (other := by_hash.get(value)) is not None:
                    at, other = (found.owner, owned[other]) if other in owned else (source, other)
                    pairs.setdefault(at, []).append((row, other))
            self.rigs[index].copies = tuple((at, *(np.array(column, np.int64) for column in zip(*listed)))
                                            for at, listed in pairs.items())

    def constraints_of(self, index: int) -> _Constraints:
        entry = self.rigs[index]
        if entry.constraints is None:
            host = None if entry.owner is None else self.constraints_of(entry.owner).rig
            entry.constraints = _Constraints(self.items[index].id, _constraint_rig(entry, self.items[index], host),
                                             [], {})
        return entry.constraints

    def add_constraints(self, graphs, game: str, edited: tuple[str, object] | None,
                        phases: tuple[int | str, ...]) -> None:
        enums = Enums.of_game(game) if game else None
        edited_key = None if edited is None else resource_key(edited[0])
        used = False
        for graph in graphs if enums is not None else ():
            for document in graph.documents.values():
                for object_id, layers in document.constraints.items():
                    item = document.objects.get(object_id)
                    joints = None if item is None else self.joints_of(document, item)
                    models = [(edited[1] if resource_key(layer.path) == edited_key else layer.model, layer.blend_rate,
                               layer.timing) for layer in layers if not layer.paused and layer.timing in phases]
                    used |= any(model is edited[1] for model, *_rest in models) if edited is not None else False
                    models = [(model, *rest) for model, *rest in models
                              if model is not None and evaluated(model.version)]
                    for index in self.rigs_of.get(object_id, ()) if joints is not None and models else ():
                        constraints = self.constraints_of(index)
                        constraints.layers += [(Evaluation(model, constraints.rig, enums), rate, timing)
                                               for model, rate, timing in models]
        main = max((index for index, entry in enumerate(self.rigs) if entry.owner is None),
                   key=lambda index: len(self.rigs[index].rig.hashes), default=None)
        if edited is not None and not used and enums is not None and evaluated(edited[1].version) and main is not None:
            constraints = self.constraints_of(main)
            constraints.layers.append((Evaluation(edited[1], constraints.rig, enums), 1.0, phases[-1]))


class ScenePoser:
    def __init__(self, graphs, loaded_meshes, game: str = "", edited: tuple[str, object] | None = None, read=None):
        self.game, self._read, self._graphs = game, read, graphs
        self._banks: dict[object, list[str]] | None = None
        self._locals: dict[int, np.ndarray] = {}
        self._maps: dict[str, tuple[dict, tuple, bool, tuple | None]] = {}
        self._mirrored, self._reverse, self._writes, self._compose, self._variables = False, False, None, None, None
        self._scales: dict[int, float] = {}
        self._weights: dict[object, dict[int, float]] = {}
        self._lists: dict = {}
        self._bank_lists: dict[tuple, list[tuple[int, str]]] = {}
        self._played: dict[tuple, TreeSampler | None] = {}
        self._join: tuple[MotionSampler | None, MotionSampler | None] = (None, None)
        self._controllers: list[MaterialController] | None = None
        self._surfaces: dict[object, list] = {}
        self._frame: tuple[dict, dict, dict] = ({}, {}, {})
        self._last_pose: tuple | None = None
        self._documents = {key: document for graph in graphs for key, document in graph.documents.items()}
        self.phases = joint_constraint_phases()
        build = _RigBuild(graphs, {mesh.key: mesh for mesh in loaded_meshes}, self._documents)
        build.add_skinned(graphs)
        build.add_hangs()
        build.add_copies()
        build.add_constraints(graphs, game, edited, self.phases)
        self.objects, self._rigs_of, self.rigs, self.skinned, self._hosts = (
            build.objects, build.rigs_of, build.rigs, build.skinned, build.hosts)

    def bind(self, sampler: MotionSampler | None, holders: tuple = (), bank: int | None = None,
             path: str | None = None, mirrored: bool = False, reverse: bool = False, changes: dict | None = None,
             writes=None, masks: dict | None = None, layer: int | None = None,
             mask: tuple[int, int] | None = None, variables=None) -> None:
        sampler, self._mirrored, self._reverse, self._writes = self._joined(sampler), mirrored, reverse, writes
        self._variables = variables
        holders = tuple(dict.fromkeys(self._motion_holder(holder) for holder in holders))
        if path is not None and sampler is not None:
            holders += self._synced(sampler, holders, path)
        played = {}
        for holder in holders or (None,):
            for index in self._players(sampler, holder):
                played.setdefault(index, holder)
        bindings = {index: self.rigs[index].rig.bind(sampler.hashes) for index in played}
        while followed := [(self.rigs[index].owner, played[index]) for index, (rows, _slots) in bindings.items()
                           if self.rigs[index].owner not in (None, *bindings) and len(rows)
                           and set(rows.tolist()) <= set(self.rigs[index].follows[0].tolist())]:
            for index, holder in followed:
                if index not in bindings:
                    played[index], bindings[index] = holder, self.rigs[index].rig.bind(sampler.hashes)
        changing = {self._motion_holder(holder): layers for holder, layers in (changes or {}).items()} \
            if sampler is not None else {}
        changed = {index: holder for holder in changing for index in self._players(sampler, holder) - played.keys()}
        first = holders[0] if holders else None
        stacks = {} if sampler is None else {holder: self._stack(
            holder, bank, sampler, changing.get(holder), (masks or {}).get(holder), layer if holder == first else None,
            mask)
            for holder in dict.fromkeys((first, *played.values()))}
        played |= changed
        stacks |= {holder: self._stack(holder, None, None, changing[holder], (masks or {}).get(holder))
                   for holder in set(changed.values())}
        rigged = set()
        for index in sorted(played):
            stack, _extras, _deforms, groups, table = stacks[played.get(index, first)]
            for _layer, player, masks in stack:
                if isinstance(player, TreeSampler) and id(player) not in rigged:
                    rigged.add(id(player))
                    player.context.parent_rotations = partial(self._parent_rotations, played.get(index, first), index)
                    player.set_rig(self.rigs[index].rig, masks, groups, table)
        interpolating = joint_attributes(self.game).get("EnableEmptyInterpolation", 0) if self.game else 0
        for index, entry in enumerate(self.rigs):
            entry.binding = bindings.get(index)
            entry.holder = played.get(index, first)
            stack, extras, deforms, _groups, table = stacks.get(entry.holder, ((), (), False, {}, None))
            entry.layer, entry.joints, entry.stack, entry.deforms = MotionLayer(), None, (), ()
            if entry.binding is not None or index in changed:
                joints = MotionJoints.of(entry.rig, extras, (value for _layer, player, _masks in stack
                                                             if player is not None for value in player.hashes),
                                         interpolating, table)
                entry.layer = next((layer for layer, player, _masks in stack if player is sampler), MotionLayer())
                entry.joints, entry.deforms = joints, tuple(value for value, _flags in extras) if deforms else ()
                entry.stack = tuple((layer, player, *((None, None) if player is None else (
                    binding := joints.bind(player.hashes), _weights(joints, binding, player, masks))))
                    for layer, player, masks in stack)
            # Moved: a layer binding a rig joint or a deform value's joint (not only joints the rig lacks).
            shown = np.arange(0 if entry.joints is None else len(entry.joints.hashes)) < len(entry.rig.hashes)
            if entry.deforms:
                shown[entry.joints.bind(entry.deforms)[0]] = True
            entry.moves = entry.constraints is not None or any(
                bound is not None and shown[bound[0]].any() for _layer, _player, bound, _weight in entry.stack)
        for _ in self.rigs:
            for entry in self.rigs:
                entry.moves = entry.moves or any(self.rigs[leader].moves for leader in (
                    entry.owner, entry.hung and entry.hung[0], *(at for at, _own, _other in entry.copies))
                    if leader is not None)
        properties = () if sampler is None else sampler.properties
        for skinned in self.skinned:
            skinned.moves = self.rigs[skinned.rig].moves or any(value in properties for _name, value in skinned.morphs)

    def _property_joints(self, motion: Motion) -> frozenset:
        data = self._read(motion.jmap_path) if motion.jmap_path and self.game and self._read is not None else None
        return frozenset() if data is None else property_joints(data, self.game)

    def bank_lists(self, holders: tuple = ()) -> list[tuple[int, str]]:
        if self._read is None:
            return []
        if holders not in self._bank_lists:
            self._bank_lists[holders] = [pair for holder in holders or tuple(self._bank_paths())
                                         for path in self._bank_paths().get(holder, ())
                                         for pair in list_paths(self._read, path)]
        return self._bank_lists[holders]

    def _joined(self, sampler: MotionSampler | TreeSampler | None) -> MotionSampler | TreeSampler | None:
        if isinstance(sampler, TreeSampler):
            if self._join[0] is not sampler:
                self._join = sampler, sampler
                sampler.set_joints(self._property_joints)
            return sampler
        if sampler is None or not self.game or self._read is None or not sampler.motion.jmap_path:
            return sampler
        if self._join[0] is not sampler:
            data = self._read(sampler.motion.jmap_path)
            joints = frozenset() if data is None else property_joints(data, self.game)
            self._join = sampler, MotionSampler(sampler.motion, sampler, joints) if joints else sampler
        return self._join[1]

    def _parent_rotations(self, holder, index: int) -> dict:
        document = self._documents.get(holder.document_id) if holder is not None else None
        item = None if document is None else document.objects.get(holder)
        parent = None if item is None else document.objects.get(document.object_by_local_id.get(item.parent_id)) or \
            self._hosts.get(document.document_id, (None, None))[1]
        at = None if parent is None else self.objects.get(parent.id)
        if at is None or at == index:
            return {}
        rig, entry = self.rigs[at].rig, self.rigs[at]
        if self._compose is not None and entry.joints is not None:
            hashes, rotations = entry.joints.hashes, self._compose(at)[2].values[1]
        else:
            hashes, rotations = rig.hashes, rig.base[1]
        own = set(self.rigs[index].rig.hashes)
        return {value: tuple(rotation.tolist()) for value, rotation in zip(hashes, rotations) if value in own}

    def layers(self, holder) -> tuple:
        return () if holder is None else self._motion(self._motion_holder(holder))[0]

    def parts(self, holder) -> list:
        if (index := self.objects.get(holder)) is None:
            return []
        root = self._root(index)
        return [(self._documents[key.document_id], self._documents[key.document_id].objects[key])
                for key, at in self.objects.items() if key.document_id != holder.document_id and self._root(at) == root]

    def _motion_holder(self, holder):
        document = self._documents.get(holder.document_id)
        item = None if document is None else document.objects.get(holder)
        while item is not None and motion_component(document, item, self.game) is None:
            parent = document.objects.get(document.object_by_local_id.get(item.parent_id))
            document, item = (document, parent) if parent is not None else self._hosts.get(
                document.document_id, (None, None))
        return holder if item is None else item.id

    def _synced(self, sampler: MotionSampler, holders: tuple, path: str) -> tuple:
        """A character's Motions naming one bank play its motions together."""
        roots = {self._root(index) for holder in holders for index in self._players(sampler, holder)}
        named = dict.fromkeys(self._motion_holder(holder) for holder, paths in self._bank_paths().items()
                              if path.casefold() in (listed.casefold() for listed in paths))
        return tuple(holder for holder in named if holder not in holders
                     and roots & {self._root(index) for index in self._players(sampler, holder)})

    def _root(self, index: int) -> int:
        for _ in self.rigs:
            entry = self.rigs[index]
            if (leader := entry.owner if entry.owner is not None else entry.hung and entry.hung[0]) is None:
                break
            index = leader
        return index

    def _bank_paths(self) -> dict[object, list[str]]:
        if self._banks is None:
            self._banks = {}
            for reference in bank_references(self._graphs):
                for owner in reference.holders:
                    self._banks.setdefault(owner, []).append(reference.path)
        return self._banks

    def _players(self, sampler: MotionSampler | None, holder) -> set[int]:
        if sampler is None:
            return set()
        if holder is None:
            keyed = set(sampler.hashes)
            owners = [index for index, entry in enumerate(self.rigs) if entry.owner is None]
            return {max(owners, key=lambda index: len(keyed.intersection(self.rigs[index].rig.hashes)))} if owners \
                else set()
        if holder in self.objects:
            return set(self._rigs_of[holder])
        return {index for object_id, index in self.objects.items()
                if self.rigs[index].owner is None and self._below(object_id, holder)}

    def runtime_holder(self, sampler: MotionSampler | TreeSampler | None):
        """The object whose Motion plays a list no holder's bank names, as game code plays a bank it adds at run time:
        the Motion holder (``_motion_holder``) of the rig the motion keys most; None without one."""
        index = next(iter(self._players(sampler, None)), None)
        return next((self._motion_holder(object_id) for object_id, at in self.objects.items() if at == index), None)

    def _below(self, object_id, holder) -> bool:
        document = self._documents.get(object_id.document_id)
        item = None if document is None or holder.document_id != object_id.document_id else document.objects.get(
            object_id)
        while item is not None and item.id != holder:
            if motion_component(document, item, self.game) is not None:
                return False
            item = document.objects.get(document.object_by_local_id.get(item.parent_id))
        return item is not None

    def _stack(self, holder, bank: int | None, sampler, changes: dict | None = None, masks: dict | None = None,
               picked: int | None = None, mask: tuple | None = None) -> tuple[list, tuple, bool, dict, tuple | None]:
        """A joint-pose motion's layer is added: the engine reads its frames only through add-blends."""
        layers, groups, extras, deforms, table = self._motion(holder)
        own = layer_joint_masks(groups, MotionLayer(mask=mask[0], mask_mode=layer_enums(self.game)[1].get(
            mask[1], "ID"))) if mask and mask[0] else None
        layers = tuple(replace(layer, mask=(masks or {}).get(at, layer.mask))
                       for at, layer in enumerate(layers or (MotionLayer(),)))
        chosen = -1 if sampler is None else picked if picked is not None and picked < len(layers) else next(
            (at for at, layer in enumerate(layers) if layer.bank == bank), 0)
        if getattr(getattr(sampler, "motion", None), "joint_poses", None):
            layers = (*layers[:chosen], replace(layers[chosen], add=True), *layers[chosen + 1:])
        changes = changes or {}
        layers = (*layers, *(MotionLayer(),) * (max(changes, default=-1) + 1 - len(layers)))

        def changed(at):
            tree = change_tree(changes[at], partial(self.tree, (holder,)))
            found, variables = tree_variables(self._read, tree), self._variables
            names = {name: guid for guid, name, _value in found}
            return TreeSampler(tree, partial(self.motion, (holder,)), {
                guid: value for guid, _name, value in found}, self._property_joints,
                partial(self.mirrored, (holder,)), partial(self.reversed, (holder,)), partial(self.masked, (holder,)),
                None if variables is None else lambda frame: {names[name]: value for name, value in variables(
                    frame).get((holder, at), {}).items() if name in names})
        return [(layer, sampler if at == chosen else changed(at) if at in changes else self._player(
            holder, at, layer), joined_masks(layer_joint_masks(groups, layer), own if at == chosen else None))
            for at, layer in enumerate(layers)], extras, deforms, groups, table

    def _motion(self, holder) -> tuple[tuple[MotionLayer, ...], dict, tuple, bool, tuple | None]:
        document = None if holder is None else self._documents.get(holder.document_id)
        item = None if document is None else document.objects.get(holder)
        held = None if item is None or not self.game else motion_component(document, item, self.game)
        if held is None:
            return (), {}, (), False, None
        layers, joint_map = held
        if not joint_map and self._read is not None:
            joint_map = next((found for path in self._bank_paths().get(holder, ())
                              if (found := bank_joint_map(self._read, path))), "")
        if joint_map not in self._maps:
            data = self._read(joint_map) if joint_map and self._read is not None else None
            deforms, extras = (False, ()) if data is None else joint_map_extras(data)
            self._maps[joint_map] = ({}, extras, deforms, None) if data is None else (
                joint_mask_groups(data), extras, deforms, joint_map_mirror(data))
        return layers, *self._maps[joint_map]

    def _player(self, holder, at: int, layer: MotionLayer) -> TreeSampler | None:
        if (holder, at) not in self._played:
            item = None if holder is None or layer.motion in (-1, 0xFFFFFFFF) else self.row(
                (holder,), layer.bank, layer.motion)
            tree = item if isinstance(item, MotionTree) else motion_tree(layer.bank, layer.motion) \
                if isinstance(item, Motion) else None
            self._played[holder, at] = None if tree is None else TreeSampler(
                tree, partial(self.motion, (holder,)), {guid: value for guid, _name, value in tree_variables(
                    self._read, tree)}, self._property_joints, partial(self.mirrored, (holder,)),
                partial(self.reversed, (holder,)), partial(self.masked, (holder,)))
        return self._played[holder, at]

    def _row(self, holders: tuple, bank: int, motion_id: int) -> tuple | None:
        for listed, path in self.bank_lists(holders):
            if listed != bank:
                continue
            if path not in self._lists:
                self._lists[path] = read_list(self._read, path)
            motion_list = self._lists[path]
            entry = None if motion_list is None else next(
                (row for row in motion_list.entries if row.motion_id == motion_id), None)
            item = None if entry is None else entry.item if isinstance(entry.item, MotionTree) else entry_motion(
                self._read, motion_list, entry, self._lists)
            if item is not None:
                return motion_list, entry, item
        return None

    def row(self, holders: tuple, bank: int, motion_id: int) -> Motion | MotionTree | None:
        return None if (found := self._row(holders, bank, motion_id)) is None else found[2]

    def mirrored(self, holders: tuple, bank: int, motion_id: int) -> bool:
        found = self._row(holders, bank, motion_id)
        return found is not None and isinstance(found[2], Motion) and entry_mirrored(self._read, *found[:2],
                                                                                      self._lists)

    def reversed(self, holders: tuple, bank: int, motion_id: int) -> bool:
        found = self._row(holders, bank, motion_id)
        return found is not None and isinstance(found[2], Motion) and entry_reversed(self._read, *found[:2],
                                                                                      self._lists)

    def masked(self, holders: tuple, bank: int, motion_id: int) -> tuple[int, str]:
        found = self._row(holders, bank, motion_id)
        return (0, "ID") if found is None else (found[1].joint_mask, layer_enums(self.game)[1].get(
            found[0].joint_mask_mode, "ID"))

    def motion(self, holders: tuple, bank: int, motion_id: int) -> Motion | None:
        return item if isinstance(item := self.row(holders, bank, motion_id), Motion) else None

    def tree(self, holders: tuple, bank: int, motion_id: int) -> MotionTree | None:
        return item if isinstance(item := self.row(holders, bank, motion_id), MotionTree) else None

    def _posed(self, entry: _Rig, sampler, sample, frame: float, lock_root: bool) -> tuple:
        joints, rig = entry.joints, entry.rig
        written = {} if self._writes is None else self._writes(frame)

        def played(at: int, layer: MotionLayer, player, binding, weights):
            def pose(below):
                laid = None
                if player is not None:
                    picked = player is sampler and not isinstance(player, TreeSampler)
                    laid = joints.layer_pose(sample if picked else player.sample(
                        frame if player is sampler else layer.frame + frame * layer.speed,
                        None if below is None else (joints.hashes, below)), binding, weights)
                    if picked and self._mirrored:
                        joints.mirror(laid)
                if values := written.get((entry.holder, at)):
                    laid = TreePose(len(joints.hashes)) if laid is None else laid
                    rows, slots = joints.bind(keys := tuple(values))
                    laid.values[0][rows] = np.array([values[keys[slot]] for slot in slots.tolist()],
                                                    np.float32).reshape(-1, 3)
                    laid.weights[rows, 0] = 1
                return laid
            return pose

        out = joints.stacked([(layer, played(at, layer, *rest)) for at, (layer, *rest) in enumerate(entry.stack)])
        count = len(rig.hashes)
        posed = tuple(values[:count].copy() for values in out.values)
        if lock_root and rig.levels:
            posed[0][rig.levels[0]] = rig.base[0][rig.levels[0]]
        changed = np.flatnonzero(np.any(posed[0] != rig.base[0], axis=1) | np.any(posed[1] != rig.base[1], axis=1)
                                 | np.any(posed[2] != rig.base[2], axis=1))
        return posed, changed, out

    def attach_materials(self, surfaces_of) -> None:
        for entry in self.rigs:
            constraints = entry.constraints
            if constraints is not None and constraints.materials is None:
                surfaces = next((found for skinned in self.skinned if skinned.object_id == constraints.holder
                                 if (found := surfaces_of(skinned.key))), None)
                if surfaces:
                    constraints.materials = MeshMaterials(surfaces)
        if self._controllers is None:
            self._controllers = material_controllers(self._documents, self.game, self._read) \
                if self.game and self._read is not None else []
        for controller in self._controllers:
            if not self._surfaces.get(controller.holder):
                self._surfaces[controller.holder] = next(
                    (found for skinned in self.skinned if skinned.object_id == controller.holder
                     if (found := surfaces_of(skinned.key))), [])

    def material_values(self) -> dict[object, dict[str, dict[str, float | tuple[float, ...]]]]:
        found = {entry.constraints.holder: entry.constraints.materials.drawn() for entry in self.rigs
                 if entry.constraints is not None and entry.constraints.materials is not None}
        values, deformed, keyed = self._frame
        for controller in self._controllers or ():
            index, surfaces = self.objects.get(controller.holder), self._surfaces.get(controller.holder)
            if index is None or not surfaces:
                continue
            joints = keyed if self.rigs[index].binding is not None else {}
            slots = next((skinned.slots for skinned in self.skinned if skinned.object_id == controller.holder), ())
            for material, variable, value in controller.totals(
                    self._weights.get(controller.holder, {}), slots, {surface.material_name for surface in surfaces}) \
                    if controller.sums else controller.values(deformed.get(index, values), joints):
                key = murmur3_hash_utf16le if controller.hashed else str
                surface = next((surface for surface in surfaces if key(surface.material_name) == material), None)
                parameter = None if surface is None else next(
                    (parameter for parameter in surface.parameters if key(parameter.name) == variable), None)
                if parameter is not None and not parameter.locked and len(parameter.values) == (
                        1 if isinstance(value, float) else 4):
                    found.setdefault(controller.holder, {}).setdefault(surface.material_name, {})[parameter.name] = \
                        value
        return found

    def poses(self, sampler: MotionSampler | None, frame: float, lock_root: bool, everything: bool = False,
              scales: dict | None = None, shapes: dict | None = None):
        """(mesh key, skin matrices 3 x 4 in bone remap order, blend-shape weights by name) per skinned mesh the
        motion moves. By object, ``scales``: the uniform scale game code set on its transform, which its own rigs'
        joints take; ``shapes``: the weights game code set on its mesh's channels by name hash, over the others'."""
        self._scales = {index: scale for target, scale in (scales or {}).items()
                        for index in self._rigs_of.get(target, ())}
        shown = [skinned for skinned in self.skinned if everything or skinned.moves]
        self._last_pose = None
        if not shown:
            return
        sampler = self._joined(sampler)
        at = float(np.float32(sampler.motion.end_frame) - np.float32(frame)) if self._reverse and isinstance(
            sampler, MotionSampler) else frame
        sample = None if sampler is None else sampler.sample(at)
        values = {} if sampler is None else sampler.property_values(at)
        composed = {}

        def compose(index: int):
            if index not in composed:
                composed[index] = self._posed(self.rigs[index], sampler, sample, frame, lock_root)
            return composed[index]
        self._compose = None if sample is None else compose
        for index, entry in enumerate(self.rigs) if sample is not None else ():
            if entry.joints is not None:
                compose(index)
        self._compose = None
        deformed = {}
        for index, entry in enumerate(self.rigs):
            if entry.deforms and index in composed:
                rows, slots = entry.joints.bind(entry.deforms)
                deformed[index] = dict(zip((entry.deforms[slot] for slot in slots.tolist()),
                                           composed[index][2].values[0][rows, 0].tolist()))
        models: list[np.ndarray | None] = [None] * len(self.rigs)
        self._constrain(sample, values, deformed, composed)
        self._last_pose = sample, models, composed
        self._frame = values, deformed, {} if sample is None else {
            value: z for value, z, keyed in zip(sampler.hashes, sample[0][0][:, 2].tolist(), sample[1][0]) if keyed}
        identity = np.eye(3, 4, dtype=np.float32)
        for skinned in shown:
            bones = transform_product(self._models(skinned.rig, sample, models, composed)[skinned.joints],
                                      skinned.inverse_binds)[:, :3]
            bones[~skinned.valid] = identity
            constraints = self.rigs[skinned.rig].constraints
            set_weights = constraints.weights if constraints is not None and constraints.holder == skinned.object_id \
                else {}
            given, shaped = deformed.get(skinned.rig, values), (shapes or {}).get(skinned.object_id, {})
            weights = self._weights[skinned.object_id] = {
                value: shaped.get(value, set_weights.get(value, given.get(value))) for _name, value in skinned.morphs
                if value in given or value in set_weights or value in shaped}
            yield skinned.key, np.ascontiguousarray(bones), tuple(
                (name, weights[value]) for name, value in skinned.morphs if value in weights)

    def constrained(self, model) -> tuple[int, Evaluation] | None:
        return next(((index, evaluation) for index, entry in enumerate(self.rigs) if entry.constraints is not None
                     for evaluation, *_rest in entry.constraints.layers if evaluation.model is model), None)

    def joint_worlds(self, index: int) -> np.ndarray | None:
        """The constrained rig's joint world matrices from the last ``poses`` (4 x 4, column vectors)."""
        constraints = self.rigs[index].constraints
        if self._last_pose is None or constraints is None:
            return None
        return np.matmul(constraints.rig.owner_matrix.T, self._models(index, *self._last_pose))

    def _models(self, index: int, sample, models: list, composed: dict) -> np.ndarray:
        if models[index] is not None:
            return models[index]
        entry = self.rigs[index]
        rig, rows, followed = entry.rig, *entry.follows
        if entry.constraints is None and (not entry.moves or sample is None):
            if entry.rest is None:
                local = rig.base_locals.copy()
                for at, own, other in entry.copies:
                    local[own] = self.rigs[at].rig.base_locals[other]
                entry.rest = rig.models(local, None if entry.owner is None else (
                    rows, self._models(entry.owner, None, models, composed)[followed]))
            models[index], self._locals[index] = entry.rest, rig.base_locals
            return entry.rest
        held = None if entry.owner is None else (rows, self._models(entry.owner, sample, models, composed)[followed])
        if entry.constraints is not None:
            constrained = entry.constraints.rig
            local = rig.locals((constrained.positions, constrained.rotations, constrained.scales),
                               np.arange(len(rig.hashes)))
        elif entry.joints is not None:
            local = rig.locals(*composed[index][:2])
        else:
            local = rig.base_locals
        if entry.copies:
            local = local.copy()
            for at, own, other in entry.copies:
                self._models(at, sample, models, composed)
                local[own] = self._locals[at][other]
        models[index], self._locals[index] = rig.models(local, held), local
        if (scale := self._scales.get(index)) is not None:
            models[index] = models[index] * np.array([scale, scale, scale, 1], np.float32)[:, None]
        if entry.hung is not None and sample is not None:
            parent, row, inverse, world, placed = entry.hung
            joint = transform_product(world, self._models(parent, sample, models, composed)[row])
            moved = transform_product(joint, placed) if isinstance(placed, np.ndarray) else \
                compose_parented_transform(joint, placed, mathex_rotation(joint.T))
            models[index] = transform_product(transform_product(inverse, moved), models[index])
        return models[index]

    def _constrain(self, sample, values: dict, deformed: dict, composed: dict) -> None:
        held: dict[int, tuple[list, Results]] = {}

        def pose(index: int) -> None:
            entry = self.rigs[index]
            if index in held or entry.constraints is None:
                return
            if entry.owner is not None:
                pose(entry.owner)
            constraints, rig = entry.constraints, entry.rig
            constraints.rig.positions[:], constraints.rig.rotations[:], constraints.rig.scales[:] = \
                rig.base if sample is None or entry.joints is None else composed[index][0]
            channels = sorted({value for skinned in self.skinned if skinned.object_id == constraints.holder
                               for _name, value in skinned.morphs})
            given = deformed.get(index, values)
            held[index] = channels, Results(channels, [np.float32(given.get(value, 0)) for value in channels],
                                            None if constraints.materials is None else constraints.materials.table)

        for index in range(len(self.rigs)):
            pose(index)
        for phase in self.phases:
            for index, (_channels, results) in held.items():
                for evaluation, blend_rate, timing in self.rigs[index].constraints.layers:
                    if timing == phase:
                        evaluation.run(0, blend_rate, results)
        for index, (channels, results) in held.items():
            self.rigs[index].constraints.weights = dict(zip(channels, (float(value) for value in results.weights))) \
                if results.weights_set else {}


def _weights(joints: MotionJoints, binding: tuple[np.ndarray, np.ndarray], player, masks: dict | None) -> np.ndarray:
    rows, slots = binding
    if isinstance(player, TreeSampler):
        return np.ones((len(rows), 3), np.float32)
    return player.weights[slots, None] * mask_weights([joints.hashes[row] for row in rows.tolist()], masks)


def _constraint_rig(entry: _Rig, holder, host: ConstraintRig | None) -> ConstraintRig:
    rig = entry.rig
    follows = None
    if host is not None:
        follows = np.full(len(rig.hashes), -1, np.int32)
        follows[entry.follows[0]] = entry.follows[1]
    return ConstraintRig(rig.hashes, rig.parents, *rig.base, *rig.base, holder.document_world_rotation,
                         np.asarray(holder.document_world_matrix, np.float32).T, host, follows, rig.symmetry)


def _morphs(handler) -> tuple[tuple[str, int], ...]:
    blend = getattr(getattr(handler, "mesh", None), "blend_shape_data", None)
    names = {channel.name for body in (blend.bodies if blend is not None else ()) for channel in body.channels
             if channel.name}
    return tuple((name, murmur3_hash_utf16le(name)) for name in sorted(names))
