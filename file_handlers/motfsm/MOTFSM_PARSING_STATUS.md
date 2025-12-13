# MOTFSM 文件解析进度

## 文件结构概览

```
MOTFSM File
├── Header (0x00)
├── BHVT Structure (at treeDataOffset)
│   ├── BHVT Header
│   ├── Nodes[] (at nodeOffset)
│   ├── Actions RSZ Block (at actionOffset)
│   ├── Selectors RSZ Block (at selectorOffset)
│   ├── SelectorCallers RSZ Block (at selectorCallerOffset)
│   ├── Conditions RSZ Block (at conditionsOffset)
│   ├── TransitionEvents RSZ Block (at transitionEventOffset)
│   ├── ExpressionTreeConditions RSZ Block (at expressionTreeConditionsOffset)
│   ├── Static* 系列 RSZ Block (6个)
│   ├── StringPool (at stringOffset)
│   ├── ResourcePaths (at resourcePathsOffset)
│   ├── UserDataPaths (at userDataPathsOffset)
│   ├── Variables (at variableOffset)
│   ├── BaseVariables (at baseVariableOffset)
│   └── ReferencePrefabGameObjects (at referencePrefabGameObjectsOffset)
├── TransitionMapTable (at transitionMapTblOffset)
├── TransitionDataTable (at transitionDataTblOffset)
└── TreeInfo (at treeInfoPtr)
```

---

## 已验证结构

### 1. MOTFSM Header
**状态**: 已验证

| 字段 | 偏移 | 大小 | 说明 |
|------|------|------|------|
| version | 0x00 | 4 | 版本号 |
| magic | 0x04 | 4 | "mfs2" (0x3273666D) |
| padding | 0x08 | 8 | 填充 |
| treeDataOffset | 0x10 | 8 | BHVT结构偏移 |
| transitionMapTblOffset | 0x18 | 8 | TransitionMap表偏移 |
| transitionDataTblOffset | 0x20 | 8 | TransitionData表偏移 |
| treeInfoPtr | 0x28 | 8 | TreeInfo指针 |
| transitionMapCount | 0x30 | 4 | TransitionMap数量 |
| transitionDataCount | 0x34 | 4 | TransitionData数量 |
| startTransitionDataIndex | 0x38 | 4 | 起始TransitionData索引 |

### 2. BHVT Header
**状态**: 已验证

| 字段 | 偏移(相对BHVT起始) | 大小 | 说明 |
|------|---------------------|------|------|
| magic | 0x00 | 4 | "BHVT" (0x54564842) |
| unknown | 0x04 | 4 | 未知 |
| nodeOffset | 0x08 | 8 | Nodes数据偏移 |
| actionOffset | 0x10 | 8 | Actions RSZ块偏移 |
| selectorOffset | 0x18 | 8 | Selectors RSZ块偏移 |
| selectorCallerOffset | 0x20 | 8 | SelectorCallers RSZ块偏移 |
| conditionsOffset | 0x28 | 8 | Conditions RSZ块偏移 |
| transitionEventOffset | 0x30 | 8 | TransitionEvents RSZ块偏移 |
| expressionTreeConditionsOffset | 0x38 | 8 | ExpressionTreeConditions RSZ块偏移 |
| staticActionOffset | 0x40 | 8 | StaticActions RSZ块偏移 |
| staticSelectorCallerOffset | 0x48 | 8 | StaticSelectorCallers RSZ块偏移 |
| staticConditionsOffset | 0x50 | 8 | StaticConditions RSZ块偏移 |
| staticTransitionEventOffset | 0x58 | 8 | StaticTransitionEvents RSZ块偏移 |
| staticExpressionTreeConditionsOffset | 0x60 | 8 | StaticExpressionTreeConditions RSZ块偏移 |
| stringOffset | 0x68 | 8 | StringPool偏移 |
| resourcePathsOffset | 0x70 | 8 | ResourcePaths偏移 |
| userDataPathsOffset | 0x78 | 8 | UserDataPaths偏移 |
| variableOffset | 0x80 | 8 | Variables偏移 |
| baseVariableOffset | 0x88 | 8 | BaseVariables偏移 |
| referencePrefabGameObjectsOffset | 0x90 | 8 | ReferencePrefabGameObjects偏移 |

