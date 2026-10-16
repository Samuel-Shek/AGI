# PAM 0.4.0 Phase 3 本地发布与回滚

本页只定义 `0.4.0 / schema 31` 的受控本机发布步骤。源码测试、构建成功、
shadow 迁移或远端 CI 都不等于已安装、live Core 已切换、真实投递完成或产品
指标通过。

## 发布信任根

- Git 基线固定为 `85706ef13da39fbd50b229b1435132cc34a51bb1`。
- 第 1 个提交固定为
  `4a7c2e54ced277f56f2d42ca07a6bfbeb5cfe03f`，绑定 Phase 0/1 归档：
  ZIP SHA-256 `8f115cc1b1263d97caeb1e4e2b1a51248e4d45ce07c4a30199c46b9a64775c38`，
  payload tree SHA-256
  `04dff9cadb515dd118767b669028b21b9c931793f26d4c579beb5aec3342fbdd`。
- 第 2 个提交固定为
  `475e336a46ae7bf96a8bd6e44915d3599e4f66f7`，绑定 Phase 2 归档：
  ZIP SHA-256 `abff7c7a496e1d47604e941c304488183c4903202d1d60f09dd6feb253fbf11d`，
  payload tree SHA-256
  `7c57c139d9d2189aedd8bade16e5ab838a46eff0237b042dbfe7613b7151c607`。
- 安装 preflight 不依赖历史 ZIP 仍留在本机；ZIP 与提交的逐文件一致性属于
  前序独立归档审计证据。preflight 会核对上述固定提交，并要求基线之后恰好
  4 个单父、线性、无 merge 提交，第 4 个提交必须等于配置、本地 HEAD、远端
  分支、tag 和 CI 绑定的完整 40 位 HEAD；候选 App 另以该提交的 12 位前缀及
  source tree/Core hash 共同核验。
- 候选必须位于私有远端的
  `release/v0.4.0-context-console`，不可移动的 `v0.4.0-rc.N` tag、远端分支、
  本地 HEAD、构建元数据和指定 GitHub Actions run 必须指向同一提交。远端
  仓库必须由 GitHub API 明确返回 `private=true`。

## 安全边界

- `tools/phase3_rollout.py` 默认 dry-run；建 shadow、安装、回滚分别要求
  `--apply` 和不同的固定确认口令。
- `./tools/phase3_rollout.py` 是唯一受支持的公开入口。其 shebang 会先清空调用方
  环境，再以固定 Python.org 3.12、`-I -S` 启动；不得绕过该入口直接加载脚本，
  也不得使用 `PYTHONPATH`、shell hook 或 DYLD 注入方式调用。
- `./scripts/build-macos-app.sh` 是唯一受支持的构建入口。它在 Bash 解析实现脚本
  前清理环境并把实现脚本绑定到当前 Git blob；发布版本固定为 `0.4.0`。本地诊断
  只能显式使用 `--allow-dirty-development`，只读 provenance 使用
  `--provenance-only`；环境变量不能覆盖版本、Git index、SDK 或编译器。
- 工具不删除 App、Core、数据库或回滚件。切换使用同卷 rename；被替换的
  目录、失败候选和取证临时件都保留在 `0700` 回滚目录。
- 真实数据库只通过 SQLite backup API 生成 WAL 一致副本；活动库、WAL 和
  SHM 不作为普通文件复制到 staging Core。
- `candidate_app` 可位于 `source_root` 内；除此之外 source、candidate、live
  Core 和 installed App 必须两两隔离。live DB 必须精确等于
  `live_core/data/activity.sqlite`，两份 plist 必须彼此独立并位于所有切换目录
  之外。此矩阵在建立目录、复制或 rename 之前检查。
- 第一个停止 Core 的动作必须是直接 `launchctl bootout`。禁止先调用旧的
  `install-launch-agent`，因为它会在 bootout 前执行 `ensure_ready()`。
