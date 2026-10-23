# EFX 创作编辑器实施计划

更新日期：2026-10-13。首个完整支持目标：Monster Hunter Rise: Sunbreak EFX 2818730。

## 目标与验收口径

- 在 REasy 内完成 EFX 对象、属性、数组和关联数据的创建、查询、修改、删除、复制及撤销。
- GUI、CLI、项目生成脚本共用操作层，保留原生序列化器和未修改数据。
- 支持自定义 PNG 序列／图集生成 TEX、UVS，并接入 EFX 粒子资源。
- 提供粒子参数、曲线／表达式及游戏外创作预览。编辑覆盖和预览覆盖分别记录；预览不声称与游戏逐像素一致。
- 以结构校验、保存回读、语义比较、原版资源回归和 GUI 操作测试验收。游戏内视觉效果单独记录，不能以序列化成功代替。
- 保留其他格式的版本隔离，不用曙光布局猜测其他游戏。

## 当前状态

曙光 EFX 的结构编辑、序列帧制作、关联资源输出与基础离线预览已落地，离线验收完成。游戏内视觉验证尚未执行；完整 RE Engine 渲染器不在当前预览覆盖内。后续工作从下方未完成项和已知边界继续。

### A. 格式覆盖与序列化

- [x] 初始基线：基础 PAK 5,630 个精确往返，23 个解析失败，339 个路径不在基础 PAK。
- [x] 补齐曙光 EmitMask、RgbCommon：5,651 个精确往返，剩 2 个解析失败。
- [x] PtBehavior 变长数据与嵌套字节数自动更新。
- [x] 补齐 Transform2DModifier（属性 ID 7）：原生文件 9 处实例验证 124 字节固定布局；字段语义仍未完全明确。
- [x] GPU 闪电的三个骨骼槽纳入模型，补齐其字符串长度和 JointName 绑定；原失败文件精确往返。
- [x] 169 种声明属性通过显式构造／读写布局检查；语料出现的 118 种通过原版重建和依赖导入，补齐 PtPathTranslate 数组长度。剩余 51 种未获得本地原版样本，不能认定为游戏实测通过。

### B. 通用 CRUD 与引用图

- [x] 条目／Action／属性／骨骼／分组的基础操作；普通动态数组操作。
- [x] 分组位置及 PtLife ActionIndex 重映射；副本 ID 分配；嵌套骨骼表关联。
- [x] 事务回滚、撤销、重做及保存后的修改状态。
- [x] 表达式参数、字段参数、UVAR 绑定的完整操作与引用维护。
- [x] 结构变体转换及必需字段构造；派生长度维护（曲线／表达式位集合见 E）。
- [x] 跨文件和嵌套作用域复制：导入依赖、解决命名冲突、重映射引用。
- [x] 骨骼／分组／Action／参数／材质纹理引用查询及约束；曲线表、表达式栈、结构长度校验。未明确的 shader 索引／表头由用户显式填写。

### C. 编辑界面与 CLI

- [x] GUI 工具栏／上下文菜单接入基础操作。
- [x] CLI new/set/insert/copy/delete/move/apply/references/batch。
- [x] 枚举、位标志、颜色、向量、资源路径等适配控件。
- [x] 同类原生属性模板、可携带依赖的预设、剪贴板；参数表单支持常用粒子编辑。没有有效模板的未知结构仍需显式 JSON。
- [x] 剪贴板与跨文件导入界面；引用查看／跳转。
- [x] 未支持操作与错误反馈、选择状态、事务回滚／撤销行为完成回归验证。

### D. 自定义序列帧

- [x] 提取现有 UVS 切帧及尾焰图集生成逻辑为通用 API。
- [x] PNG 序列／已有图集导入、帧序、裁切、边距和 mipmap 处理。
- [x] TEX 编码、UVS 生成与播放预览；验证颜色空间和透明度。
- [x] EFX UVSequence 引用联动；序列／帧／纹理槽 CRUD 和位置重映射；拒绝悬空引用与不能表示为连续区间的选择。
- [x] GUI／CLI 成套输出 EFX、UVS、TEX，按需复制可解析依赖，清单记录未解析依赖；不递归解析不透明的 shader／material 内部依赖。
- [x] 32／64 帧自定义素材的完整创作流程自动验证。

### E. 粒子、曲线和表达式