### 3. BHVTNode 结构
**状态**: 已验证

变长结构，使用交错数组存储。关键字段：
- id_hash, ex_id, name_index, parent, parent_ex
- children[] (交错: 所有IDs, 所有exIDs, 所有indices)
- selector_id, selector_callers[]
- actions[] (交错: 所有IDs, 所有indices)
- priority, node_attribute, work_flags
- FSM特有字段 (当 node_attribute & 0x20)
- states[] (交错数组，6个字段)
- transitions[] (交错数组，4个字段)
- all_states[] (当没有ReferenceTree时)
- reference_tree_index

### 4. Actions RSZ Block
**状态**: 已验证

RSZ结构解析关键发现：
- **Action index = Instance index** (不通过ObjectTable)
- **字段对齐是绝对地址对齐** (不是相对于实例起始)
- **数组元素独立对齐** (每个元素按元素对齐值对齐)
- **UserData实例只占1字节** (skipFileData字段)

测试验证：
- Instance[6642] `snow.player.fsm.PlayerFsm2Action` @ 0x10B938
- Instance[6643] `snow.PlayerPlayMotion2` @ 0x10B94A (BankID=100, MotionID=615, Speed=1.0)
- Instance[6644] `snow.player.fsm.PlayerFsm2ActionEscape` @ 0x10B97C
- Instance[8842-8845] 也已验证通过

### 5. Selectors RSZ Block
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x120C10 |
| Object Count | 684 |
| Instance Count | 685 |
| UserData Count | 0 |

主要类型: `via.behaviortree.SelectorFSM` (2字节: LateSelect + Illegal)

### 6. SelectorCallers RSZ Block
**状态**: 已验证 (空块)

| 属性 | 值 |
|------|------|
| 位置 | 0x1231C0 |
| Object Count | 0 |
| Instance Count | 1 (仅NULL) |

### 7. Conditions RSZ Block
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x123200 |
| Object Count | 7165 |
| Instance Count | 7166 |
| UserData Count | 0 |

主要类型:
- `snow.player.fsm.PlayerFsm2Command` (72字节)
- `snow.player.fsm.PlayerFsm2ConditionItemUse`

### 8. TransitionEvents RSZ Block
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x1A0300 |
| Object Count | 3807 |
| Instance Count | 3808 |
| UserData Count | 0 |

事件类型示例:
- `snow.player.fsm.PlayerFsm2EventOtomoCommunication` (5字节)
- `snow.player.fsm.PlayerFsm2EventStartBatto` (8字节)
- `snow.player.fsm.PlayerFsm2EventStateInitOption` (27字节)

### 9. ExpressionTreeConditions RSZ Block
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x1BA5D0 |
| Object Count | 41 |
| Instance Count | 42 |
| UserData Count | 0 |

条件类型示例:
- `snow.player.fsm.PlayerFsm2ConditionStick` (49字节)
- `snow.player.fsm.PlayerFsm2ConditionCountTimer` (24字节)

### 10. StringPool
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x1D38F0 |
| 大小 | 36574字节 |
| 格式 | 4字节size + UTF-16LE字符串数据 |

示例字符串: "root", "wait", "wp_on", "wp_off", "atk_start", "esc_front_cmn"

### 11. TransitionMapTable
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x1E9A10 |
| 条目数 | 11347 |
| 条目大小 | 8字节 |

### 12. TransitionDataTable
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x1FFCB0 |
| 条目数 | 1979 |
| 起始索引 | 0 |

### 13. TreeInfo
**状态**: 已验证