- 主 Core 与 shadow 的 plist 必须分别匹配完整 argv 契约；shadow 只允许
  `task-sources serve-receipts --socket ABSOLUTE_PATH`，并使用与 live Core/DB
  路径、解析路径和 inode 都独立的 schema 库。Python 可使用 root-owned 的
  symlink 链，但最终解释器必须为 Python Software Foundation 严格签名的
  `python3`（Team ID `BMM5U3QVKW`）、版本至少 3.12；解析路径、SHA-256、版本
  和签名身份写入 source identity。版本只能来自后述单 FD 读取的已绑定
  `Info.plist`：`CFBundleShortVersionString` 与 `CFBundleVersion` 必须是相同的规范
  `X.Y.Z`，其 major/minor 必须同时匹配 launcher 文件名和 framework version 目录。
  身份探测不得为了取得版本而执行 launcher 或内层 Python；候选 Python 只可在
  launcher、外层 bundle 与内层进程映像全部认证后用于明确的迁移或运行步骤。
  Python.org 在 macOS 上会由该 launcher 启动
  同版本根内固定的 `Resources/Python.app/Contents/MacOS/Python` 进程映像；其
  framework 拓扑、路径、dev/inode、大小、SHA-256 和 bundle 版本必须固定；外层
  `Python.app` 与内层进程映像的 Identifier、Team ID、CDHash 和完整 Authority 链
  必须分别验证并一同写入 source identity；launcher、外层 bundle 和内层进程映像
  还必须分别通过 `anchor apple generic + exact identifier + exact leaf Team ID` 的
  designated requirement，不能只信任可显示的签名文本。`Info.plist` 必须通过
  `O_NOFOLLOW` 打开的单一文件描述符读取，同一份 bytes 用于 SHA-256 与 plist 解析，
  并在读取前后用 `fstat` 固定身份。路径链必须由 root 拥有并拒绝 world-writable；
  兼容 Python.org 的 root:wheel/root:admin group-writable 目录，且只允许这两个预期 group。
  root:admin `0775` 代表其他 admin 组成员仍可能改写路径，编排工具不声称可以阻止管理员；
  该风险由精确签名 requirement、完整 Authority 链、SHA-256、dev/inode 与多次身份复核
  共同约束，任一漂移均 fail closed。三层身份均在切换前后及运行 PID 上复核。主、shadow
  可共享同一份已核验解释器。shadow 与正式 staging 的迁移子进程必须直接使用
  该 source identity 中的解释器，并在启动前重新核对完整身份；不得退回编排工具
  自身的 `sys.executable`。
- `launchctl print` 的 launcher program、完整 arguments 数组、工作目录和正 PID
  必须逐字段匹配；随后以 `lsof` 核对 PID 的受认证进程映像路径/dev/inode 和 cwd，
  cwd 也必须同时匹配路径/dev/inode，再以 `ps` 核对 UID，以及“进程映像路径 +
  plist arguments[1...]”组成的完整命令；完成这些 OS 级证明和解释器双身份复核后，
  必须对同一 label 再执行一次 `launchctl print`，program、完整 arguments、工作目录与 PID
  必须和首次快照完全一致。证据不可读、
  含糊或只在旁路文本中“包含”期望字符串时均 fail closed。
- 安装事务的状态文件、SQLite backup 文件及父目录、staging App/Core/runtime
  的全部普通文件和目录、私有快照和 rename 目录项在宣告完成前都执行 fsync；
  staging 树中的 symlink、socket、FIFO 或其他特殊项会被拒绝。
  最终 `installed` journal 若无法持久化，安装也视为失败并恢复 0.3.22。
- 旧 `0.3.22` Core 只可打开 schema 28。回滚以安装时封存的旧 App、Core、
  两份 plist 和 schema-28 数据为信任根；即使当前 0.4 App/Core 缺失或当前
  schema-31 库损坏，也不得阻止恢复。封存的回滚件以及 canonical App/Core 路径
  遇到符号链接或特殊文件仍 fail closed；不可信的当前数据库或 plist 只可隔离，
  不得写入或作为回滚信任根。
