lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from .native_loop_flow import reachable_blocks, region_postdominators, region_postorder


def spirv_cfg(rows):
    blocks, successors, conditions, merges, current = [], {}, {}, {}, None
    for row in rows:
        if row.opcode == 248:  # OpLabel
            current = row.result
            blocks.append(current)
            successors[current] = set()
        elif row.opcode in {246, 247}:  # OpLoopMerge, OpSelectionMerge
            merges[current] = row.values[0]
        elif row.opcode == 249:  # OpBranch
            successors[current].add(row.values[0])
        elif row.opcode == 250:  # OpBranchConditional
            successors[current].update(row.values[1:3])
            conditions[current] = row.values[0]
        elif row.opcode == 251:  # OpSwitch
            successors[current].update(row.values[1::2])
            conditions[current] = row.values[0]
    return blocks, successors, conditions, merges


class SpirvControlFlow:
    end = -1

    def __init__(self, instructions):
        self._contexts = {}
        self.blocks, current = {}, None
        for row in instructions:
            if row.opcode == 248:
                current = row.result
                if current in self.blocks:
                    raise ShaderGraphError('Vulkan block labels are duplicated.')
                self.blocks[current] = []
            elif current is None:
                raise ShaderGraphError('A Vulkan entry has instructions outside a labeled block.')
            else:
                self.blocks[current].append(row)
        if not self.blocks:
            raise ShaderGraphError('A Vulkan entry has no labeled blocks.')
        self.start = next(iter(self.blocks))
        self.edges, self.conditions, self.switches, self.loops = {self.end: ()}, {}, {}, {}
        self.kills = set()
        structural_merges = set()
        for label, rows in self.blocks.items():
            if not rows:
                raise ShaderGraphError('A Vulkan block has no terminator.')
            for row in rows:
                if row.opcode in {246, 247}:
                    structural_merges.add(row.values[0])
                if row.opcode == 246:
                    self.loops[label] = dict(merge=row.values[0], continuation=row.values[1])
            term = rows[-1]
            if term.opcode == 249:
                self.edges[label] = (term.values[0],)
            elif term.opcode == 250:
                self.conditions[label] = term.values[0]
                self.edges[label] = tuple(term.values[1:3])
            elif term.opcode == 251:
                if len(term.values) < 2 or len(term.values) % 2:
                    raise ShaderGraphError('A Vulkan switch needs complete literal/target pairs.')
                selector, default, *pairs = term.values
                cases = tuple(zip(pairs[::2], pairs[1::2]))
                if len({literal for literal, _ in cases}) != len(cases):
                    raise ShaderGraphError('A Vulkan switch repeats a case literal.')
                targets = tuple(dict.fromkeys(target for _, target in cases if target != default))
                self.edges[label] = (*targets, default)
                self.switches[label] = selector, tuple(tuple(literal for literal, destination in cases if destination == target)
                                                       for target in targets)
            elif term.opcode in {252, 253}:
                self.edges[label] = (self.end,)
                if term.opcode == 252:
                    self.kills.add(label)
            elif term.opcode == 255:
                self.edges[label] = ()
            else:
                raise ShaderGraphError(f'{term.name} needs an explicit Vulkan control-flow adapter.')
        reachable = reachable_blocks(self.start, self.edges)
        if not reachable <= self.edges.keys():
            raise ShaderGraphError('A Vulkan branch targets a missing block.')
        for label in set(self.blocks) - reachable:
            rows = self.blocks[label]
            if label not in structural_merges or len(rows) != 1 or rows[0].opcode != 255:
                raise ShaderGraphError('Unreachable Vulkan instructions need explicit execution validation.')
            # A required structural merge label is not an executable edge.
            del self.edges[label]
        if any(self.blocks[label][-1].opcode == 255 for label in reachable if label in self.blocks):
            raise ShaderGraphError('An executable OpUnreachable path needs explicit undefined execution ownership.')
        self.reachable = reachable
        for header, loop in self.loops.items():
            members, pending = set(), [header]
            while pending:
                label = pending.pop()
                if label == loop['merge'] or label in members:
                    continue
                if label == self.end:
                    raise ShaderGraphError('A Vulkan loop with an early shader return needs ordered exit ownership.')
                members.add(label)
                pending.extend(self.edges[label])
            if loop['continuation'] not in members:
                raise ShaderGraphError('A Vulkan loop continuation is outside its iteration region.')
            if any(target in members and target != header for source in reachable - members for target in self.edges[source]):
                raise ShaderGraphError('A Vulkan loop has an entry bypassing its header.')
            loop['members'] = members
        for loop in self.loops.values():
            for other in self.loops.values():
                a, b = loop['members'], other['members']
                if a & b and not (a <= b or b <= a):
                    raise ShaderGraphError('Overlapping Vulkan loops need explicit execution ownership.')

    def phis(self, label):
        return [r for r in self.blocks.get(label, ()) if r.opcode == 245]

    def context(self, parent=None):
        if parent in self._contexts:
            return self._contexts[parent]
        domain = self.loops[parent]['members'] if parent is not None else self.reachable
        children = {h: r for h, r in self.loops.items() if h != parent and r['members'] < domain}
        owner = {}
        for header, loop in sorted(children.items(), key=lambda item: len(item[1]['members']), reverse=True):
            for member in loop['members']:
                owner.setdefault(member, header)
        visible = {label for label in domain if owner.get(label, label) == label}
        boundary = ('boundary', parent)
        edges = {}
        for label in visible:
            targets = (children[label]['merge'],) if label in children else self.edges[label]
            translated = []
            for target in targets:
                if parent is not None and target in {parent, self.loops[parent]['merge']}:
                    bridge = ('edge', label, target)
                    edges[bridge] = (boundary,)
                    translated.append(bridge)
                else:
                    translated.append(target)
            edges[label] = tuple(translated)
        if parent is not None:
            edges[boundary] = ()
        order = region_postorder(self.start if parent is None else parent, edges,
                                 'A Vulkan cycle bypasses its declared loop boundary.',
                                 'A Vulkan region exits outside its declared boundary.')
        # OpKill paths don't decide the join: ``if (c) kill;`` rejoins at its selection merge, keeping quad lanes
        # converged for derivatives.
        postdominators = region_postdominators(order, edges, self.kills)
        result = {h: children[h] for h in visible if h in children}, edges, postdominators
        self._contexts[parent] = result
        return result
