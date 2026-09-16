"""Typed BHVT references, resolved through native identity tables."""


class References:
    def __init__(self, document):
        self.document = document
        self.invalidate()

    def invalidate(self):
        self._nodes = None
        self._node_hashes = None
        self._actions = None

    def node_index(self, id_hash, ex_id):
        if (id_hash & 0xFFFFFFFF) in (0, 0xFFFFFFFF):
            return None
        return self._node_identity(id_hash, ex_id)

    def parent_index(self, node):
        if node.parent & 0xFFFFFFFF == 0xFFFFFFFF:
            return None
        return self._node_identity(node.parent, node.parent_ex)

    def _node_identity(self, id_hash, ex_id):
        if self._nodes is None:
            self._nodes = {}
            for index, node in enumerate(self.document.bhvt.nodes):
                key = (node.id_hash, node.ex_id)
                if key in self._nodes:
                    raise ValueError(f"Duplicate BHVT node identity: {key}")
                self._nodes[key] = index
        key = (id_hash, ex_id)
        if key not in self._nodes:
            raise IndexError(f"Unknown node identity: 0x{id_hash:08X}, ex={ex_id}")
        return self._nodes[key]

    def state_target(self, id_hash):
        # State links carry a hash; mStatesEx remains separate raw metadata.
        if id_hash in (0, 0xFFFFFFFF):
            return None
        if self._node_hashes is None:
            self._node_hashes = {}
            for index, node in enumerate(self.document.bhvt.nodes):
                self._node_hashes.setdefault(node.id_hash, []).append(index)
        matches = self._node_hashes.get(id_hash, [])
        if len(matches) > 1:
            raise ValueError(f"Ambiguous state target hash: 0x{id_hash:08X}")
        if not matches:
            raise IndexError(f"Unknown state target hash: 0x{id_hash:08X}")
        return matches[0]

    def object_instance(self, block_name, raw):
        raw &= 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return None
        tag, reserved, index = raw >> 24, (raw >> 16) & 0xFF, raw & 0xFFFF
        if reserved or tag not in (0, 0x40):
            raise ValueError(f"Unsupported BHVT object reference: 0x{raw:08X}")
        if tag == 0x40:
            block_name = "static_" + block_name
        block = self.document.rsz_blocks.get_block(block_name)
        return block.get_object(index)

    def action(self, id_hash, ex_id):
        if id_hash in (0, 0xFFFFFFFF):
            return None
        target = self.action_identities().get((id_hash, ex_id))
        if target is None:
            raise IndexError(f"Unknown action identity: 0x{id_hash:08X}, ex={ex_id}")
        return self.document.rsz_blocks.get_block(target[0]).get_instance(target[1])

    def action_identities(self):
        if self._actions is None:
            identities = {}
            for block_name, extensions in (
                ("actions", self.document.bhvt.action_ex_ids),
                ("static_actions", self.document.bhvt.static_action_ex_ids),
            ):
                block = self.document.rsz_blocks.get_block(block_name)
                for object_index, instance_index in enumerate(block.object_table):
                    fields = block.file.parsed_elements[instance_index]
                    # via.behaviortree.Action's base ID field in the type dump.
                    action_id = fields["v1_ID"].value
                    key = (action_id, extensions[object_index])
                    if key in identities:
                        raise ValueError(f"Duplicate action identity: {key}")
                    identities[key] = (block_name, instance_index)
            self._actions = identities
        return self._actions