- 回滚异常时只允许继续完成 0.3.22 恢复；不得重新启动 0.4。若旧版也无法
  完整恢复，App、主 Core 和 shadow 均保持停止，UI 开关保持 false，并写入
  `rollback_failed_recovery_required`，等待人工取证。
- 安装时另存一份独立、已核验 SHA-256 与确认摘要的 schema-28 SQLite backup。
  回滚时健康的旧 Core 数据库按原 inode 直接启用；若其缺失、截断、schema
  错误或 WAL/SHM 损坏，则先验证独立 backup，再把整个坏数据库家族 durable
  rename 到唯一的 `0700` quarantine（不覆盖、不删除），用 SQLite backup API
  重建标准路径并复核 schema 28、聚合计数与本人确认摘要。此过程可在崩溃后
  重入；独立 backup 损坏时保持全部服务停止并 fail closed。
- Core 功能开关 `context_console_phase1_enabled` 与 UI 开关
  `context_console_phase2_ui_enabled` 相互独立。初装二者均为 false；三个永久
  安全门禁始终必须为 false：`ha_auto_package_enabled`、
  `context_memory_writer_enabled`、`context_source_revocation_cascade_ready`。
- live Core 的 `AGENTS.md` 只能从旧 live Core 原样复制，必须是普通 `0644`
  文件并保持 SHA-256；其他未知 Core 根目录文件不得顺带迁移。
- shadow 和正式 staging Core 都只从 `expected_revision` 的 Git blob 物化，目录
  固定为 `0755`，不执行 release worktree。Git 内容/模式清单（排除随后单独治理的
  `AGENTS.md` 与 `data/`）、AGENTS SHA-256 及最终完整清单会在 renderer、迁移、
  激活和 verify 边界重复核验；忽略文件、untracked import shadow、`.pyc`、原生
  扩展和后封存篡改均 fail closed。
- 旧 `0.3.22` Core 的完整可执行树也会单独封存内容/模式清单，根目录采用闭合
  allowlist，并拒绝 import shadow、孤立 bytecode、原生扩展和可写路径。数据库
  `data/` 由独立的 canonical-path、权限、inode 与 SQLite backup 规则治理。
- App 的强制停止不能只凭 argv。每次发出 TERM/KILL 前，PID 的启动身份和 text
  vnode 必须匹配已独立核验的当前、旧版或失败 App 工件；未知同名进程不会收到信号。
- 空间门禁按卷聚合 staging、迁移后数据库、唯一回滚数据库、恢复空间与 journal，
  并在关键停服/rename 边界重算；操作后剩余空间必须至少为 1 GiB 或峰值基数的
  25%（取较大者）。第二次回滚空间门禁失败属于 mutation 前安全中止，会恢复原
  进程状态，不执行 rename、数据库重建或向前回滚。
- journal 使用专用私有临时文件、fsync 与 `os.replace`；切换树使用 exclusive
  rename 并 fsync 两侧父目录。rename 已成功但随后 fsync 报错时，状态机必须按
  实际目录状态补偿或完成向前回滚，不能把“抛错”误当成“未移动”。
- 已安装 `0.3.22` App 的回滚身份覆盖代码签名、普通文件内容/模式以及禁止的
  quarantine/FinderInfo/ResourceFork xattr；现有 POSIX ACL 与其他允许 xattr 不纳入
  payload digest。它们不会被自动清理或声称已证明无差异，异常时应停在 preflight
  并人工审计。新的 `0.4.0` 候选必须来自 clean build，不能复用旧 dirty 构建包。

## 私有配置

配置文件必须是绝对路径、普通文件和 `0600`，不得放进 Git。示例中的值必须
由本次 clean 构建和私有远端证据填写，不能照抄占位符：

