# MHR resource notes

Use the [resource CLI](README.md) for current commands and the migration table.

## Engine conventions these tools rely on (all measured on native files)

| Rule | Value |
| --- | --- |
| MOT 495 payload layout | animation block at `payload+0x80`, then sequences, then the name block, `size` 16-aligned |
| Shared rig | `pointers[0] >= size` means "no skeleton of my own"; the single 001_Loop payload is the file's anchor (`size == 0`) |
| Slot row `+0x0C` | private per-motion data; a new motion must write **0** (inheriting e.g. 0x1A collapses the pose to a T-pose) |
| IK goals | goal world transform == driven bone world transform: `L_Hand_IK←L_Arm_03`, `R_Hand_IK←R_Arm_03`, `L_Foot_IK←L_Leg_02`, `R_Foot_IK←R_Leg_02` (T+R); `LookAt←Head_00` position only, rotation a single identity key |
| Sequence region | Native layout: `table[align_up(count*8,16)]`, then per sequence `wrapper(64) + clip(align 16) + tracks(28*N, align 16)`. MOT wrapper/section pointers use the payload base; slot override wrapper/section pointers are absolute. CLIP pool references use their section base. |
| CLIP key values | scalar types (`U8..U64`, `S8..S64`, `BOOL`, `F32/F64`) are stored **inline** in the key record at `+16`; string-valued keys use `payload` as a section offset |
| BHVT hit action | `snow.player.fsm.PlayerHitAction2._hitIndex` and ChainsawHit `_HitId` select RCOL **field0**; authoring `id` and table position are separate |
| RCOL request set | its own attack params live at `object_table[request table index]`; its shape window is `object_table[shape.user_data_index + request.shape_offset]` |
| RCOL `_HitStartDelay` / `_HitEndDelay` | window start frame and **duration** (window = start .. start+duration) |
| Userdata graph | every userdata instance owns a dedicated `via.physics.UserData` container (1 child each) |
| Sequence replacement | sequence-copy/delete handles any payload and relocates subsequent MOTs, slot tables and absolute override pointers; fixed-width field edits preserve their native offsets |

## Tools

### Effects (`.pfb.17`)

* `tools rsz query` – PFB/SCN/USR structure dump: type histogram, the `EPVStandardData.Elements` table
  (ID, `.efx` path, joint, offset/rotation, variant group) and a regex search over string fields.
* `tools pfb element-add` – add one element by **deep-cloning** an existing one: the element itself plus
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

* `tools clip sequence-copy` – copy whole CLIP sequences (e.g. `SOUND`, `VFX`) from one motion into
  another, retiming property ranges and key frames. Rebases pointers from the model's own
  relocation table and then compares the decoded clip graph with the source.
  `--shift N` moves every copied event by N frames, `--total-frame target` sets the clip duration.
