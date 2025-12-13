# MOTFSM Technical Challenges and Solutions

This document records all technical challenges encountered during MOTFSM file format implementation and their solutions. This serves as a reference to prevent repeating mistakes after context resets.

---

## 1. Hash-Based Reference Architecture

### Challenge
MOTFSM uses **hash-based references** instead of direct indices for referencing nodes and RSZ instances.

### Details
- BHVT nodes reference child nodes via `id_hash` (uint32), not index
- BHVT nodes reference Actions/Selectors/Conditions via `id_hash` (uint32)
- The `index` field in node children and actions is **NOT reliable for direct lookup**

### Solution
Always search by hash when resolving references:

```python
def _get_node_name_by_hash(self, hash_value: int) -> str:
    """Get node name by hash for display"""
    if hash_value == 0:
        return "<NULL>"
    if self.motfsm.bhvt:
        for idx, node in enumerate(self.motfsm.bhvt.nodes):
            if node.id_hash == hash_value:
                name = node.name if node.name else f"Node_{idx}"
                return f"[{idx}] {name}"
    return f"<hash:0x{hash_value:08X}>"
```

**Key Takeaway:** Never assume `index` fields are valid. Always use hash for lookups.

---

## 2. Action Index Always Zero Problem

### Challenge
Testing revealed that **all action.index values are 0** in MOTFSM files, making direct RSZ instance access impossible.

### Test Evidence
From `test_action_index2.py`:
```python
for j, action in enumerate(node.actions[:5]):
    print(f"  [{j}] hash=0x{action.id_hash:08X}, index={action.index}")
# Output: index=0 for ALL actions
```

### Solution
Search RSZ blocks by matching the action's `id_hash` with the RSZ instance's ID field:

```python
def _load_rsz_instance_by_hash(self, parent, block_name, target_hash):
    """Load RSZ instance by finding it through hash match"""
    block = blocks.get_block(block_name)

    # Search for matching instance by hash
    for i in range(1, block.instance_count):  # Skip index 0 (NULL)
        instance = block.get_instance(i)
        if instance and instance.fields:
            # Check first ID field
            for field in instance.fields:
                if 'ID' in field.name.upper() and not field.is_array:
                    if isinstance(field.value, int) and field.value == target_hash:
                        # Found matching instance
                        return i
```

**Key Takeaway:** Action indices cannot be trusted. Always search RSZ blocks by comparing hash with the instance's ID field value.

---

## 3. RSZ Instance ID Field Matching

### Challenge
RSZ instances are referenced by hash, but the ID field location varies between instance types.

### Details
- Most instances have an ID field (e.g., `v0_ID`, `ID`, `mID`)
- The ID field is usually (but not always) the first field
- Field names are not standardized across instance types

### Solution
Search all fields for ID-like names and match against target hash:

```python
def _get_action_class_name_by_hash(self, action_hash: int) -> str:
    """Get Action RSZ instance class name by matching ID hash"""
    action_block = blocks.actions
    for i in range(1, action_block.instance_count):
        instance = action_block.get_instance(i)
        if instance and instance.fields:
            # Check first field - usually ID or v0_ID
            for field in instance.fields:
                if 'ID' in field.name.upper() and not field.is_array:
                    if isinstance(field.value, int) and field.value == action_hash:
                        return instance.class_name.split(".")[-1]
                    break  # Only check first ID-like field
```

**Key Takeaway:** Look for fields containing 'ID' in their name (case-insensitive) and compare their integer values against the target hash.

---

## 4. Nested Splitter UI Layout

### Challenge
Initial implementation had File Info header and tree in a VBoxLayout, which prevented vertical resizing. User reported: "还是只有左右能拖动..."

### Incorrect Approach
```python
# Wrong: VBoxLayout doesn't support resizing
left_widget = QWidget()
left_layout = QVBoxLayout(left_widget)
left_layout.addWidget(header_group)
left_layout.addWidget(self.tree)
```

### Solution
Use **nested splitters** for full resizability:

```python
# Outer horizontal splitter for left panel and detail panel
splitter = QSplitter(Qt.Horizontal)

# Inner vertical splitter for File Info and tree
left_splitter = QSplitter(Qt.Vertical)
left_splitter.addWidget(header_group)      # File Info
left_splitter.addWidget(self.tree)         # Tree
left_splitter.setSizes([80, 600])          # Initial sizes

splitter.addWidget(left_splitter)          # Left panel
splitter.addWidget(right_widget)           # Detail panel
splitter.setSizes([700, 400])
```