- [x] 发射、寿命、运动、颜色、大小、旋转及附着的便捷编辑。
- [x] 曲线通道／关键帧 CRUD、插值码及切线、位集合联动；材质索引需显式提供。
- [x] 表达式结构／参数 CRUD 和公式编译、栈合法性校验。
- [x] 材质浮点／纹理值、纹理槽重排与变长路径；行为变体结构替换及尺寸联动。
- [x] 原版 Billboard、Ribbon、Mesh、PlayEmitter 等代表类型完成结构／跨文件导入测试；火焰、火星等实际外观仍属游戏内视觉验收。

### F. 游戏外创作预览与交付

- [x] 序列帧、发射区域、时间轴、基础 Billboard 粒子预览。
- [x] 明确各粒子类型的预览覆盖与已知近似。
- [x] 固定随机种子和时间采样，验证播放／暂停／拖动时间一致。
- [x] 最终相关测试、资源审计、界面检查、用法文档。
- [ ] 游戏内视觉验证（尚未执行；资源部署需按当前任务实际范围处理）。

## 2026-10-13 本地验证记录

- 67 项测试通过：EFX 操作层、引用图、材质／曲线／表达式、序列帧／UVS、GUI、CLI，以及共享 CLI 合约和 MHW 查询回归。
- 236 个基础 PAK 原生 EFX 完成发射器复制／删除／撤销的字节恢复验证（抽样 250，14 个不在该 PAK）。
- 15 个斩龙 mod/stage EFX 完成复制／删除／撤销、重建及依赖导入检查，覆盖 25 种属性；未写入生产资源。
- 基础 PAK 最近完整检查：5,653 成功、0 失败、339 未找到（这些路径不在该 PAK 中）。
- 新增 2D modifier 布局和 GPU 闪电多骨骼／字符串长度回归测试。
- 最终全量审计同时启用构造与跨文件导入：5,653 成功，0 失败，118 种原版属性重建与导入通过。
- 32／64 帧 PNG 管线、BC7／RGBA8 解码、sRGB 线性光过滤、透明度、帧间隔离与输出保护通过。
- 新界面已实际渲染检查；Linked UVS 对话框与 CLI 成套输出冒烟检查通过。

## 关键入口与参考

- `file_handlers/efx/efx_file.py`：原生读写。
- `file_handlers/efx/editing.py`：事务与操作层。
- `file_handlers/efx/efx_viewer.py`：界面。
- `file_handlers/efx/sunbreak_schema.py`：曙光补充布局。
- `file_handlers/uvs/`、`file_handlers/tex/`：序列帧和贴图。
- `tools/cli/formats/efx.py`、`tools/audit_efx.py`、`tests/test_efx*.py`。
- 消费项目示例：斩龙项目 `vfx_world/rise_prototype/build_tail_flame.py` 使用公共资源 API 维护专属素材。
- 格式参考：<https://github.com/kagenocookie/RE-Engine-Lib>、<https://github.com/NSACloud/MHR-EFX-Template>。
- 编辑交互参考：<https://github.com/kagenocookie/REE-Content-Editor>、<https://github.com/Dimcirui/MHW-EFX-Editor>（后者为 World 格式）。
- BC7 编码：<https://github.com/K0lb3/ispc_texcomp_py>，依赖 `ispc_texcomp==1.0.1` 已加入 requirements／打包入口。
- 使用入口：运行 `run_reasy.bat`，在 EFX 页选中条目后使用 Parameters／Curves／Expressions／Sequence／Linked UVS／Preview。CLI 示例见 `tools/cli/README.md` 的 EFX 章节。

## 后续接续点

本次开发与离线验收已完成。未启动或部署游戏验证，也未重建独立 EXE；通过仓库源码启动使用新增功能。
新增发现：曙光闪电表达式实际为一个位集合字、29 个赋值槽；已修复原定义字段错位。763 个 TypeMeshClip 实例验证 clipHeader 高位为曲线数×2，最低位保留原值。
预览边界：序列帧播放和基础粒子近似；未实现 RE Engine 原生着色器、复杂粒子行为、骨骼附着与游戏同步渲染。此边界不影响原生字段读写。
继续研究时优先：用已知原生 Billboard 制作一份自定义序列帧测试效果，按斩龙项目 PAK 流程做游戏内视觉验收；收集另外 51 种属性实例；依据实测扩展插值、粒子模拟和 shader 预览，避免把当前基础预览当成 Blender 或游戏渲染器。
