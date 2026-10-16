# PAM Phase 2 本地源码候选验收

验收日期：2026-07-30
验收范围：源码、临时数据库、测试和本地构建
明确不在范围：安装 App、启动 live Core、真实数据库迁移、LaunchAgent、
Hermes/飞书真实投递、VoiceOver 实机、p95、30 秒找回及 14–21 天产品实验

## 1. 基线

- Git 基线：`main@85706ef13da39fbd50b229b1435132cc34a51bb1`
- Phase 0/1 源码 ZIP：`4,325,885` bytes
- Phase 0/1 ZIP SHA-256：
  `8f115cc1b1263d97caeb1e4e2b1a51248e4d45ce07c4a30199c46b9a64775c38`
- Phase 0/1 payload tree SHA-256：
  `04dff9cadb515dd118767b669028b21b9c931793f26d4c579beb5aec3342fbdd`
- 开发和复验均使用独立 worktree；原始 `main`、已安装 App 和 live Core
  未参与构建或测试。

## 2. 已实现的 Phase 2 范围

- schema 30 增量升级到 31，只新增：
  `context_activity_confirmation_links` 和
  `context_product_feedback` 两张持久领域表。
- 本人确认和“记一下”原子双写旧确认与 Context 图；重答、显式删除、
  撤回、隐私重分类和 retention 采用明确生命周期规则。
- 旧确认只读兼容投影独立于 VerifiedContext，不进入可信 Bundle、外部投递
  或长期记忆。
- `ContextSnapshotV1`、`OwnerContextActionV1`、
  `MemoryCandidateListV1`、`DeliveryStatusV1`、`LocalHandoffV1` 和
  `ProductFeedbackV1` 已接入真实 CLI/Context 图。
- 搜索和所有动作正文只经 JSON stdin；本地 FTS、项目和半开时间范围有界。
- Phase 2 stdout 与 Swift public model 不接收 `reason_code`、内部 Context
  引用、数据库 ID、原始 receipt、路径、URL、原始 JSON、秘密样式值或原始
  聊天/OCR/截图内容。
- Swift 新壳独立于旧七页界面，提供“今天、找回、记忆”和统一设置；
  `context_console_phase2_ui_enabled` 默认关闭，旧界面完整可回滚。
- busy/unknown/unavailable 会立即清空 Today、Find、Memory、Delivery 和操作
  能力；只有 `capturing`、`ready`、`running` 可进入菜单栏“正常”状态。
- Memory writer、HA 自动包、自动外发和不完整删除级联门禁继续关闭。
- 没有新增第三方依赖，没有锁文件变化。

## 3. 源码级证据

- Python 完整发现式测试：`853` 项通过。
- Phase 2 专项 Core 测试：`18` 项通过，覆盖 schema、双写、重答、级联、
  feedback 防伪、Memory/Delivery 读锁、搜索和 10k/100k 有界场景。
- `PAMInteractionModelChecks`：普通运行 `416` 个断言通过；使用真实 Python
  CLI 对临时空数据库生成的 Snapshot fixture 时 `421` 个断言通过。
- `swift test` 成功编译并链接 XCTest 和 Swift Testing 源文件；当前
  Command Line Tools 环境没有枚举测试用例，因此不得把它写成 XCTest 已
  实际执行。
- macOS release 源码编译通过；开发用 `.app` bundle 构建、图标往返和
  ad-hoc 签名校验通过，但该 bundle 未安装、未启动。
- 隐私扫描、`git diff --check`、依赖/锁文件比较、schema 30→31 临时空库
  比较通过。
- CLI→Swift fixture 只使用 `/tmp` 临时数据库；没有读取或迁移真实 PAM
  数据。

## 4. 外部审查与独立裁决

ChatGPT Pro 的首次 Core 子包因旁路模块、无真实集成、无完整测试和无效清单
被拒绝，未应用。随后同一对话收到真实集成候选作只读复审；其第一次结论又
因只在 `storage.py` 搜索 DDL、漏读 `context_console.py` 而产生错误 P0。
Codex 用准确源码位置和临时数据库差异证据要求其撤回误判并重新审计。
ChatGPT Pro 已正式撤回错误 P0/P1，并给出
`SOURCE REVIEW PASS（源码结构审计）`：确认 schema 31、Context 契约、
本人确认事务、反馈绑定、stdin-only CLI 路由和旧契约兼容均通过；未发现
阻断项。它没有重新执行测试，完整测试和最终包清单/哈希仍由 Codex 独立
完成。

外部模型的意见只作审查输入。最终是否通过以隔离 worktree、源码审查和本地
门禁结果为准。

## 5. 结论与剩余风险

当前结论只允许表述为：

> 本地 Phase 2 源码候选通过源码级验收；未安装、未上线、未连接 live
> Core，也未验证真实投递或产品价值指标。

仍需 Phase 3 验证：

- 安装版新旧入口、菜单栏、真实通知去重和 VoiceOver；
- 锁屏、排除应用和敏感页面真实采集；
- live Core 与真实数据库迁移、busy、备份和恢复；
- Memory Gateway 删除/撤回端到端回执；
- Hermes 消费和飞书 provider receipt；
- p95、30 秒找回、至少 10 次真实找回及 14–21 天自用实验。