```json
{
  "source_root": "/absolute/clean/release-worktree",
  "candidate_app": "/absolute/clean/release-worktree/build/Personal AI Monitor.app",
  "live_core": "/absolute/Application Support/Personal AI Monitor/core",
  "live_database": "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite",
  "installed_app": "/Applications/Personal AI Monitor.app",
  "launch_agent_plist": "/absolute/Library/LaunchAgents/app.personalai.monitor.core.plist",
  "shadow_launch_agent_label": "app.personalai.monitor.codex-shadow",
  "shadow_launch_agent_plist": "/absolute/Library/LaunchAgents/app.personalai.monitor.codex-shadow.plist",
  "workspace": "/absolute/private/phase3-run",
  "expected_revision": "FULL_40_HEX_FINAL_COMMIT",
  "expected_source_tree_sha256": "FULL_64_HEX_SOURCE_TREE_HASH",
  "expected_core_code_sha256": "FULL_64_HEX_CORE_HASH",
  "expected_app_payload_sha256": "FULL_64_HEX_APP_PAYLOAD_HASH",
  "expected_app_executable_sha256": "FULL_64_HEX_APP_EXECUTABLE_HASH",
  "expected_app_cdhash": "FULL_40_OR_64_HEX_CDHASH",
  "expected_app_signature_kind": "adhoc",
  "expected_app_team_identifier": "not set",
  "gh_executable": "/absolute/trusted/gh",
  "expected_gh_sha256": "FULL_64_HEX_GH_HASH",
  "expected_privacy_profile": "personal_high_precision",
  "github_repository": "PRIVATE_OWNER/PRIVATE_REPOSITORY",
  "github_actions_run_id": 123456789,
  "release_branch": "release/v0.4.0-context-console",
  "release_candidate_tag": "v0.4.0-rc.1",
  "service_label": "app.personalai.monitor.core"
}
```

路径须满足上文的闭合矩阵，workspace 必须是新的专用目录。候选 App 必须来自 clean
commit；Info.plist 的版本、12 位 commit 前缀、tree/Core hash、`dirty=false`、
xattr 和代码签名必须与完整 40 位配置及构建证据一致。shadow plist 必须指向独立 Core 和独立
数据库；工具在需要恢复已运行的 shadow 时还会要求真实 PID 与 canonical 路径。
目标 Mac 的 System Integrity Protection 必须明确为 `enabled`；preflight、shadow 和
install 都会通过固定 `/usr/bin/csrutil` 重新核验，disabled、custom、命令失败或非精确
输出均 fail closed。GitHub hosted runner 上因架构不兼容而拒绝 DYLD 探针，只能记为
loader fail-closed，不得替代目标 Mac 的 SIP 现场证据。

## 安装前门禁

私有 GitHub Actions 必须实际完成以下 blocking jobs，不能以本机缺少
`xctest`、零测试、skip 或 `continue-on-error` 代替：

- `core`；
- `Swift tests (Xcode 15.4)`；
- `Swift tests (Xcode 16.4)`；
- `app-build`。

Swift job 会把源码声明与版本化 baseline manifest 精确比对，再用 Swift/XCTest
实际枚举交叉验证，并运行筛选执行与完整 `swift test`；XCTest 与 Swift Testing
证据分开解析，拒绝零测试、基线漂移、缺项、重复、not-run、skip、failure 或
error，不依赖会过期的硬编码数量。远端 run 必须是上述私有 release branch 的 `push`
事件，并与 RC tag 和 `expected_revision` 完全相同。
GitHub Actions run 的 `path` 还必须精确等于 `.github/workflows/ci.yml`；同一
commit/branch 上其他 workflow 即使伪造相同 job 名也不得满足安装门禁。

以下命令先只读/规划；只有对应结果已独立复核才执行下一步：

