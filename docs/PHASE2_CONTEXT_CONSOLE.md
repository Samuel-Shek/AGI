# PAM Phase 2：个人上下文控制台源码架构

本文定义 PAM Phase 2 源码候选的持久架构、契约、迁移和回滚边界。它以
Phase 0/1 的 schema 30 Context 图为基础，目标是用“今天、找回、记忆”
三个用户入口提供可信、有来源、可撤销的本地上下文。

本文描述的是源码设计，不是安装、live Core、真实数据库迁移、Hermes/飞书
送达或产品指标验收结果。

## 1. 不可破坏的架构边界

Phase 2 继续遵守以下信任分层：

```text
Observation -> Evidence -> ContextCandidate -> VerifiedContext
                                                |
                       ContextBundle / LocalHandoff / MemoryCandidate
```

- 屏幕、OCR、应用/窗口和其他活动信号只能产生本地线索，不能单独证明
  “任务已完成”。
- 只有明确的本人确认，或通过既有受治理校验的独立可靠回执，才能产生
  `VerifiedContext`。
- 未回答、忽略、延后、否认、误识别、过期及
  `information_insufficient` 均不得生成 `VerifiedContext`。
- `verified_contexts`、旧确认兼容投影和 `unverified_clues` 是三个不同
  分区；界面和续接包不得把它们合并成同一可信列表。
- 原始截图、OCR、聊天正文、prompt、工具参数、diff、文件路径、原始
  receipt 和工程诊断字段不得进入公共投影。
- PAM 只产生 `MemoryCandidateV1`，不直接写长期记忆。

## 2. schema 31 的增量范围

schema 31 相对已存在的 schema 30 **只新增两个领域表**。伴随表建立的唯一
约束和查询索引属于这两个表的结构，不代表引入第三个领域表。

### 2.1 `context_activity_confirmation_links`

该表连接旧活动确认记录与 Context 图，保存一次本人操作的私有关联：

- 旧确认引用、类型、私有 key 和 revision；
- 幂等键及规范化 payload hash；
- Observation、Evidence、ContextCandidate、VerifiedContext 的内部引用；
- 对外可用的派生 `public_context_ref`；
- active、revoked、expired 等生命周期状态和撤回原因。

它解决两个问题：

1. 本人正向确认和“记一下”可在同一事务内写入旧确认记录和受控 Context
   图，避免一边成功、一边缺失。
2. 重答、删除、来源撤回和过期可沿显式链接定位依赖对象并执行级联，而不是
   通过正文或模糊时间匹配猜测关系。

同一确认只能建立一个链接；同一幂等键重复提交相同 payload 是安全重放，
不同 payload 必须冲突并 fail closed。内部 `verified_ref`、数据库主键和
私有确认 key 不得透出 Core 公共 JSON 或 Swift 模型。

### 2.2 `context_product_feedback`

该表把一次“这次有用/没用”反馈绑定到真实 `ContextBundleV1` 或本地
`LocalHandoffV1`：

- `source_kind` 只能标识 Bundle 或本地续接包；
- `source_ref` 必须能解析到仍有效、项目一致的真实 frozen Bundle；
- 每个 `source_ref` 全局只能反馈一次，不能通过更换 `source_kind` 重复记账；
- 相同幂等键和 payload 可重放，不同内容必须冲突；
- 正反馈可以生成不含正文的产品胜绩；
- 存在活跃实验时，负反馈进入真实实验分母，但不能生成胜绩；
- 反馈只保存来源绑定、布尔结果、可选续接耗时和内容无关的实验引用，不保存
  搜索正文或续接包正文。

### 2.3 迁移原则

- 不重写、回填或重新分类历史活动数据。
- schema 30 已有 Context 图、FTS、Bundle、MemoryCandidate 和 Delivery
  数据模型保持原语义。
