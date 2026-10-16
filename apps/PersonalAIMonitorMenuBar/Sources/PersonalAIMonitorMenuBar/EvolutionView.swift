import AppKit
import SwiftUI
import PAMInteractionModel

enum ContextConsumer: String, CaseIterable, Identifiable {
    case codex, agi
    var id: String { rawValue }
    var label: String { self == .agi ? "AGI 自主进化" : "Codex" }
}

enum EvolutionAction: String { case status, replay, pause, stop, resume; case refreshContext = "refresh-context" }

struct EvolutionTask: Identifiable {
    let id: String
    let title: String
    let phase: String
    let source: String
    let reference: String
    var status: String {
        switch phase {
        case "evidence_invalidated": return "证据失效 · 已停止使用"
        case "source_unavailable": return "来源暂不可核对"
        case "no_change": return "无需改动"
        case "observed": return "线索已记录 · 待补充证据"
        case "blocked": return "暂缓"
        default: return "待核对"
        }
    }
}

@MainActor
final class EvolutionModel: ObservableObject {
    static let shared = EvolutionModel()
    @Published var available = false
    @Published var busy = false
    @Published var mode = "unknown"
    @Published var contextCount = 0
    @Published var contextState = "unknown"
    @Published var checkedAt = ""
    @Published var tasks: [EvolutionTask] = []
    @Published var message = ""
    @Published var replayLabel = "尚未回放"
    @Published var componentVersion = "待读取"
    @Published var stopResults = ""
    @Published var isPaused = false

    var stateLabel: String {
        if !available { return "自主进化暂不可用" }
        if mode == "stopped" { return "自主进化已停止" }
        if isPaused { return "自主进化已暂停" }
        if contextState == "unavailable" || contextState == "unknown" { return "共享摘要暂不可核对" }
        return contextCount > 0 ? "已有获准摘要 · 可按需整理" : "等待单独共享的摘要"
    }
    var canAnalyze: Bool { available && mode != "stopped" && !isPaused && contextCount > 0 && !busy }

    @discardableResult
    func perform(_ action: EvolutionAction, runner: CoreRunner) async -> Bool {
        guard !busy else { return false }
        busy = true
        defer { busy = false }
        if action == .pause { _ = await runner.feedbackRequest("pause") }
        let raw = await runner.evolutionRequest(action)
        guard let data = raw.data(using: .utf8),
              let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              !["unavailable", "rejected"].contains(result["status"] as? String ?? "") else {
            available = false; tasks = []; contextCount = 0
            message = "进化组件未响应。原有工作页面可继续使用；操作结果未确认时请刷新核对。"
            return false
        }
        if action == .status {
            apply(result)
        } else {
            let messages: [EvolutionAction: String] = [
                .refreshContext: "已完成本地线索整理。没有调用模型或修改应用。",
                .replay: "已完成固定回放。回放结果不代表实际工作已经改善。",
                .pause: "已暂停自主进化，原有记录设置未改变。",
                .stop: "自主进化已停止，重启后仍保持停止。",
                .resume: "已恢复按需本地分析；没有开启持续观察或屏幕采集。"]
            message = messages[action] ?? ""
            let snapshot = await runner.evolutionRequest(.status)
            if let bytes = snapshot.data(using: .utf8),
               let value = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any], value["mode"] != nil {
                apply(value)
            } else { available = false; tasks = []; contextCount = 0 }
        }
        return true
    }

    private func apply(_ value: [String: Any]) {
        guard let state = value["mode"] as? String else { available = false; tasks = []; return }
        available = true; mode = state
        isPaused = value["local_analysis_paused"] as? Bool ?? false
        componentVersion = value["version"] as? String ?? "待核对"
        let context = value["context"] as? [String: Any] ?? [:]
        contextCount = context["authorized_references"] as? Int ?? 0
        contextState = context["state"] as? String ?? "unavailable"
        checkedAt = (value["updated_at"] as? String).map(LocalStatusTime.label) ?? "时间暂不可读"
        tasks = (value["tasks"] as? [[String: Any]] ?? []).reversed().map { row in
            let payload = row["payload"] as? [String: Any] ?? [:]
            return EvolutionTask(id: String(describing: row["id"] ?? ""), title: row["title"] as? String ?? "待核对线索",
                phase: row["phase"] as? String ?? "unknown", source: row["asset_id"] as? String ?? "",
                reference: payload["context_reference"] as? String ?? "")
        }
        if let replay = value["last_replay"] as? [String: Any], let total = replay["total"] as? Int,
           let passed = replay["passed"] as? Int {
            replayLabel = "\(passed) / \(total) 项固定回放通过"
        }
    }

    func stopAll(runner: CoreRunner) async {
        // Stop admissions through an independent marker, even if a UI request is busy.
        let feedbackRaw = await runner.feedbackRequest("stop")
        let feedbackStopped = FeedbackModel.decode(feedbackRaw)?["status"] as? String == "stopped"
        let evolutionRaw = await runner.evolutionRequest(.stop)
        let evolution = evolutionRaw.data(using: .utf8).flatMap {
            (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any]
        }
        let evolutionStopped = evolution?["mode"] as? String == "stopped"
        let contextStopped = await runner.stopRecordingService(requiresCaptureControls: false)
        stopResults = "批评与改进：\(feedbackStopped ? "已停止" : "停止未确认")；自主进化：\(evolutionStopped ? "已停止" : "停止未确认")；工作记录：\(contextStopped ? "已停止" : "停止未确认，请检查系统状态")。"
        _ = await perform(.status, runner: runner)
    }
}