```bash
./tools/phase3_rollout.py --config /private/config.json preflight
./tools/phase3_rollout.py --config /private/config.json shadow
./tools/phase3_rollout.py --config /private/config.json shadow \
  --apply --confirm CREATE_SCHEMA31_SHADOW
./tools/phase3_rollout.py --config /private/config.json verify --target shadow
./tools/phase3_rollout.py --config /private/config.json install
./tools/phase3_rollout.py --config /private/config.json install \
  --apply --confirm INSTALL_PAM_V040
./tools/phase3_rollout.py --config /private/config.json verify --target installed
```

第一条 `preflight` 的 JSON 是本机实际 Python.org 只读 probe 证据，必须原样保存。
其中 `identity.python_interpreter` 必须同时包含 launcher、外层 `Python.app`、内层
进程映像及各自的 version/hash/dev/inode/CDHash/Authority chain；任一缺失都不得安装。
该 identity 检查本身只调用系统签名与文件读取工具；除启动编排工具本身的既有
解释器外，不会再执行目标 launcher 的 `--version` 或另行启动候选 Python。由于
GitHub hosted runner 不具备本机固定的 root-owned Python.org
路径，这项证据不放进可移植的 unittest discovery，也不得用 remote skip 或无目标
时空跑冒充通过；远端 discovery 必须保持零 skip，本机则以保存的真实 preflight JSON
单独验收。

shadow 会核验 schema 28、`quick_check`、`foreign_key_check`、权限、关键聚合
计数和明确本人回答摘要；迁移后核验 schema 31、要求的 Context 图、两张新增
表、迁移备份身份和关闭的全部门禁。install 会停机后重新备份最新数据库，
不复用可能过时的 shadow 数据。

安装固定顺序为：先创建 journal 与回滚目录并只读封存两份 plist → 在旧服务仍
运行时 staging 新 App、Git 物化 Core 与两份新 plist，递归 fsync、验证静态候选并
重算停服前空间 → 退出 App → 直接 bootout 主 Core → 暂停但不删除 shadow →
确认 DB/WAL/SHM 无句柄 → 对 live schema-28 DB 建立
`BEGIN IMMEDIATE` writer barrier 并记录 inode，只允许当前编排 PID 持有数据库
家族 → 快照并关闭 UI → 通过 SQLite backup 封存最新数据 → 构造、迁移、递归
fsync 并验证 staging runtime/数据库 → 在 barrier 内重查 inode/外部句柄并同卷 rename 旧 Core
→ rollback/关闭 barrier → 同卷切换新 Core 与 App → canonical 验证 →
bootstrap 新 Core → 恢复 App 原先运行/未运行状态。shadow 保持暂停；任何一步
  失败都恢复原 App/Core/UI/主 Core/shadow 状态，失败候选保留。可捕获的
  `KeyboardInterrupt` / `SystemExit` 也必须先完成并持久化同一补偿或 fail-closed
  恢复，再以原异常类型和值向调用方重抛；普通异常保持既有 RolloutError/恢复结果语义。

## 受控启用顺序

安装完成后的首次验收必须核对 Core/UI 仍为 false。后续显式启用后，
`verify --target installed` 仍可通过并如实回报两者状态，但三个永久安全门禁
始终必须为 false。先从安装版逐项验证旧七页完整可达；随后才使用新 Core 和
明确的 live DB 开启 Core：

```bash
cd "/absolute/Application Support/Personal AI Monitor/core"
python3 -m activity_mvp context-console configure \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite" \
  --enabled on --confirm ENABLE_CONTEXT_CONSOLE_SOURCE_CANDIDATE
python3 -m activity_mvp context-console status \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite"
defaults write app.personalai.monitor context_console_phase2_ui_enabled -bool true
defaults read app.personalai.monitor context_console_phase2_ui_enabled
```

必须先验证 Core 返回 enabled、三个永久安全门禁仍为 false，再打开 UI。任一步
出现 busy/unknown、身份不一致或界面缺陷，先关闭 UI；Core/迁移/隐私门槛失败
才整套回滚。不得借此开启 Memory writer、HA 自动包或自动外发。