- schema 初始化必须保持以下治理门禁关闭：

  ```text
  ha_auto_package_enabled=false
  context_memory_writer_enabled=false
  context_source_revocation_cascade_ready=false
  ```

- schema 31 数据库不得交给只支持更低 schema 的旧 Core 继续写入；旧 Core
  应拒绝降级。Phase 2 所说的“回滚”是同一新版 Core 上的界面功能开关回滚，
  不是把数据库版本改回 30。

## 3. 旧确认兼容与新双写路径

### 3.1 只读兼容投影

历史 `activity_confirmation_items` 中仍有效且已明确回答 `occurred` 的记录，
可通过只读适配器进入 `ContextSnapshotV1` 的
`projects[].compatibility_contexts` 分区，参与“今天”和“找回”的本地展示。
该适配器：

- 使用经过旧隐私防火墙处理的安全摘要；
- 在候选数量限制与本地 FTS 之前排除所有已经由
  `context_activity_confirmation_links` 接管的记录；verified、non-verifying、
  revoked 和 expired link 均作为永久防重、防撤回复现的墓碑；
- 派生公共项目引用，不暴露旧 item/group key；
- 不创建新的 Observation、Evidence、ContextCandidate 或
  `VerifiedContext`；
- 不自动进入可信 `ContextBundleV1` 正文、外部投递或长期记忆；
- 随旧活动保留期自然退出，不延长历史活动数据寿命。

### 3.2 新的本人操作

`OwnerContextActionV1` 支持两类动作：

- `confirm_card`：对当前确认卡按 revision 回答；
- `manual_note`：菜单栏或控制台中的“记一下”文字补记。

两类动作都必须带幂等键、精确契约版本和
`CONFIRM_OWNER_CONTEXT_ACTION` 本人确认标记。Core 是项目、主体、隐私等级、
状态和 Context 内部引用的权威派生方；Swift 不得自行提交或伪造这些字段。

只有 `occurred` 和明确本人提交的 `manual_note` 走正向验证路径。其他回答只
更新旧确认状态和链接生命周期，不得生成可信 Context。双写失败必须整体
回滚，不能留下孤立的旧确认或 Context 节点。

## 4. Phase 2 六个新契约

以下契约是 Phase 2 面向 macOS 新壳的公共边界。所有契约使用封闭字段集、
精确 `contract_version`、有界 JSON，并固定 `raw_access=false`。

| 契约 | 用途 | 必须保持的约束 |
|---|---|---|
| `ContextSnapshotV1` | “今天”和“找回”的统一快照 | 同一次读取返回状态、计数、项目分组、确认问题和嵌套 `ContextBundleV1`；busy、missing、unavailable 或禁用时，可信内容、计数和 capabilities 必须为 `null`，且 `actions_enabled=false` |
| `OwnerContextActionV1` | 本人确认与“记一下” | 只通过 JSON stdin；必须有幂等键、版本、本人确认和乐观 revision；Core 原子双写旧确认与 Context 图；普通补记默认 `memory_eligible=false` |
| `MemoryCandidateListV1` | “记忆”列表 | 安全摘要优先，只列仍有效且可追溯的候选；状态区分待复核、继续有效、历史、过期、错误主体隔离和已撤回；只读审查，不提供直接写入动作 |
| `DeliveryStatusV1` | 日报和投递状态 | 分别表达待补充、prepared、consumed、delivered、failed；没有 provider receipt 不得显示 delivered；撤回/受限状态不能伪装成当前有效送达 |
| `LocalHandoffV1` | 本地续接包 | 必须指定项目，只冻结 verified-only Bundle；目标、结果、阻塞、明确下一步、来源、未决问题、有效期和撤回状态分区表达；不得自动外发 |
| `ProductFeedbackV1` | “这次有用”反馈 | 必须绑定真实 Bundle/续接包且每个来源一次；正反馈才产生胜绩，负反馈只进入分母；不保存正文 |