struct AGIMark: View {
    let size: CGFloat
    var body: some View {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"), let image = NSImage(contentsOf: url) {
            Image(nsImage: image).resizable().frame(width: size, height: size).accessibilityLabel("AGI")
        } else { fallback }
    }
    private var fallback: some View {
        ZStack {
            RoundedRectangle(cornerRadius: size * 0.225).fill(Color(red: 0.05, green: 0.07, blue: 0.10))
            ForEach(0..<3) { segment in
                Circle().trim(from: 0, to: 70.0 / 360)
                    .stroke(Color(red: 0.075, green: 0.81, blue: 0.98), lineWidth: size * 0.09)
                    .rotationEffect(.degrees(-125 + Double(segment) * 120)).padding(size * 0.244)
            }
            Circle().fill(Color(red: 0.87, green: 0.98, blue: 1))
                .frame(width: size * 0.19, height: size * 0.19)
        }.frame(width: size, height: size).accessibilityLabel("AGI")
    }
}

struct EvolutionTodayCard: View {
    let runner: CoreRunner
    let open: () -> Void
    @ObservedObject private var model = EvolutionModel.shared
    var body: some View {
        GroupBox {
            HStack(spacing: 12) {
                AGIMark(size: 36)
                VStack(alignment: .leading, spacing: 3) {
                    Text("共享摘要 · " + model.stateLabel).font(.headline)
                    Text(model.available && ["available", "empty"].contains(model.contextState)
                         ? "\(model.contextCount) 条有效摘要 · \(model.tasks.count) 条整理记录 · 最近核对 \(model.checkedAt)"
                         : "当前摘要状态暂不可读；请打开页面核对。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("查看摘要", action: open)
            }.padding(8)
        }.task { _ = await model.perform(.status, runner: runner) }
    }
}

// Keep section headings as explicit accessibility elements. Named GroupBox
// labels can expose an invalid child relation to accessibility clients on macOS 27.
struct AGISectionBox<Content: View>: View {
    let title: String
    let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline).accessibilityAddTraits(.isHeader)
            GroupBox { content }
        }
    }
}