**Key Takeaway:** For resizable multi-panel layouts, use nested `QSplitter` objects, not `QVBoxLayout` or `QHBoxLayout`.

---

## 5. Tree Column Width and Resizability

### Challenge
Initial column widths (300/200) were too narrow. User complained: "你这个name选项卡不能做大点或者可以拖动吗？太小了不方便"

### Solution
- Set wider default widths (500/300)
- Enable user resizing with `QHeaderView.Interactive`
- Don't auto-stretch last column
- Remove layout margins to maximize space
- Adjust splitter ratios to give tree area more space

```python
# Remove margins for maximum space
layout.setContentsMargins(0, 0, 0, 0)

# Wide columns
self.tree.setColumnWidth(0, 500)  # Name column
self.tree.setColumnWidth(1, 300)  # Value column

# Enable resizing
header = self.tree.header()
header.setSectionResizeMode(0, QHeaderView.Interactive)
header.setSectionResizeMode(1, QHeaderView.Interactive)
header.setSectionResizeMode(2, QHeaderView.Interactive)
header.setStretchLastSection(False)

# Give left side (tree) much more space
splitter.setSizes([900, 300])  # 3:1 ratio favoring tree
left_splitter.setSizes([50, 800])  # File info small, tree large
```

**Key Takeaway:** Make tree columns wider by default, enable user resizing with `Interactive` mode, remove margins, and adjust splitter ratios to maximize tree viewing area.

---

## 6. Absolute Field Alignment

### Challenge
RSZ field alignment must be calculated based on **absolute file position**, not relative position within the instance.

### Details
- Each field has an alignment requirement (1, 2, 4, or 8 bytes)
- Alignment is relative to the file's start, not the instance's start
- Incorrect alignment causes field offset mismatches

### Solution
```python
def get_aligned_offset(pos: int, alignment: int) -> int:
    """Calculate aligned offset for field positioning."""
    if alignment <= 1:
        return pos
    elif alignment == 2:
        return pos + (pos % 2)
    elif alignment == 4:
        return (pos + 3) & ~3
    elif alignment == 8:
        return (pos + 7) & ~7
    elif alignment == 16:
        return (pos + 15) & ~15
    else:
        return (pos + alignment - 1) & ~(alignment - 1)
```

**Key Takeaway:** Always use absolute file positions for alignment calculations, not relative offsets.

---

## 7. UserData Instance Size

### Challenge
UserData instances (external data references) only occupy **1 byte** in the data stream, not the full instance size.

### Details
- Regular RSZ instances occupy their full size
- UserData instances have `is_userdata = True`
- UserData instances only use 1 byte (the `skipFileData` flag)

### Solution
```python
def _calc_instance_end(self, instance_index: int) -> int:
    """Calculate end position of an instance"""
    instance = self._get_instance_info(instance_index)

    if instance.is_userdata:
        # UserData instances only occupy 1 byte
        return instance.start_offset + 1

    # For regular instances, traverse all fields
    # ... calculate end by parsing fields ...
```

**Key Takeaway:** Check `is_userdata` flag and handle UserData instances as 1-byte entries.

---

## 8. Lazy Loading Architecture

### Challenge
MOTFSM files can be large. Parsing everything upfront causes slow load times and high memory usage.

### Solution
Implement **three-tier lazy loading**:

1. **Level 1:** Load header and BHVT structure immediately
2. **Level 2:** Parse RSZ block positions when first accessed
3. **Level 3:** Parse individual RSZ instances only when expanded in UI

```python
@property
def rsz_blocks(self) -> Optional[RSZBlockCollection]:
    """Get RSZ block collection (lazy loaded)"""
    if self._rsz_blocks is not None:
        return self._rsz_blocks
    # ... load RSZ blocks on first access ...
    return self._rsz_blocks

def get_instance(self, index: int) -> Optional[RSZInstance]:
    """Get RSZ instance (cached after first access)"""
    if index in self._instance_cache:
        return self._instance_cache[index]
    # ... parse instance on first access ...
    self._instance_cache[index] = instance
    return instance
```

### UI Integration
Use `QTreeWidgetItem` placeholders and expand events:

```python
# Add placeholder for lazy loading
QTreeWidgetItem(item, ["Loading...", "", ""])

def _on_item_expanded(self, item: QTreeWidgetItem):
    """Handle lazy loading when item is expanded"""
    # Remove placeholder
    while item.childCount() > 0:
        item.takeChild(0)
    # Load actual data
    self._load_node_details(item, node_index)
```

**Key Takeaway:** Use lazy loading with caching to optimize performance. Load only what's needed, when it's needed.