| 属性 | 值 |
|------|------|
| 位置 | 0x1E9A00 |
| TreeDataSize | 2005440字节 (0x1E99C0) |

### 14. Variables & BaseVariables
**状态**: 已验证

- Variables: 3个变量 @ 0x1E5C22
- BaseVariables: 7个变量 @ 0x1E639C
- ReferencePrefabGameObjects: 空 (count=0)

---

## 验证完成总结

### RSZ Blocks - 全部完成
- [x] Selectors RSZ Block (684对象)
- [x] SelectorCallers RSZ Block (空块)
- [x] Conditions RSZ Block (7165对象)
- [x] TransitionEvents RSZ Block (3807对象)
- [x] ExpressionTreeConditions RSZ Block (41对象)

### Static RSZ Blocks - 全部完成
- [x] StaticActions RSZ Block (61对象)
- [x] StaticSelectorCallers RSZ Block (空块)
- [x] StaticConditions RSZ Block (2656对象)
- [x] StaticTransitionEvents RSZ Block (空块)
- [x] StaticExpressionTreeConditions RSZ Block (7对象)

### 其他结构 - 全部完成
- [x] StringPool (36574字节)
- [x] Variables (3个) / BaseVariables (7个)
- [x] TransitionMapTable (11347条目)
- [x] TransitionDataTable (1979条目)
- [x] TreeInfo

---

## RSZ 解析核心逻辑

### 对齐函数 (来自 RE_RSZ.bt)
```python
def get_aligned_offset(pos, alignment):
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
        if pos % alignment != 0:
            return pos + (alignment - (pos % alignment))
        return pos
```

### 实例大小计算
1. 数组: 对齐到4读count，然后每个元素独立对齐
2. 字符串: 对齐到4读count，然后 count*2+2 字节 (UTF-16+null)
3. 普通字段: 按字段对齐值对齐，然后读size字节

### UserData 处理
- RSZUserDataInfo: instanceId(4) + typeId(4) + stringOffset(8) = 16字节
- UserData实例在数据流中只占1字节 (skipFileData)

---

## 测试文件

- 路径: `C:\Users\YunWuLian\Desktop\natives\STM\player\Fsm\LongSword\LongSword.motfsm2.43`
- RSZ JSON: `G:\REasy\resources\data\dumps\rszmhrise.json`

---

## 实现完成

所有结构验证完成，已实现完整的MOTFSM解析器：

### 已实现文件

1. **rsz_parser.py** - RSZ块解析器
   - `RSZBlock`: 单个RSZ块的懒加载解析
   - `RSZBlockCollection`: 所有RSZ块的集合管理
   - `RSZInstance`: 解析后的实例数据
   - 验证通过的对齐和字段解析逻辑

2. **motfsm_file.py** - MOTFSM文件解析器
   - Header/BHVT/Nodes解析
   - RSZ块懒加载集成
   - `get_action_instance()`, `get_condition_instance()` 等便捷方法

3. **motfsm_handler.py** - 文件处理器
   - 自动从app settings获取RSZ JSON路径
   - 创建viewer实例

4. **motfsm_viewer.py** - UI查看器
   - 树形结构展示
   - 懒加载节点展开
   - RSZ实例字段显示

### 使用方式

```python
# 读取MOTFSM文件
handler = MotfsmHandler()
handler.motfsm.set_rsz_type_info_path("path/to/rszmhrise.json")
handler.read(file_data)

# 访问节点
node = handler.motfsm.get_node_by_index(0)

# 访问RSZ实例 (通过node.actions[i].index)
action_instance = handler.motfsm.get_action_instance(node.actions[0].index)
print(action_instance.class_name)
for field in action_instance.fields:
    print(f"  {field.name}: {field.value}")
```

### 懒加载机制

- BHVT和Nodes在 `read()` 时解析
- RSZ块在首次访问 `rsz_blocks` 属性时创建
- RSZ块内的实例位置在首次访问实例时计算
- Viewer中的树节点在展开时才加载内容
