import AppKit
import SwiftUI
import PAMInteractionModel

struct FeedbackIncident: Identifiable {
    let id: String
    let title: String
    let status: String
    let phase: String
    let cause: String
    let evidence: String
    let fix: String
    let gap: String
    let revision: Int
}

@MainActor
final class FeedbackModel: ObservableObject {
    static let shared = FeedbackModel()
    @Published var progress: FeedbackProgress?
    @Published var autonomy: AutonomySnapshot?
    @Published var controlBusy = false
    @Published var incidents: [FeedbackIncident] = []
    @Published var sources: [String] = []
    @Published var enabled = false
    @Published var paused = true
    @Published var busy = false
    @Published var pending = 0
    @Published var message = ""
    @Published var journal = ""
    @Published var analysisProblem = ""
    @Published var repair = "自动修复尚未验收"
    @Published var analysis = "模型通道尚未验收"
    @Published var lastReview = "尚未复查"
    @Published var lastTick = "尚未收录"
    @Published var budget = ""
    var attentionCount: Int { progress?.decisions ?? incidents.filter { FeedbackDecisionPresentation.requiresHuman(state: $0.phase) }.count }

    func refresh(_ runner: CoreRunner) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        let raw = await runner.feedbackRequest("status")
        guard let value = Self.decode(raw), value["status"] as? String == "ok" else {
            progress = nil; autonomy = nil; incidents = []; sources = []; enabled = false; paused = true
            journal = ""; repair = "暂不可读"; analysis = "暂不可读"; budget = "暂不可读"
            pending = 0; lastTick = "暂不可读"; lastReview = "暂不可读"; analysisProblem = ""
            message = "批评与改进暂不可读取；不能据此判断没有待办。"; return
        }
        progress = (value["progress"] as? [String: Any]).flatMap(FeedbackProgress.init)
        autonomy = (value["autonomy"] as? [String: Any]).flatMap(AutonomySnapshot.init)
        enabled = value["enabled"] as? Bool ?? false
        paused = value["paused"] as? Bool ?? true
        journal = value["journal"] as? String ?? ""
        pending = value["pending_messages"] as? Int ?? 0
        let lastError = value["last_error"] as? String ?? ""
        let control = value["analysis_control"] as? [String: Any] ?? [:]
        let retryAt = control["next_retry_at"] as? Double ?? 0
        let retryText = retryAt > Date().timeIntervalSince1970 ? "预计 \(Date(timeIntervalSince1970: retryAt).formatted(date: .omitted, time: .shortened)) 后重试。" : "后台会继续分批处理。"
        if lastError == "account_rate_limited" {
            analysisProblem = "Codex 通道返回用量或频率限制。\(retryText)"
        } else {
            analysisProblem = lastError.isEmpty || lastError == "daily_budget_exhausted" ? "" : "最近一次分析未完成。\(retryText)处理进度保留在系统记录中。"
        }
        let changed = value["source_changed_messages"] as? Int ?? 0
        message = changed > 0 ? "有 \(changed) 条来源已变化，保留待核对，未继续转交分析。" : ""
        repair = (value["repair_capability"] as? [String: Any])?["reason"] as? String ?? "待核对"
        let capability = value["model_capability"] as? [String: Any] ?? [:]
        analysis = capability["enabled"] as? Bool == true ? "现有 Codex 通道已验证 · gpt-6-astra／Max" : capability["reason"] as? String ?? "待核对"
        lastReview = value["last_review"] as? String ?? "尚未复查"
        if let stamp = value["last_tick"] as? Double { lastTick = Date(timeIntervalSince1970: stamp).formatted(date: .abbreviated, time: .shortened) }
        incidents = (value["incidents"] as? [[String: Any]] ?? []).map { r in
            FeedbackIncident(id: r["id"] as? String ?? "", title: r["title"] as? String ?? "待核对",
                status: r["status_label"] as? String ?? "待核对", phase: r["state"] as? String ?? "",
                cause: r["cause"] as? String ?? "", evidence: r["evidence"] as? String ?? "",
                fix: r["fix"] as? String ?? "", gap: r["gap"] as? String ?? "", revision: r["revision"] as? Int ?? 0)
        }
        sources = (value["sources"] as? [[String: Any]] ?? []).map { row in
            let labels = ["current": "已接入", "no_owner_events": "没有近期已核实本人记录", "backfill": "历史补录中", "pending": "等待首次读取", "partial_identity": "已接入本人来源 · 部分身份未核实", "unavailable": "未覆盖 · 来源不可用"]
            return "\(row["id"] as? String ?? "来源")：\(labels[row["status"] as? String ?? ""] ?? "待核对")"
        }
        budget = (value["budget"] as? [[String: Any]] ?? []).map { row in
            "\(row["kind"] as? String == "analysis" ? "分析" : "修复") \(row["used"] as? Int ?? 0) 次"
        }.joined(separator: " · ")
    }
    func control(_ action: String, runner: CoreRunner) async {
        guard !controlBusy && !busy else { return }
        controlBusy = true; defer { controlBusy = false }
        let value = Self.decode(await runner.feedbackRequest(action))
        guard let status = value?["status"] as? String, ["paused", "resumed"].contains(status) else {
            message = "操作未确认，请刷新后核对。"; return
        }
        await refresh(runner)
    }
    static func decode(_ raw: String) -> [String: Any]? {
        raw.data(using: .utf8).flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
    }
}