struct EvolutionView: View {
    let runner: CoreRunner
    var focusFeedback = false
    @ObservedObject private var model = EvolutionModel.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(spacing: 14) {
                    AGIMark(size: 48)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("自主进化").font(.largeTitle.bold())
                        Text("系统持续查证、处理和复查；只有关键取舍才需要你。") .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if model.busy { ProgressView().controlSize(.small) }
                    Button("刷新状态") { run(.status); Task { await FeedbackModel.shared.refresh(runner) } }.disabled(model.busy)
                }
                AutonomyWorkView(runner: runner)
                GroupBox {
                    VStack(alignment: .leading, spacing: 12) {
                        Label(model.stateLabel, systemImage: model.mode == "stopped" ? "stop.circle" : "circle.dotted").font(.title3.bold())
                        Text("工作上下文只使用你单独共享给 AGI 的摘要。当前可整理本地线索、运行固定回放；批评与改进按下方独立授权运行，原摘要权限保持独立。")
                        HStack {
                            Button("选择共享摘要…") { CodexSharingWindow.shared.show(runner: runner, consumer: .agi) }
                            Button("整理获准线索") { run(.refreshContext) }.disabled(!model.canAnalyze)
                            Spacer()
                            Text("\(model.contextCount) 条有效摘要").foregroundStyle(.secondary)
                        }
                    }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                }
                if !model.message.isEmpty { Text(model.message).font(.callout).foregroundStyle(.secondary).textSelection(.enabled) }
                FeedbackView(runner: runner).id("feedback")
                AGISectionBox("共享摘要整理记录") {
                    VStack(alignment: .leading, spacing: 12) {
                        if model.tasks.isEmpty {
                            Text("暂无候选问题").font(.headline)
                            Text("选择摘要并整理线索后，候选及其证据状态会出现在这里。没有足够依据时，保持观察也是有效决定。")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(model.tasks) { task in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(task.title).font(.headline)
                                Text(task.status).foregroundStyle(task.phase == "evidence_invalidated" ? .orange : .secondary)
                                if !task.reference.isEmpty { Text("已关联获准的来源摘要").font(.caption).foregroundStyle(.secondary) }
                                Text("尚未证明收益 · 未修改应用").font(.caption).foregroundStyle(.secondary)
                            }
                            Divider()
                        }
                    }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                }
                AGISectionBox("验证与掌控") {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text(model.replayLabel)
                            Spacer()
                            Button("运行固定回放") { run(.replay) }.disabled(model.busy || !model.available || model.mode == "stopped" || model.isPaused)
                        }
                        Text("回放使用随应用封存的合成故障样本，不读取业务正文。真实节省时间和故障改善尚未验证。")
                            .font(.caption).foregroundStyle(.secondary)
                        Divider()
                        HStack {
                            Button("暂停自主进化") { run(.pause) }.disabled(model.busy || !model.available || model.mode == "stopped")
                            Button("恢复本地分析") { run(.resume) }.disabled(model.busy || !model.available || (!model.isPaused && model.mode != "stopped"))
                            Spacer()
                            Button("停止 AGI 全部后台", role: .destructive) { Task { await model.stopAll(runner: runner) } }
                        }
                        Text("暂停只影响自主进化。全部停止会分别核对进化与工作记录；恢复本地分析不会重新开启屏幕采集。")
                            .font(.caption).foregroundStyle(.secondary)
                        if !model.stopResults.isEmpty { Text(model.stopResults).textSelection(.enabled) }
                    }.padding(10)
                }
                Text("AGI · 进化组件 \(model.componentVersion) · 兼容原有本地数据与 Codex 授权")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(24)
        }
        .task {
            _ = await model.perform(.status, runner: runner)
            if focusFeedback { proxy.scrollTo("feedback", anchor: .top) }
        }
        .onReceive(NotificationCenter.default.publisher(for: Notification.Name("AGISharingChanged"))) { _ in run(.status) }
        .onChange(of: scenePhase) { _, phase in if phase == .active { run(.status) } }
        }
    }
    private func run(_ action: EvolutionAction) { Task { _ = await model.perform(action, runner: runner) } }
}