---

## 9. Double-Click Navigation for Child Nodes

### Challenge
User requested: "双击child跳转到对应节点"

### Solution
Store reference metadata in tree items and handle double-click events:

```python
# Store metadata in tree item
child_item.setData(0, Qt.UserRole, {
    "type": "child_ref",
    "target_hash": child.id_hash
})

def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
    """Handle double-click - navigate to referenced node"""
    data = item.data(0, Qt.UserRole)
    if data and data.get("type") == "child_ref":
        target_hash = data.get("target_hash")
        self._find_and_expand_node_by_hash(target_hash)

def _find_and_expand_node_by_hash(self, hash_value: int):
    """Find node by hash and expand it in the tree"""
    # 1. Find node index by hash
    # 2. Navigate to BHVT → Nodes in tree
    # 3. Expand Nodes if collapsed
    # 4. Find and scroll to target node
    # 5. Select and expand target node
```

**Key Takeaway:** Use `Qt.UserRole` to store navigation metadata in tree items. Handle double-click to navigate between related nodes.

---

## 10. RSZ Type Info Loading

### Challenge
RSZ parsing requires type information (field types, sizes, alignments) from external JSON files.

### Solution
Implement multiple fallback paths for finding RSZ JSON files:

```python
def _load_rsz_type_info(self) -> dict:
    """Load RSZ type info with multiple fallback paths"""
    rsz_json_path = self.handler.rsz_json_path  # From app settings

    # Try multiple locations
    search_paths = [
        rsz_json_path,
        rsz_json_path / "rsz.json",
        Path(__file__).parent / "rsz.json",
        Path(__file__).parent.parent.parent / "rsz" / "rsz.json",
    ]

    for path in search_paths:
        if path and path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)

    # Fallback to empty dict
    return {}
```

**Key Takeaway:** Always provide fallback paths for external dependencies. Don't assume files are in one specific location.

---

## Summary of Common Mistakes to Avoid

1. ❌ **Never use `action.index` or `child.index` for direct array access** → Use hash-based search
2. ❌ **Never use relative offsets for field alignment** → Use absolute file positions
3. ❌ **Never parse all RSZ instances upfront** → Implement lazy loading
4. ❌ **Never use VBoxLayout for resizable panels** → Use QSplitter
5. ❌ **Never assume UserData instances have full size** → They only occupy 1 byte
6. ❌ **Never hardcode RSZ JSON path** → Provide multiple fallback paths
7. ❌ **Never make tree columns non-resizable** → Always set Interactive mode

---

## Quick Reference: Hash vs Index

| Data Type | Reference Method | Why |
|-----------|-----------------|-----|
| Child Nodes | `id_hash` → search nodes | `child.index` unreliable |
| Actions | `id_hash` → search RSZ by ID field | `action.index` always 0 |
| Selectors | `id_hash` → search RSZ by ID field | Same as Actions |
| Conditions | `id_hash` → search RSZ by ID field | Same as Actions |
| State Nodes | `state_id` (int32 index) | This is an actual index, not hash |

**Golden Rule:** If it's called `id_hash` or `*_hash`, search by hash. If it's called `*_id` or `*_index` in state contexts, it might be a real index.

---

## 11. Application Window Layout Space Distribution

### Challenge
User reported: "这行文字占了半个屏幕！上面的可浏览区域明明更重要，为什么这样布局？"

The main application window had a layout issue where the debug console widget at the bottom was taking up too much vertical space, leaving insufficient space for the file viewer/notebook area.

### Problem Analysis
Main window layout structure:
1. **Notebook (tabs)** - Should occupy most space
2. **Status bar** - Fixed 20px height at bottom
3. **Debug console** - Was set to max 100px but no size policy

Without proper size policies and stretch factors, the layout system was distributing space incorrectly, with the console widget expanding to fill available space instead of the notebook.

### Solution
Set explicit size policies and stretch factors:

```python
# Notebook gets most space
self.notebook.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
main_layout.addWidget(self.notebook, 1)  # Stretch factor 1

# Console has fixed height
self.console_widget.setMaximumHeight(100)
self.console_widget.setMinimumHeight(20)
self.console_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
main_layout.addWidget(self.console_widget, 0)  # Stretch factor 0
```

**Key Takeaway:** In QVBoxLayout, use stretch factors to control space distribution. Widgets that should take most space get stretch factor > 0, fixed-size widgets get stretch factor = 0. Also set appropriate size policies (Fixed vs Expanding).

---

## 12. State Structure Field Meanings