Phase 0/1 已有的 `ContextReceiptV1`、`ContextBundleV1`、
`MemoryCandidateV1` 和 `DeliveryReceiptV1` 继续保持既有信任和撤回语义。
`report-context` contract v2 暂时保留为 `ContextBundleV1` 的兼容适配出口，
本阶段不移除旧消费者，也不增加第二条自动外发链。

## 5. stdin、搜索和公共投影

### 5.1 进程边界

Swift 通过可注入的 `CoreCommandExecuting` 和
`ContextConsoleServicing` 调用 Core：

- 请求正文和搜索词只进入 stdin；
- `stdout` 与 `stderr` 分离，只有 `stdout` 可作为公共 JSON 解析；
- 读请求使用明确的短超时，非零退出、空输出、超大输出和非法 JSON 均
  fail closed；
- 取消或较旧 generation 的异步响应不得覆盖较新的 UI 状态。

`search` 必须使用 JSON `{"query": ...}` 配合 `--query-stdin`；
`context-console snapshot` 等 Phase 2 命令必须使用 `--payload-stdin`。查询
正文不得作为位置参数或其他 argv 值出现。

### 5.2 本地搜索

Phase 2 只使用本地 FTS、项目和半开时间范围过滤，不引入云端向量库。查询
输入需经过 NFKC 规范化、长度上限和 Unicode 控制字符检查；FTS 词项必须
作为字面量转义。因此 `%`、`_`、单双引号、FTS 操作符及其他标点不能被
解释成 SQL 通配符、SQL 片段或未授权的 FTS 语法。

FTS 仅索引批准的安全标签和状态，不索引截图/OCR、聊天正文或原始来源正文。
结果必须有界，并按项目及事件聚合，不能向 UI 返回原始 JSON、相邻重复帧、
内部 ID 或工程诊断字段。

### 5.3 public ref 与 internal ref

- Swift 只接收公共 `context_ref`、`bundle_ref`、`handoff_ref` 等不透明
  派生引用。
- 公共 `context_ref` 从规范化公共投影派生，不能复制、编码或可逆映射
  `verified_ref`、rowid 或数据库主键。
- `verified_ref`、Evidence/Candidate 私有引用和确认私有 key 只存在于
  Core 关系表和事务内部。
- `reason_code`、数据库错误文字和其他工程诊断只允许留在内部日志或高级诊断
  链，不进入 Phase 2 stdout 公共投影和 Swift public model。公共降级只用
  `busy`、`unknown`、`unavailable`、`blocked` 等闭合 `status` 表达。
- Bundle hash 覆盖稳定的公共 frozen projection，不包含生成时间、行顺序、
  内部引用或诊断字段。

## 6. macOS 新壳与降级状态

Phase 2 新壳独立于旧七页布局，业务逻辑不继续堆入原有大型视图：

- 顶级入口只有“今天、找回、记忆”；“设置”固定在侧栏底部。
- “今天”按项目突出 3–5 个可信进展、结果、阻塞和明确下一步；缺少可信
  下一步时显示缺失，不推测。最多突出 3 个可直接回答的问题，其余放入
  可展开收件箱；`information_insufficient` 不进入用户界面。
- “找回”采用项目结果与续接包预览的左右分栏；可信 Context 与待核实线索
  视觉隔离，续接包只使用可信内容。
- `⌘K` 只在 PAM 主窗口内切到“找回”并聚焦搜索，不建立系统级全局监听。
- “记忆”先显示安全摘要和治理状态，技术指纹、原因码和正文只在明确展开时
  显示；所有实际写入仍须经过未来的 Memory Gateway。
- 设置合并隐私、数据、健康和普通配置；漏斗、校准、VLM 队列和治理诊断
  只留在高级区或旧版界面。
- 菜单栏健康状态只显示正常、暂停、异常。数据库 busy/unknown 映射为异常，
  同时显示“状态未知＋观察时间”。