* `tools clip sequence-delete`, `node-copy/delete`, `property-copy/delete`, `key-copy/delete` –
  structural edits with automatic count/index/pointer maintenance. Use `clip dump` for selectors;
  see the [CLI guide](README.md#structural-clip-edits) for scope and donor options.
* `tools clip events-drop` – silence events from a frame on (or exactly `--frames 109,123`) by zeroing
  the inline trigger id. Nothing moves, so the file keeps its size. **Never delete a loop's stop
  event**: skip ids that the culling (footstep/movement) tracks also use.
* `tools clip dump` – decoded CLIP tree per motion (nodes, properties, frames, key values).
* `tools clip effect-set` – retarget a string-valued CLIP key in place (e.g. a `VFXRangeTrack`
  `EffectId`): the CLIP keeps string values in its own pool and a key's `payload` indexes it, so a
  same-length replacement is a few bytes of surgery.  Shared storage is detected and refused.
  Effect IDs are `"<provider>-<elementID>"`: prefix 0 = `epvs-prg` (program/FSM effects, where
  `PlayerFsm2ActionSetEffect._ElementID` also resolves), 50 = `epvs-mot`, 100 = another provider.
* `tools clip layout` – byte layout of a motion's sequence region (wrapper/clip/tracks spans).

### FSM (`.motfsm2.43`)

Use `python -m tools.fsm --help`. See the [FSM CLI guide](../fsm/README.md) for
query/dump, node/Action/Condition/State editing, batch plans, and JSON output.

### RCOL (`.rcol.20`)

* `tools rcol request-add` – add a request set that **reuses an existing 中央/根元/先端 composite group**
  (no new shapes) and copies every attack parameter from a template request set (default
  `気刃斬りフィニッシュ`). Creates the missing `via.physics.UserData` containers and verifies that all
  pre-existing request graphs are unchanged.
* `tools rcol request-edit` – set scalar parameters of one request set
  (`--set _HitStartDelay=40 --set _HitEndDelay=7`), then verify that only those fields moved.

### 给任意攻击节点补"电锯分支"的完整清单（实测，缺一项就无效）

原生 20 个带电锯分支的攻击节点（`atk_axe_slashup_main` / `atk_axe_slashdown_main` /
`atk_axe_rush_strike_main` / `atk_axe_element_slash_1_main(_no_bottle)` / `atk_axe_rush_element_slash_main(_no_bottle)` /
`atk_axe_chain_saucer_end(_by_counter)` / `atk_axe_strike_end(+副本+_by_counter)` / `attack_0` `attack_1` /
`atk_axe_wire_airdash_start/jump/swing`）都是同一套结构。**下面四项缺任何一项，`chainsaw_run` 都不会被引擎选中**
（表现：电锯状态下该招打不出电锯追加判定，但盾牌旋转等 Lua 效果不受影响）：

| 项 | 值 | 说明 |
| --- | --- | --- |
| `children` | `[chainsaw_run, chainsaw_end, normal]` | `chainsaw_end` / `normal` 都是空落地节点，**不能省** |
| `transitions[0]` | `PlayerFsm2ConditionChargeAxeChainsawBuff → chainsaw_run` | 必须插在数组最前（顺序=优先级）|
| `transitions[1]` | 无条件 → `normal` | **默认出口**：没有它，buff 不满足时节点没有出口，分支等于不存在 |
| `selector_id` | 非 -1 且与其它节点不重复 | **最容易漏**：selector 决定"引擎如何在这些 children 里挑一个"；为 -1 时 children 永远选不到 |

`chainsaw_run` 自身只带 1 个 action（`PlayerFsm2ActionChargeAxeChainsawHit`）+ 1 条 state
（`ChargeAxeChainsawAttackEnd → chainsaw_end`）—— motion / hit / 回避派生全部由 BHVT 父子继承而来，
一个都不用复制。`ChainsawHit` 参数：`_Command` = 按键线（0=X / 1=A / 2=锯），
`_HitId` = **rcol request set 的 `field0`**（不是 `id`、不是表位置）。

**特例**：`atk_axe_element_release_main` 系列（AED）**原本就有**一条无条件 transition 和 selector，
所以只需把 `ChainsawBuff → chainsaw_run` 插到第 0 位即可，不必另建 `normal`、也不必补 selector。

工具：`python -m tools.fsm node-chainsaw SRC -o OUTPUT_FILE --node ... --template-node atk_axe_rush_strike_main --command 0 --hit-id 47`
—— 它会自动判断目标是不是纯叶子节点（缺默认出口就补 `normal`）、是不是缺 selector（缺就克隆模板的）。

## Typical workflows

```bash
GAME="C:/Program Files (x86)/Steam/steamapps/common/MonsterHunterRise"

# 1) import a Wilds motion into a Rise hunter motlist (native layout, IK goals, WeaponHold, contract checks)
./.venv/Scripts/python.exe -m tools motion import-wilds <plw_*.motlist.528> \
    --donor <src.motlist.992> \
    --hold-template <plw_*.motlist.528> --motion 293:620 -o out/imported.motlist.528

# 2) give the new FSM node an attack judgment
./.venv/Scripts/python.exe -m tools rcol request-add "$GAME/natives/STM/player/hit/LongSword.rcol.20" -o out/LongSword.rcol.20 --name atk_620 --template-id 32
./.venv/Scripts/python.exe -m tools.fsm hit-request "$GAME/natives/STM/player/Fsm/LongSword/LongSword.motfsm2.43" -o out/LongSword.motfsm2.43 --node atk_620 --request 72

# 3) retime the judgment
./.venv/Scripts/python.exe -m tools rcol request-edit <rcol> -o out/edited.rcol.20 --id 72 --set _HitStartDelay=40 --set _HitEndDelay=7

# 4) give the new motion another move's audio/effect
./.venv/Scripts/python.exe -m tools clip sequence-copy <motlist> -o out/edited.motlist.528 --from 109 --to 620 --shift 0
./.venv/Scripts/python.exe -m tools clip events-drop <motlist> -o out/edited.motlist.528 --motion 620 --frames 109
```

Afterwards copy the candidates over the files under `<GAME>/natives/...` (keep a `.bak-*` copy of
every file you replace) and test in game; MOTLIST/RCOL changes need a game restart, the FSM is
reloaded by the plugin.

### 判定窗口 / damage reflex（实测，2025）

判定窗口动作同形：`_StartFrame` + `_EndFrame`（`DamageReflex` 另有 `_Type`）。

| 动作类 | 用途 | 备注 |
| --- | --- | --- |
| `PlayerFsm2ActionDamageReflex` | 伤害反射窗口，`_Type` = `snow.player.DamageReflexInfo.Type` | 跨武器实测 `_Type`：1 = 大剑格挡反击 `blocking_start`，2 = 弓回避，3 = 片手 `Atk_GuardA`，4 = 乘骑/器械（150 处），**5 = GunLance `root.atk.ワイヤーガード.wireguard.start`（wire guard）**，8 = 斩击斧属性反击，10 = `EquipSkill210` ウツセミ |
| `PlayerFsm2ActionChargeAxeGuardPoint` | 盾斧 GP 窗口 | 剑↔斧变形/滑步/翻滚等 28 处 |
| `PlayerFsm2ActionDamageSetGuardFrame` | wire guard / wire cancel 的格挡窗口 | 盾斧 `atk_wire_anchor_guard*`、`atk_wire_cancel_*` |
| `PlayerFsm2ActionSeeThroughAttack` | 太刀見切り窗口 | `atk_155` 的 `_StartFrame=0,_EndFrame=18` |

**窗口触发后的派生**：不要在窗口节点上等 `PlayerFsm2ConditionDamageReflexSuccess`（原生只用在弓/斩击斧 5 处）；
用「受伤/命中」条件即可跳转：

* GunLance wire guard —— 窗口在子节点，派生写在父节点：
  `root.atk.ワイヤーガード.wireguard`：`PlayerFsm2ConditionQuestBaseDamage -> ワイヤーガード 受付中 ノックバック 小`
* 太刀 `atk_155` —— `成否` 子节点：`PlayerFsm2ConditionQuestBaseDamage -> success`，`always -> faled`

**工具**

* 给既有节点追加 action：`python -m tools.fsm action-add SRC -o OUTPUT_FILE --node 0x59A5C461 --class PlayerFsm2ActionDamageReflex --exact --from 0x… --set _Type=5 --set _EndFrame=16`
* 给既有节点加派生 state：`python -m tools.fsm state-add SRC -o OUTPUT_FILE --node 0x… --target <node> --template-from 0x… --template-state N --events drop`
* **改共享的 transition event**：`event-edit` 遇到多用户事件会拒绝（`Event is shared with [...]; use --allow-shared or clone it first`）。先复制成私有副本再改：`python -m tools.fsm event-copy SRC -o OUTPUT_FILE --node <state 所在节点> --states N --class PlayerFsm2EventStateInitOption --set _LimitAngle=0`（早期克隆工具会共享 `transition_events` 实例 → “我的新节点和原生节点共用同一个事件”是常见历史遗留；`--allow-shared` 会连原生一起改，慎用）
  （`--condition-class` 只认 `PlayerFsm2Command` 系模板，别的条件类要用 `--template-state` 指定下标）
* 查询节点：`python -m tools.fsm query <fsm> [--name/--motion/--class/--derives-to] [--actions] [--states] [--json]`

**坑**

* `v1_ID` 是**每个 RSZ block 各自的命名空间**（`actions` 与 `static_actions` 会撞号）→ 解析模板实例必须用 `references.action_identities()` 给出的 block + 索引，
  否则会静默 clone 出同号的无关动作（曾把 `ChargeAxeSetWeaponMode` 变成 `SubStamina`）。
* CLI 自动补齐节点字符串池的对齐；`--node/--from` 支持 `0x<id_hash>:<ex_id>` 以消除重名。

### 精准防御（前后摇拆分）落地记录

* 判定窗口挂在**前置动作**上：`root.guard.start`（motion 430，16 帧）追加 `PlayerFsm2ActionDamageReflex(_Type=5, 0..16)` =
  wire guard 同类 reflex（GunLance `root.atk.ワイヤーガード.wireguard.start` 用的就是 `_Type=5`；`_Type` 枚举实测：1 大剑格挡反击 / 2 弓回避 / 3 片手 GuardA / 4 器械 / 5 wire guard / 8 斩击斧属性反击 / 10 ウツセミ）。
* 触发后的跳转用 `PlayerFsm2ConditionQuestBaseDamage -> <精防后摇 node>`（同 GunLance wire guard、太刀 `atk_155` 的 `成否`）。
* 改既有 state 的目标：`python -m tools.fsm state-target SRC -o OUTPUT_FILE --node guard_precise_m --state 10 --target atk_wait`
  （`mTransitions` 是定宽字段 → 节点表长度不变、无需重定位；节点表末尾的对齐填充要原样保留）。
* 爽解的 Lua 派生表 `dataTable` 是**按武器类型嵌套**的：`dataTable[wepType][actionId 或 nodeHash]`，并要求 `_action_bank_id == 100`；
  新增动作只要在同级加 `[<动作ID>] = { … }`。Lua 的 `targetCmd` 用的是 `constant.isCmd`（按钮），FSM 里的 `CmdType` 用的是 `constant.commandFsm`，**两套枚举不可直接互抄**。

## 盾斧「带瓶招式 + 电锯」新增配方（FSM / motlist / rcol）

参考实现：**631 追加解放斬**（Wilds motion 527 → Rise bank 100 / ID 631）。一个「带瓶斧攻击 + 电锯」
= **1 个新 motion + 1 个新 RCOL request set + 3~4 个新 FSM 节点**，三条线可独立验收；后面成批加招时照这份
配方走，每步都是 `SOURCE -o OUTPUT_FILE` 的候选文件流（工具不覆盖输入、不自动部署）。

### 0. 命名与 ID

| 对象 | 规则 | 631 实例 |
| --- | --- | --- |
| motion ID | 盾斧 bank `100`，取未占用 ID（`query --motion` 查重名）| `631`（260 帧，来自 Wilds `527`）|
| RCOL request set | `request-add` 分配；有瓶/无瓶**共用一套** | `id = field0 = 66` |
| 招式节点 | `<名>_bot`（有瓶）/ `_nbt`（无瓶）/ `_brn`（瓶子分叉）| `atk_axe_add_release_{bot,nbt,brn}` |
| 节点名长度 | `(len(name)+1) % 8 == 0`（字符串池 16 字节对齐），否则 CLI 断言 | `atk_axe_add_release_bot` |

### 1. motlist（动画，改动需重启游戏）

```bash
py='./.venv/Scripts/python.exe'; M='plw_ChargeAxe_100.motlist.528'
& $py -m tools motion import-wilds $M --donor source.motlist.992 \
      --hold-template $M --motion 527:631 -o out/m1/motlist.528
```

* `--hold-template` 提供原生 `001_Loop` 的 WeaponHold（斧模式 `_leftWp=4` / `_rightWp=1`）；
  工具自动重定向骨骼、烘焙 IK 目标、逐骨骼矩阵 + IK 校验。
* **WeaponHold 的范围必须自己铺满动作全长（实测坑）**：键值**不会**顺延到最后一个键之后，
  从模板抄来的 range 只有模板动作的长度（631 初版只到 114 帧 → 114 帧后武器绑定数据全丢）。补长：

```bash
& $py -m tools clip property-range out/m1/motlist.528 -o out/m2/motlist.528 \
      --motion 631 --node WeaponHold --property _leftWp --end-frame 260    # _rightWp 再来一遍
```

  `property-range` 只伸长不缩短、只改 float 高位字节（**文件长度不变**），并重解析校验。
* 音效/特效轨整段搬家（可选）：`clip sequence-copy --from 131 --to 631 --categories SOUND,VFX --shift 0`
  目标可以是任意物理位置的 payload；可先批量导入，再逐个复制 CLIP。
  增删会自动重定位后续 MOT、slot 表及 override 尾部。
* 相机轨 `PlayerMotionCameraTrack_*` 是"触发/渐变"语义，通常**不必**跟着拉长（按镜头需求决定）。
* **电锯窗口**（`ChargeAxeChainsawRunTrack._Flag`，四键 BOOL）单独调：

```bash
& $py -m tools clip bool-window <motlist> -o out/... --motion 632 \
      --node ChargeAxeChainsawRunTrack --property _Flag --on 10 --off 140
```

  键位布局固定为 `(0,False) (on,True) (off,True) (off+1,False)`，属性 range = `0 .. off+1`
  （原生 128 是 `0/4/52/53` = 第 4~52 帧放电锯）；原地改 float，文件长度不变。

### 2. RCOL（命中判定，需重启游戏）

抄 AED（`斧：高出力属性解放斬り（ビン有）`）的形态，只改窗口：

```bash
& $py -m tools rcol request-add  ChargeAxe.rcol.20 -o out/r1/ChargeAxe.rcol.20 \
      --name atk_axe_add_release --template-id 26
& $py -m tools rcol request-edit out/r1/ChargeAxe.rcol.20 -o out/r2/ChargeAxe.rcol.20 \
      --field0 66 --set _HitStartDelay=55 --set _HitEndDelay=18
```

* `_HitStartDelay` = **起始帧**，`_HitEndDelay` = **持续帧数**（窗口 = start … start+duration）→ 55~73 帧。
* 斧模式碰撞体复用 `group 1「斧」` 的中央/根元/先端复合组（joint `L_Weapon_00`），不新建 shape。
* 其余参数与模板保持一致（`_Priority=127`、`_DamageType=3`、`_Power=30`、`_BaseDamage=90`）。
* **坑**：FSM 的 `PlayerHitAction2._hitIndex` 与 `ChainsawHit._HitId` 指向 RCOL 的 **`field0`**，
  不是 authoring `id`、也不是表内位置（AED: `id 26 → field0 25`）。`request-add` 可用 `--field0` 直接指定。

### 3. FSM（插件热重载即可）

**3a 瓶子分叉节点 `_brn`** —— 照抄 AED 的瓶子分叉（动作集与有瓶节点一致，含 `PlayMotion2`）：

| state | 条件 | 目标 |
| --- | --- | --- |
| 0 | `PlayerFsm2ConditionChargeAxeBottleEmpty{v2_Condition=True}`（瓶空）| `_nbt` |
| 1 | `Fsm2ConditionMotionEnd` | `_bot` |
| 2 | `PlayerFsm2ConditionChargeAxeBottleEmpty{v2_Condition=False}`（有瓶）| `_bot` |

每条 state 都要**自己的 TransitionMaps id**（一 state 一 map，共用会被旧绑定静默覆盖）。

**3b 招式节点 `_bot` / `_nbt`** —— 同一 motion 的两个动作集（`fsm attack-clone` + 逐条 `action-add`）：

| action | `_bot` | `_nbt` |
| --- | --- | --- |
| `PlayerPlayMotion2{v3_BankID=100, v4_MotionID=631}` | ✓ | ✓ |
| `PlayerFsm2ActionChargeAxeRequestBottleAttack{_Type=3, _IsUseBottle=True}` | ✓（扣瓶）| ✗ |
| `PlayerHitAction2{_hitIndex=<RCOL field0>}` | ✓ | ✓ |
| `PlayerFsm2ActionCharegeAxeOverwriteHitForCounterFullChargeToElementEnhance` | ✓ | ✗ |
| `PlayerFsm2ActionAddMotionSequenceFilter` | ✓ | ✓ |
| `PlayerFsm2ActionTeamAtk{_TeamAtkIndex=26}` | ✓ | ✓ |

派生：抄 AED 的通用派生（`StartFrame=105`、`PreFrame=20`；`EndFrame` 原生写 `0`，本实现写 `999`
——`move retime` 把 `EndFrame=0` 视作不设上限，两种写法都见于原生文件）：

| 条件 | 目标 |
| --- | --- |
| `Fsm2ConditionMotionEnd` | `atk_axe_wait`（收招回斧待机，**必备**）|
| `ConditionStick{CmdType=3}` | `atk_axe_run_start`（移动打断）|
| `CmdType=24 / 196 / 37 / 37 / 38 / 38` | `回避のレバー判定` / `utsusemi_Axe_MR` / `atk_axe_wire_advance` / `atk_axe_wire_airdash_MR` / `atk_axe_canceller_MR` / `atk_axe_element_slash_2.replaceC` |
| `CmdType=4`（AtkXA）| `atk_axe_element_release_high`（SAED）|
| `CmdType=8`（AtkR1）| `atk_axe_element_slash_2.replaceB` |
| `CmdType=1`（AtkA）| `atk_axe_element_slash_1` |
| `CmdType=2`（AtkX）| `atk_axe_slashup` 等 |

**3c 电锯分支** —— `node-chainsaw` 一次补齐四项（`children` 含 `normal` + `ChainsawBuff→chainsaw_run` +
无条件→`normal` + 非 -1 `selector_id`；缺一项电锯判定就不触发）：

```bash
& $py -m tools.fsm node-chainsaw ChargeAxe.motfsm2.43 -o out/f1/ChargeAxe.motfsm2.43 \
      --node atk_axe_add_release_bot --template-node atk_axe_rush_strike_main --command 1 --hit-id 43
```

* `_Command` = 按键线（`0`=X 系 / `1`=A 系 / `2`=锯），且 `ChainsawHit._Command` 与
  `ChainsawAttackEnd._Command` 必须**成对相等**（631 A 系：两者都 `1`）；`_HitId` = 电锯判定的 RCOL `field0`。
* `--motion-id` 可以把该节点的 `PlayMotion2.v4_MotionID` 指向**另一个自带电锯动画轨的 slot**
  （见文末「自带电锯动画的招式」）。

**3d 入口**（让既有招式派生进来）：给每个变体加一条 state，例如 131 的 4 个变体
（`atk_axe_element_release_main` / `_no_bottle` / `_heavy` / `_no_bottle_heavy`）各加
`PlayerFsm2CommandChargeAxe{CmdType=1(AtkA), StartFrame=95, PreFrame=10} → atk_axe_add_release_brn`：

```bash
& $py -m tools.fsm state-add ChargeAxe.motfsm2.43 -o out/f2/ChargeAxe.motfsm2.43 \
      --node atk_axe_element_release_main --target atk_axe_add_release_brn \
      --template-from <同形命令 state 的节点> --template-state <n> --events drop
```

**3e 长按减速子机（可选，631 现场有）** —— 在 `_bot/_nbt` 的 `chainsaw_run` / `normal` 下挂
`hold_start → hold_slow → hold_released`：`hold_start` 里
`PlayerFsm2CommandChargeAxe{CmdType=14(AtkAOn), StartFrame=17}` → `hold_slow`（带
`PlayerFsm2ActionChangeMotionSpeed{_MotionSpeed=0.05}` 把动作压到 5% 速度）、
`CmdType=17(AtkAOff)` → `hold_released`（空叶子，交还给父节点的派生）。

### 4. Lua（可选：吃爽解盾斧精华版加成）

* 新动作 ID 加进 `is_guard` / `gp_current_actions` / `gp_pre_actions` / `dataTable[wepType][<ID>]` 等表，
  即可吃到倍率、转向、派生（`430` 因带 damage reflex 已不再走普通格挡，故同时进 `gp_pre_actions`）。
* `dataTable` 按武器类型嵌套且要求 `_action_bank_id == 100`；`targetCmd` 用 `constant.isCmd`，
  FSM `CmdType` 用 `constant.commandFsm`，两套枚举不可互抄。

### 5. 验收

```bash
& $py -m tools move check move.json --json
```

`move check` 覆盖动作/bank 引用、state map 独占、`field0` 命中引用、CLIP 时序、派生窗口；窗口起点超出
动画长度会报 warning。它**不**推断运行时继承分支，也不证明游戏内行为——最终以游戏内测试为准。

**部署 / 同步口径**：每轮开工前先核对「游戏内 ↔ 项目副本（`natives/`、`lua/`）」的 md5 —— **游戏内文件由用户
手动更新，游戏版视为最新版**；不一致时以游戏为准同步回项目副本（旧版先留 `_deprecated/*.bak-before-gamesync`），
再做编辑。FSM 插件热重载即可生效；motlist / rcol / Lua 需重启游戏。

### 自带电锯动画的招式（后续方向）

如果 donor motion 的 CLIP 里**已经含有电锯/盾牌旋转动画**（Wilds 侧本来就是电锯状态的招），那么
`motion import-wilds` / `motion duplicate` 拷过来即自带，**Lua 侧不必再驱动 `playChainsawLoopMotion`**
（盾牌旋转交给动画轨道）。配套做法：

* 载体是 CLIP **EXTRA_0** 里的 `snow.player.PlayerWeaponCtrlCA_Shield.ChargeAxeChainsawRunTrack`，
  单条 BOOL 属性 `_Flag`（四键：`0=False, 4=True, 52=True, 53=False` = 第 4~52 帧放电锯）。
  实测**原生 motion 128（`atk_axe_element_slash_1`）的 EXTRA_0 就带这条轨**，所以
  `clip sequence-copy --from 128 --to <新ID> --categories SOUND,VFX,EXTRA_0 --replace --total-frame target`
  一次就把电锯轨带过去（顺便带上 SOUND/VFX）。
* 每招的电锯窗口用 `clip bool-window --motion <ID> --node ChargeAxeChainsawRunTrack --property _Flag --on <起> --off <止>` 单独调。
* 想只加这条轨、保留目标原有的 EXTRA_0，用 `clip track-add --motion <新ID> --template-motion 128
  --track ChargeAxeChainsawRunTrack --flag _Flag --true-frame 4 --false-frame 53`。
* 复制一个自带电锯轨的 slot，再用 `fsm node-chainsaw --motion-id <新ID>` 把招式节点指过去；
* FSM 侧的**电锯分支仍然必需**（否则电锯追加判定 `ChainsawHit` 不触发）；
* Lua 只剩命中/特效等表，不再需要"按动作 ID + 帧窗口驱动盾牌旋转"那一整套。