### Challenge
User reported: "state列表仍然不能读取目标节点。这是因为你错误理解了state的结构！"

Initial incorrect understanding was that `mStates` contained target node references, when it actually contains TransitionEvent indices.

### Correct State Structure

State structure (using interleaved storage):

1. **mStates** (variable length: count + count*4 bytes)
   - Type: BHVTId (count + array of indices)
   - Contains: **TransitionEvent indices** (NOT node references!)
   - Each value is an index to the TransitionEvents RSZ block

2. **mTransitions** (4 bytes)
   - Type: uint32 **hash** (NOT index!)
   - Contains: **Target node hash** (like child nodes)
   - Must be resolved using `_get_node_name_by_hash()`

3. **TransitionConditions** (4 bytes)
   - Type: int32 index
   - Contains: Index to Conditions RSZ block

4. **TransitionMaps** (4 bytes)
   - Type: uint32
   - Purpose: Unknown, not parsed yet

5. **mTransitionAttributes** (4 bytes)
   - Type: uint32
   - Purpose: Flags or attributes

6. **mStatesEx** (4 bytes)
   - Type: uint32
   - Purpose: Extended state info

### Solution

```python
# WRONG: Treating mStates values as node indices
state_node_name = self._get_node_name_by_index(state_id)

# CORRECT: mStates contains TransitionEvent indices
QTreeWidgetItem(mstates_item, [f"[{j}]", f"TransitionEvent[{event_idx}]", "int32"])

# WRONG: Displaying mTransitions as raw value
QTreeWidgetItem(state_item, ["mTransitions", str(state.mTransitions), "uint32"])

# CORRECT: mTransitions is a node hash, resolve it
target_node_name = self._get_node_name_by_hash(state.mTransitions)
QTreeWidgetItem(state_item, ["mTransitions (Target Node)", target_node_name, "uint32"])
```

### Key Structure Differences

| Field | Type | Contains | Resolution Method |
|-------|------|----------|-------------------|
| mStates.values | int32[] | TransitionEvent indices | Direct display as `TransitionEvent[idx]` |
| mTransitions | uint32 | Target node **hash** | Search nodes by hash |
| TransitionConditions | int32 | Condition index | Direct index (when >= 0) |

**Key Takeaway:** State's `mStates` contains TransitionEvent indices (RSZ references), while `mTransitions` contains the target node hash. Don't confuse which field references nodes vs RSZ structures.

---

## 13. Transition Structure Field Meanings

### Challenge
Transitions have a similar structure to States and were initially misunderstood. The target node reference and condition reference were not correctly identified.

### Correct Transition Structure

Transition structure (using interleaved storage):

1. **mStartTransitionEvent** (variable length: count + count*4 bytes)
   - Type: BHVTId (count + array of indices)
   - Contains: **TransitionEvent indices** (NOT node references!)
   - Each value is an index to the TransitionEvents RSZ block

2. **mStartState** (4 bytes)
   - Type: uint32 **hash** (NOT index!)
   - Contains: **Target node hash** (like child nodes and state mTransitions)
   - Must be resolved using `_get_node_name_by_hash()`

3. **mStartStateTransition** (4 bytes)
   - Type: int32
   - Contains: **Condition hash/index**
   - Value -1 means "no condition" (unconditional transition)
   - Otherwise refers to Conditions RSZ block

4. **mStartStateEx** (4 bytes)
   - Type: uint32
   - Purpose: Extended transition info, display as-is

### Solution

```python
# WRONG: Treating mStartState as node index
start_state_name = self._get_node_name_by_index(trans.mStartState)

# CORRECT: mStartState is a node hash, resolve it
target_node_name = self._get_node_name_by_hash(trans.mStartState)

# Display transition with target node name
t_item = QTreeWidgetItem(trans_item, [f"[{i}] → {target_node_name}", "", "Transition"])

# CORRECT: mStartTransitionEvent contains TransitionEvent indices
for j, event_idx in enumerate(trans.mStartTransitionEvent.values):
    QTreeWidgetItem(mevents_item, [f"[{j}]", f"TransitionEvent[{event_idx}]", "int32"])

# CORRECT: mStartStateTransition is condition, -1 means unconditional
if trans.mStartStateTransition == -1:
    condition_text = "None (unconditional)"
else:
    condition_text = f"Condition[{trans.mStartStateTransition}]"
QTreeWidgetItem(t_item, ["mStartStateTransition (Condition)", condition_text, "int32"])
```

### Key Structure Similarities with State