- “记一下”只接收文字，可自然使用 macOS 系统听写；不新增录音、音频文件
  或语音存储链路。
- Phase 2 激活时清理旧的日常确认和夜间批次待发通知；只有去重并持续存在的
  系统异常可以提醒。

任何 `available -> busy/unavailable/unknown` 转换都必须立即清空 Today、
Find、Memory 和 Delivery 的旧可信内容及操作能力。设置和旧版回滚入口仍
可访问，但 UI 不得用上一次成功快照伪装当前健康。

## 7. 功能开关、门禁与回滚

`context_console_phase2_ui_enabled` 是 macOS 新壳开关，默认值为 `false`：

- 关闭：主窗口直接显示完整旧界面；
- 开启：默认进入 Phase 2 新壳；
- 开启后仍可从设置高级区和菜单栏进入完整旧界面；
- 切换界面不删除、改写或迁移 Context 数据。

该 UI 开关与 Core 的 `context_console_phase1_enabled` 是两个不同边界：
界面可见不等于 Core 已获准写入；Core disabled/unknown 时新壳必须显示真实
降级状态并禁用动作。

本阶段不得打开以下能力：

- `ha_auto_package_enabled`；
- `context_memory_writer_enabled` 或任何绕过 Memory Gateway 的 writer；
- `context_source_revocation_cascade_ready`，直至真实端到端删除/撤回闭环另行
  验收；
- 自动 Hermes/飞书外发或任何把 prepared/consumed 当作 delivered 的捷径。

回滚只需关闭 `context_console_phase2_ui_enabled`，不删除 schema 31 新表和
已写入数据。数据库备份、实际 schema 31 live 迁移及恢复演练属于 Phase 3；
不得用旧二进制直接打开已经升级的数据库来模拟回滚。

## 8. 源码级验证边界

Phase 2 源码候选应接受以下门禁，但单次命令成功不代表安装或 live 验收：

- 完整 Python 测试发现，而不是硬编码旧测试数量；
- schema 30→31 仅新增两表、幂等迁移和旧数据不重写；
- 本人确认/“记一下”原子双写、非正向回答不验证、重答/删除/撤回级联；
- 反馈来源防伪、每来源一次、正负反馈记账边界；
- JSON stdin、查询转义、Unicode 控制字符、长度和结果上限；
- Delivery 的 needs-input、prepared、consumed、delivered、failed、重试、
  旧 ACK、缺 provider receipt 和撤回；
- 公共投影隐私检查，以及 busy/unknown 清空可信内容；
- 10k/100k 合成数据下的索引使用、结果上限、载荷和调用次数；
- `swift test`、现有 InteractionModel checks、CLI→Swift 真实 JSON fixture；
- macOS release 源码编译、`git diff --check`、依赖/锁文件审计、隐私和密钥
  扫描。

这些是源码、测试和构建层证据。Phase 2 不得据此声称已安装、已连接 live
Core、已操作真实数据库、已真实送达 Hermes/飞书，或已通过产品价值指标。

## 9. 明确保留到 Phase 3 的验收

以下项目必须在后续受控安装和真实自用阶段单独验证：

- 安装版入口、旧版回滚入口和 live Core 真实数据路径；
- 锁屏、排除应用和敏感页面的真实采集行为；
- 真实数据库迁移、备份、恢复及长期运行中的 busy 状态；
- Hermes 消费和飞书 provider receipt 的真实独立送达；
- VoiceOver 实机、普通操作 p95、30 秒找回；
- 14–21 个自然日、至少 10 次真实找回及 80%“有用”标记；
- 删除/撤回在安装 App、live Core、Memory Gateway 和外部消费者之间的端到端
  回执闭环。

在这些证据完成前，唯一允许的结论是：

> 本地 Phase 2 源码候选已准备接受源码级验收；安装、上线、真实投递和产品
> 指标均未在本阶段验证。