struct FeedbackView: View {
    let runner: CoreRunner
    @ObservedObject private var model = FeedbackModel.shared
    @State private var selected: FeedbackIncident?
    var body: some View {
        AGISectionBox("批评与改进") {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(model.progress?.label ?? "批评与改进暂不可读").font(.headline)
                    Spacer()
                    Button("刷新") { Task { await model.refresh(runner) } }.disabled(model.busy)
                    Button(model.paused ? "恢复" : "暂停") { Task { await model.control(model.paused ? "resume" : "pause", runner: runner) } }.disabled(!model.enabled || model.controlBusy)
                }
                if let progress = model.progress {
                    Text("\(progress.total) 件已收录 · \(progress.pending) 条待分析 · \(model.attentionCount) 件需要你选择").foregroundStyle(.secondary)
                }
                Text("最近收录：\(model.lastTick)；每日复查：\(model.lastReview)").font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("来源与自动处理范围") {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(model.sources, id: \.self) { Text($0) }
                        Text(model.analysis); Text(model.repair)
                        Text("分析不设本机每日次数上限，每批最多五条，逐批进行；仍受 Codex 账户实际限制。")
                        Text("每天最多一个修复任务，每个修复最多两轮。今日调用记录：\(model.budget)")
                        Text("系统先核查记忆和关联项目，再在已授权范围内处理；偏好、新增权限、费用和重大不可逆操作才需要你选择。")
                    }.font(.caption).padding(.top, 6)
                }
                if !model.analysisProblem.isEmpty { Text(model.analysisProblem).foregroundStyle(.orange) }
                if !model.message.isEmpty { Text(model.message).font(.callout).foregroundStyle(.secondary) }
                if model.progress != nil && model.incidents.isEmpty { Text("尚无已核实可归档的批评。新消息识别和历史补录会分批继续。") }
                ForEach(model.incidents) { item in
                    Button { selected = item } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 3) { Text(item.title).font(.headline); Text(item.status).font(.caption).foregroundStyle(.secondary) }
                            Spacer(); Image(systemName: "chevron.right").foregroundStyle(.secondary)
                        }.padding(.vertical, 6).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    Divider()
                }
                Button("打开 Obsidian 日志") {
                    let url = URL(fileURLWithPath: model.journal).appendingPathComponent("00 批评与改进索引.md")
                    guard FileManager.default.fileExists(atPath: url.path) else {
                        model.message = "日志文件暂时不可用，请刷新后再试。"; return
                    }
                    let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
                    guard let path = url.path.addingPercentEncoding(withAllowedCharacters: unreserved),
                          let target = URL(string: "obsidian://open?path=\(path)&paneType=tab"),
                          NSWorkspace.shared.open(target) else {
                        model.message = "暂时无法打开 Obsidian，请确认应用可用。日志仍保留在仓库中。"; return
                    }
                }.disabled(model.journal.isEmpty)
                Text("原因假设、修复安装与实际效果分别核对；没有新的批评不能证明问题已经解决。")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await model.refresh(runner) }
        .onChange(of: model.incidents.map { "\($0.id):\($0.revision)" }) { _, _ in
            if let current = selected, !model.incidents.contains(where: { $0.id == current.id && $0.revision == current.revision }) { selected = nil }
        }
        .sheet(item: $selected) { item in FeedbackDetailView(runner: runner, item: item) }
    }
}