## 14–21 天产品实验

只有 `TECHNICAL_LOCAL_PASS` 和唯一一次受控投递的 `INTEGRATION_PASS` 已分别
取得现场证据后，才可在 live Core 上配置自然日时区并显式启动实验。启动命令
没有日期参数：Core 必须在同一写事务内确认 Context 开关已开启，并从已配置的
IANA 时区与当前时刻推导开始日期，禁止回填或提前填写日期。

```bash
cd "/absolute/Application Support/Personal AI Monitor/core"
python3 -m activity_mvp context-console configure-timezone \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite" \
  --timezone Asia/Shanghai
python3 -m activity_mvp context-console start-experiment \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite" \
  --experiment-ref experiment_<opaque_random_ref> \
  --confirm START_LOCAL_PRODUCT_EXPERIMENT
python3 -m activity_mvp context-console experiment-status \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite"
```

实验分母只能由新版“有用，继续工作”或“没帮助”动作产生的
`ProductFeedbackV1` 写入。每条反馈必须绑定真实 Bundle/本地续接包、当前活动
实验和单调时钟冻结的 `resume_seconds`；负反馈计入分母但不产生胜绩。CLI 不
提供人工补写尝试、结果或秒数的入口。查询正文、项目正文、内部 ID 和活动正文
不得进入指标、状态证据或日志。

每日只读取并保存脱敏后的 `ProductExperimentStatusV1`。读取状态不得启动、
延长、缩短或终结实验。第 14 个上海自然日若已有至少 10 条完整真实反馈，写入
第 10 条合格反馈的事务会冻结首个可判定结果；不足 10 条时状态只能是
`extended`，继续到首次达到 10 条或第 21 个自然日。第 21 日仍不足时，使用
下列带确认口令的命令把结果冻结为 `insufficient/not_passed`；证据缺字段时必须
拒绝终结，不能猜测或补零。

```bash
python3 -m activity_mvp context-console finalize-experiment \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite" \
  --experiment-ref experiment_<same_opaque_ref> \
  --confirm FINALIZE_LOCAL_PRODUCT_EXPERIMENT
python3 -m activity_mvp context-console experiment-status \
  --db "/absolute/Application Support/Personal AI Monitor/core/data/activity.sqlite"
```

有用率必须 `>=80%`，找回中位时间必须严格 `<30` 秒；达到第 10 次即冻结，最迟
Day 21。状态 JSON 只作为不含正文的产品证据保存，必须同时记录安装 commit、
时区、观察时间和来源哈希。源码测试、合成尝试或人工数据库写入一律不得计入
`PRODUCT_PASS`。

## 回滚

```bash
./tools/phase3_rollout.py --config /private/config.json rollback
./tools/phase3_rollout.py --config /private/config.json rollback \
  --apply --confirm ROLLBACK_PAM_V0322
```

回滚先停止 App、主 Core 和 shadow，确认当前数据库家族无句柄并关闭 UI；只有
当前库可安全识别为 schema 31 时才尝试关闭 Core 功能开关，未知/损坏库不会被
写入。随后保留当前候选，恢复未修改的 `0.3.22 / schema 28` App、Core、plist
和安装前的 UI、主 Core、shadow 状态；旧版 App 是否重新打开则按回滚开始前的
App 运行状态恢复。工具绝不把 schema 31 改标成 28，也不删除新表。

安装后仍须分别保存源码、构建 App、`/Applications`、Application Support
Core、LaunchAgent/PID、真实入口与外部 delivery receipt 证据。只有安装版技术
验收全部通过，才可声明 `TECHNICAL_LOCAL_PASS`；唯一一次 Hermes/飞书消息需
当场再次确认；14–21 天产品实验另行计时，源码或短时 smoke 不能代替。