/// Durable controller progress is separate from the independently shared context module.
private struct AutonomyWorkView: View {
    let runner: CoreRunner
    @ObservedObject private var model = FeedbackModel.shared
    @State private var filter = "active"
    private var visibleItems: [AutonomyWorkItem] {
        (model.autonomy?.items ?? []).filter { item in
            switch filter {
            case "waiting": return ["waiting_evidence", "blocked"].contains(item.state)
            case "history": return ["observing", "verified", "resolved"].contains(item.state)
            default: return !["verified", "resolved"].contains(item.state) || item.evidenceChanged
            }
        }
    }
    var body: some View {
        AGISectionBox("系统自主处理") {
            VStack(alignment: .leading, spacing: 12) {
                if let snapshot = model.autonomy {
                    HStack {
                        Text(!snapshot.enabled ? "尚未配置自主查证" : snapshot.paused ? "自主查证已暂停" : model.paused ? "后台已暂停 · 自主查证等待恢复" : "按证据持续查证与复查").font(.headline)
                        Spacer()
                        Button(snapshot.paused ? "恢复自主查证" : "暂停自主查证") {
                            Task { await model.control(snapshot.paused ? "autonomy-resume" : "autonomy-pause", runner: runner) }
                        }.disabled(!snapshot.enabled || model.controlBusy || model.busy)
                    }
                    Text(snapshot.summary).font(.callout).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("记忆全量检查与 Jev 辅助").font(.headline)
                        ForEach(Array(snapshot.memoryCoverageLines.enumerated()), id: \.offset) { _, line in
                            Text(line).font(.caption).textSelection(.enabled)
                        }
                        Text("敏感内容留在本地；完成检查不代表事实获批。普通优先级会保留原顺序。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text("新证据出现后重新核对；没有变化时保留等待。你不需要为了清空列表去回忆或猜测。")
                        .font(.callout).foregroundStyle(.secondary)
                    Picker("查看自主处理", selection: $filter) {
                        Text("进行中").tag("active")
                        Text("等待证据").tag("waiting")
                        Text("处理与验证记录").tag("history")
                    }.pickerStyle(.segmented)
                    if visibleItems.isEmpty {
                        Text("当前范围内暂无事项。处理状态以后台记录为准。").foregroundStyle(.secondary)
                    }
                    ForEach(visibleItems) { item in
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(item.kind == "memory" ? "记忆核查" : item.projectLabel).font(.caption).foregroundStyle(.secondary)
                                if item.evidenceSummaries.isEmpty {
                                    Text("依据：暂无可展示的核查摘要。")
                                } else {
                                    Text("已核对的依据").font(.headline)
                                    ForEach(Array(item.evidenceSummaries.enumerated()), id: \.offset) { _, summary in Text(summary) }
                                }
                                if !item.resultSummary.isEmpty { Text("处理结果：" + item.resultSummary) }
                                if !item.nextAction.isEmpty { Text("下一步：" + item.nextAction) }
                                if !item.nextTrigger.isEmpty { Text("继续核对的条件：" + item.nextTrigger).foregroundStyle(.secondary) }
                                if !item.changes.isEmpty {
                                    Text(item.evidenceChanged ? "原处理记录 · 待重新核对" : "实际处理结果").font(.headline)
                                    ForEach(Array(item.changes.enumerated()), id: \.offset) { _, change in Text(change) }
                                }
                                if !item.verificationSummary.isEmpty { Text("验证结果：" + item.verificationSummary) }
                                if !item.limitation.isEmpty { Text(item.limitation).foregroundStyle(.secondary) }
                                if let date = item.nextCheck { Text("下次核对：" + LocalStatusTime.label(date)).font(.caption).foregroundStyle(.secondary) }
                                if let date = item.updated { Text("最近更新：" + LocalStatusTime.label(date)).font(.caption).foregroundStyle(.secondary) }
                            }.padding(.vertical, 8).textSelection(.enabled)
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(item.summary).font(.headline)
                                Text(item.statusLabel).font(.caption).foregroundStyle(.secondary)
                            }.padding(.vertical, 4)
                        }
                        Divider()
                    }
                    Text("处理完成和实际效果已验证分别记录；旧证据失效后会重新核查。")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("自主处理状态暂不可读取").font(.headline)
                    Text("无法确认当前队列和结果，未将未知状态显示为已完成。请刷新状态。")
                        .foregroundStyle(.secondary)
                }
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
        }.task {
            while !Task.isCancelled {
                await model.refresh(runner)
                do { try await Task.sleep(nanoseconds: 30_000_000_000) } catch { break }
            }
        }
    }
}