private struct FeedbackDetailView: View {
    let runner: CoreRunner
    let item: FeedbackIncident
    @Environment(\.dismiss) private var dismiss
    @State private var quotes: [String] = []
    @State private var decisions: [[String: Any]] = []
    @State private var choice = ""
    @State private var preview = false
    @State private var message = ""
    @State private var busy = false
    @State private var verified = false
    private var proposal: [String: Any] {
        guard let raw = decisions.first?["proposal"] as? String else { return [:] }
        return FeedbackModel.decode(raw) ?? [:]
    }
    private var offeredChoices: [FeedbackPresentedChoice] { FeedbackDecisionPresentation.choices(proposal) }
    private var choiceLabel: String { offeredChoices.first { $0.id == choice }?.label ?? "" }
    private var choicePreview: String {
        if choice == "dismiss" { return "将本事项标为已排除；保留原话与历史记录。" }
        if choice == "defer" { return "保持待决策，不自动选择或修改程序。" }
        if choice == "investigate" { return "登记继续核查请求；不会因此扩大自动修复范围。" }
        return "记录你的选择：\(choiceLabel)。这一步仅保存选择及其对应版本，不表示方案已执行。"
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text(verified ? item.title : "正在核对事项").font(.title2.bold()); Spacer(); Button("关闭") { dismiss() } }
            if verified { ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(item.status).foregroundStyle(.secondary)
                    ForEach(Array(quotes.enumerated()), id: \.offset) { _, text in Text(text).padding(10).background(Color.secondary.opacity(0.08)).textSelection(.enabled) }
                    section("背景与依据", item.evidence)
                    section("原因判断", item.cause)
                    section("实际改进", item.fix.isEmpty ? "尚无可核实的修复记录。" : item.fix)
                    section("验证与剩余问题", item.gap)
                    if let decision = decisions.first {
                        if let proposal = decision["proposal"] as? String, let value = FeedbackModel.decode(proposal) {
                            section("需要你的决定", value["reason"] as? String ?? "待确认具体方案。")
                            section("建议与影响", value["recommendation"] as? String ?? "")
                            section("后果与限制", value["consequence"] as? String ?? "")
                            if let alternatives = value["alternatives"] as? [String], !alternatives.isEmpty {
                                section("可选方案", alternatives.enumerated().map { "\($0.offset + 1). \($0.element)" }.joined(separator: "\n"))
                                Text("选择后先预览影响；确认会保存你的答复，执行结果仍需单独核对。")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Picker("处理方式", selection: $choice) {
                            Text("尚未选择").tag("")
                            ForEach(offeredChoices) { option in Text(option.label).tag(option.id) }
                        }
                        Button("预览影响") { preview = true }.disabled(choice.isEmpty || busy)
                        if preview {
                            Text(choicePreview)
                            if let consequence = proposal["consequence"] as? String, !consequence.isEmpty { Text(consequence).foregroundStyle(.secondary) }
                            Button("确认此选择") { Task { await submit(decision) } }.disabled(busy)
                        }
                    }
                    if !message.isEmpty { Text(message).foregroundStyle(.secondary) }
                }.frame(maxWidth: .infinity, alignment: .leading)
            } }
        }.padding(24).frame(minWidth: 560, idealWidth: 680, minHeight: 440, idealHeight: 650)
        .task {
            if let result = FeedbackModel.decode(await runner.feedbackRequest("detail", payload: ["id": item.id])) {
                guard let current = result["incident"] as? [String: Any], current["id"] as? String == item.id else {
                    quotes = []; decisions = []; verified = false; dismiss(); return
                }
                quotes = (result["quotes"] as? [[String: Any]] ?? []).compactMap { $0["quote"] as? String }
                let currentState = (result["incident"] as? [String: Any])?["state"] as? String ?? item.phase
                decisions = FeedbackDecisionPresentation.pendingDecisions(result["decisions"] as? [[String: Any]] ?? [], state: currentState)
                verified = true
            }
        }
        .onChange(of: choice) { _, _ in preview = false }
        .onDisappear { quotes = []; decisions = [] }
    }
    private func section(_ title: String, _ content: String) -> some View {
        VStack(alignment: .leading, spacing: 4) { Text(title).font(.headline); Text(content.isEmpty ? "待核对。" : content).textSelection(.enabled) }
    }
    private func submit(_ decision: [String: Any]) async {
        busy = true; defer { busy = false }
        let payload: [String: Any] = ["id": decision["id"] ?? "", "revision": decision["revision"] ?? -1, "choice": choice]
        let value = FeedbackModel.decode(await runner.feedbackRequest("decision", payload: payload))
        if value?["status"] as? String == "recorded" { await FeedbackModel.shared.refresh(runner); dismiss() }
        else { message = value?["status"] as? String == "pending" ? "已暂缓，保持待决策。" : "事项已变化或提交未确认，请关闭后刷新重新核对。"; preview = false }
    }
}


struct FeedbackTodayCard: View {
    let runner: CoreRunner
    let open: () -> Void
    @ObservedObject private var model = FeedbackModel.shared
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Label("批评与改进", systemImage: "text.bubble")
                        .font(.headline)
                    Spacer()
                    Button("查看改进事项", action: open)
                }
                if let progress = model.progress {
                    Text(progress.label).font(.callout.weight(.medium))
                    Text("已收录 \(progress.total) 件 · 已分析 \(progress.analyzed) 条消息 · 待分析 \(progress.pending) 条")
                    Text("需要你选择 \(model.attentionCount) 件 · 已安装修复 \(progress.installed) 件 · 效果已验证 \(progress.verified) 件")
                    Text("最近成功分析：\(progress.lastSuccessLabel)").font(.caption).foregroundStyle(.secondary)
                    if progress.state == "retrying", progress.retryAt > 0 {
                        Text("下次重试：" + LocalStatusTime.label(Date(timeIntervalSince1970: progress.retryAt)))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if progress.state == "stale" || progress.state == "error" {
                        Text("旧产出仍保留，当前后台进度需要核对。")
                            .font(.caption).foregroundStyle(.orange)
                    }
                } else {
                    Text("状态暂不可读").font(.callout)
                    Text("无法确认当前进度；请稍后刷新。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
        }.task {
            while !Task.isCancelled {
                await model.refresh(runner)
                do { try await Task.sleep(nanoseconds: 30_000_000_000) } catch { break }
            }
        }
    }
}