| Field | State Equivalent | Type | Contains | Resolution Method |
|-------|-----------------|------|----------|-------------------|
| mStartTransitionEvent | mStates | BHVTId | TransitionEvent indices | Direct display as `TransitionEvent[idx]` |
| mStartState | mTransitions | uint32 hash | Target node **hash** | Search nodes by hash |
| mStartStateTransition | TransitionConditions | int32 | Condition hash/index | Display as `Condition[idx]` or "None" if -1 |
| mStartStateEx | mStatesEx | uint32 | Extended info | Display as-is |

**Key Takeaway:** Transitions follow the same pattern as States: first field is TransitionEvent indices, second field is target node hash (not index!), third field is condition (-1 means unconditional).

---

## 14. Expanding TransitionEvent and Condition RSZ References

### Challenge
User requested: "我现在希望state和transition中（它们的结构很像）的：transitionEvent和condition可以展开到RSZ结构！就像你对action处理的那样，但是这两个字段是通过index来解析的！"

TransitionEvent and Condition fields in States and Transitions needed to be expandable to view the corresponding RSZ instance details. Unlike Actions which use hash-based references, these use direct index-based references.

### Details

**In States:**
- `mStates.values[]` - Each value is a TransitionEvent **index** (not hash)
- `TransitionConditions` - Condition **index** (not hash), -1 means no condition

**In Transitions:**
- `mStartTransitionEvent.values[]` - Each value is a TransitionEvent **index**
- `mStartStateTransition` - Condition **index**, -1 means no condition

### Solution

Make each TransitionEvent and Condition entry expandable with lazy loading:

```python
# For TransitionEvent in State
for j, event_idx in enumerate(state.mStates.values):
    event_item = QTreeWidgetItem(mstates_item, [
        f"[{j}] TransitionEvent[{event_idx}]",
        "(expand to load)",
        "int32"
    ])
    # Add lazy loading for RSZ instance
    event_item.setData(0, Qt.UserRole, {
        "type": "rsz_instance",
        "block_name": "transition_events",
        "instance_index": event_idx
    })
    QTreeWidgetItem(event_item, ["Loading...", "", ""])

# For Condition in State
if state.TransitionConditions >= 0:
    cond_item = QTreeWidgetItem(parent, [
        f"TransitionConditions: Condition[{state.TransitionConditions}]",
        "(expand to load)",
        "int32"
    ])
    cond_item.setData(0, Qt.UserRole, {
        "type": "rsz_instance",
        "block_name": "conditions",
        "instance_index": state.TransitionConditions
    })
    QTreeWidgetItem(cond_item, ["Loading...", "", ""])
```

### Key Differences from Action References

| Feature | Actions | TransitionEvents/Conditions |
|---------|---------|---------------------------|
| Reference Type | Hash (uint32) | Index (int32) |
| Resolution Method | Search by matching ID field | Direct array access by index |
| RSZ Instance Type | "rsz_instance_by_hash" | "rsz_instance" |
| Block Names | "actions" | "transition_events", "conditions" |
| Special Values | 0 = NULL | -1 = None (for conditions) |

**Key Takeaway:** TransitionEvents and Conditions use direct index-based references to RSZ instances, unlike Actions which use hash-based references. Use "rsz_instance" type with direct index for lazy loading.

### Critical Detail: RSZ Instance[0] is NULL

**IMPORTANT:** When accessing RSZ instances by index, remember that `instance[0]` is always NULL. Therefore, the actual RSZ array index must be `field_index + 1`.

**Example:**
- State field: `TransitionConditions = 3367`
- Display: "Condition[3367]"
- Actual RSZ access: `conditions.get_instance(3368)` (3367 + 1)

This applies to:
- TransitionEvent indices in States and Transitions
- Condition indices in States and Transitions
- Any other index-based RSZ references

**Does NOT apply to:**
- Hash-based references (Actions, Selectors, etc.) - these search by ID field value, not direct array access
- Node indices in BHVT node array - these are direct indices without offset

```python
# CORRECT: Add 1 for index-based RSZ access
event_item.setData(0, Qt.UserRole, {
    "type": "rsz_instance",
    "block_name": "transition_events",
    "instance_index": event_idx + 1  # +1 because instance[0] is NULL
})

# WRONG: Using raw index
event_item.setData(0, Qt.UserRole, {
    "type": "rsz_instance",
    "block_name": "transition_events",
    "instance_index": event_idx  # ERROR: Will access wrong instance!
})
```

---

*Last Updated: 2025-12-13*
*This document should be updated whenever new technical challenges are discovered and solved.*
