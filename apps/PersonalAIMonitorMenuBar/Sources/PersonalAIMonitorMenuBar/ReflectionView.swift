import SwiftUI
import AppKit

struct ReflectionItem: Identifiable {
    let id: String
    let revision: Int
    let state: String
    let level: String
    let question: String
    let pattern: String
    let explanation: String
    let alternatives: String
    let refutation: String
    let distinguishingQuestion: String
    let reason: String
    let evidence: [[String: Any]]
}

@MainActor
final class ReflectionModel: ObservableObject {
    static let shared = ReflectionModel()
    @Published var items: [ReflectionItem] = []
    @Published var readable = false
    @Published var enabled = false
    @Published var paused = true
    @Published var busy = false
    @Published var message = ""
    @Published var sources: [String] = []
    @Published var pending = 0
    @Published var lastScan = "尚未读取"
    @Published var lastAnalysis = "尚未分析"
    @Published var analysisNote = ""
    @Published var calls = ""
    @Published var coverageGap = ""
    @Published var pipelineStatus = ""
    var active: [ReflectionItem] { (enabled && !paused ? items : []).filter { $0.state == "active" } }

    func refresh(_ runner: CoreRunner) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        let raw = await runner.reflectionRequest("status")
        guard let data = raw.data(using: .utf8),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              value["status"] as? String == "ok", value["contract"] as? String == "ReflectionSnapshotV1" else {
            readable = false; items = []; sources = []; enabled = false; paused = true
            pipelineStatus = "状态暂不可读取，不能据此判断没有待分析资料。"
            analysisNote = "上次分析暂不可读取，不能据此判断没有思路停滞。"
            message = "思考与反思尚未启用或暂不可读取，不能据此判断没有思路停滞。"; return
        }
        readable = true; enabled = value["enabled"] as? Bool ?? false
        paused = value["paused"] as? Bool ?? true; pending = value["pending_contexts"] as? Int ?? 0
        lastScan = Self.date(value["last_scan"])
        coverageGap = value["coverage_gap"] as? String ?? ""
        let pipeline = value["pipeline"] as? [String: Any] ?? [:]
        pipelineStatus = [pipeline["label"] as? String, pipeline["explanation"] as? String].compactMap { $0 }.joined(separator: "：")
        let analysis = value["last_analysis"] as? [String: Any] ?? [:]
        lastAnalysis = Self.date(analysis["at"])
        let levels = ["supported": "有较充分依据", "preliminary": "仅有初步线索", "not_found": "尚未发现"]
        analysisNote = analysis["status"] as? String == "unavailable"
            ? "上次分析的来源已退出或暂不可核对，摘要暂不展示。"
            : (analysis["publication_deferred"] as? Bool == true ? "发布暂缓" : (levels[analysis["judgment"] as? String ?? ""] ?? "尚未判断")) + "。" + (analysis["explanation"] as? String ?? "")
        message = (value["last_error"] as? String ?? "").isEmpty ? "" : "最近一次分析未完成，原进度与调用记录保留。"
        sources = (value["sources"] as? [[String: Any]] ?? []).map { row in
            let labels = ["current": "近期可访问记录已读取", "backfill": "分批读取中，覆盖尚不完整", "partial_identity": "本人来源已读取，部分身份未核实", "unavailable": "来源不可用，覆盖有缺口", "pending": "等待首次读取"]
            return "\(row["id"] as? String ?? "来源")：\(labels[row["status"] as? String ?? ""] ?? "待核对")"
        }
        calls = (value["calls"] as? [[String: Any]] ?? []).map { row in
            let labels = ["settled": "完成", "unknown": "结果不明", "running": "进行中", "validation_failed": "结果核验未通过"]
            return "\(labels[row["state"] as? String ?? ""] ?? "待核对") \(row["count"] as? Int ?? 0) 次"
        }.joined(separator: " · ")
        items = (value["insights"] as? [[String: Any]] ?? []).compactMap { row in
            guard let id = row["id"] as? String, let revision = row["revision"] as? Int else { return nil }
            return ReflectionItem(id: id, revision: revision, state: row["state"] as? String ?? "withdrawn",
                level: row["judgment_label"] as? String ?? "待核对", question: row["question"] as? String ?? "",
                pattern: row["pattern"] as? String ?? "", explanation: row["explanation"] as? String ?? "",
                alternatives: row["alternatives"] as? String ?? "", refutation: row["refutation"] as? String ?? "",
                distinguishingQuestion: row["distinguishing_question"] as? String ?? "", reason: row["reason"] as? String ?? "",
                evidence: row["evidence"] as? [[String: Any]] ?? [])
        }
    }

    func action(_ runner: CoreRunner, _ action: String, payload: [String: Any] = [:]) async {
        guard !busy else { return }; busy = true
        let raw = await runner.reflectionRequest(action, payload: payload)
        busy = false
        await refresh(runner)
        if raw.contains("\"status\":\"queued\"") { message = "已排队，后台会在下一可用分析时段处理。" }
        else if raw.contains("\"status\":\"unavailable\"") { message = "本次操作未完成；状态可能已变化，请刷新核对。" }
    }

    static func date(_ value: Any?) -> String {
        guard let stamp = value as? Double else { return "尚无记录" }
        return Date(timeIntervalSince1970: stamp).formatted(date: .abbreviated, time: .shortened)
    }
}

struct ReflectionView: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject private var model = ReflectionModel.shared
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("思考与反思").font(.largeTitle.weight(.semibold))
                Text("帮助你看清目标、依据和判断标准，发现可能的停滞，并找到可验证的下一步。")
                    .foregroundStyle(.secondary)
                HStack {
                    Button("刷新") { Task { await model.refresh(runner) } }
                    Button("分析近期讨论") { Task { await model.action(runner, "analyze") } }
                        .disabled(!model.readable || !model.enabled || model.paused)
                    Spacer()
                    Button(model.enabled ? "关闭反思分析" : "开启反思分析") {
                        Task { await model.action(runner, "configure", payload: ["enabled": !model.enabled]) }
                    }
                }.disabled(model.busy)
                if !model.message.isEmpty { Text(model.message).foregroundStyle(.secondary) }
                if model.readable {
                    if !model.pipelineStatus.isEmpty { Text(model.pipelineStatus).font(.callout).foregroundStyle(.secondary) }
                    Text(model.enabled ? (model.paused ? "已暂停，停止新读取与分析。" : "已开启 · 最近14天 · 有依据时主动提醒") : "已关闭，历史判断与更正记录保留。")
                        .font(.callout)
                    if model.active.isEmpty {
                        Text(model.analysisNote).padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
                    }
                    ForEach(model.active) { item in reflectionCard(item) }
                    DisclosureGroup("来源与处理进度") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("最近读取：\(model.lastScan)　最近分析：\(model.lastAnalysis)")
                            Text("待处理讨论：\(model.pending)；分批结果不代表全部近期对话已覆盖。")
                            if !model.coverageGap.isEmpty { Text(model.coverageGap) }
                            ForEach(model.sources, id: \.self) { Text($0) }
                            if !model.calls.isEmpty { Text("本功能调用：\(model.calls)") }
                        }.font(.caption).foregroundStyle(.secondary).padding(.top, 8)
                    }
                    if model.items.contains(where: { $0.state != "active" || !model.enabled || model.paused }) {
                        DisclosureGroup("撤回与暂不提醒的记录") {
                            ForEach(model.items.filter { $0.state != "active" || !model.enabled || model.paused }) { item in
                                VStack(alignment: .leading) { Text(item.question); Text(item.reason).font(.caption).foregroundStyle(.secondary) }.padding(.vertical, 6)
                            }
                        }
                    }
                    Text("反思是待检验的假设。至少七天与三次独立自然使用案例后另行评估；觉得被说中不证明正确。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.padding(28).frame(maxWidth: 920, alignment: .leading).frame(maxWidth: .infinity)
        }.task { await model.refresh(runner) }
    }

    private func reflectionCard(_ item: ReflectionItem) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(item.level).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text("1. 更深的问题是什么").font(.headline)
            Text(item.question).font(.title3.weight(.semibold))
            Text(item.explanation)
            Text("2. 怎么发现的").font(.headline)
            Text(item.pattern)
            DisclosureGroup("查看具体对话线索") {
                ForEach(Array(item.evidence.enumerated()), id: \.offset) { _, evidence in
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(evidence["platform"] as? String ?? "") · \(evidence["attribution"] as? String == "material" ? "引用材料" : (evidence["role"] as? String == "user" ? "本人陈述" : "AI回答")) · \(ReflectionModel.date(evidence["observed"]))").font(.caption).foregroundStyle(.secondary)
                        Text(evidence["quote"] as? String ?? "").textSelection(.enabled)
                        Text("讨论标识：\(evidence["session"] as? String ?? "")").font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
                    }.padding(.vertical, 6)
                }
            }
            Text("3. 可能错在哪里").font(.headline)
            Text(item.alternatives)
            Text(item.refutation)
            Text(item.distinguishingQuestion).fontWeight(.medium)
            HStack {
                feedbackButton("有帮助", "helpful", item)
                feedbackButton("判断不准确", "inaccurate", item)
                feedbackButton("暂不提醒这个问题", "mute", item)
            }.disabled(model.busy)
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
    }

    private func feedbackButton(_ title: String, _ choice: String, _ item: ReflectionItem) -> some View {
        Button(title) { Task { await model.action(runner, "feedback", payload: ["id": item.id, "revision": item.revision, "choice": choice]) } }
    }
}

struct ReflectionTodayCard: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject private var model = ReflectionModel.shared
    let open: () -> Void
    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 8) {
                Label("思考与反思", systemImage: "lightbulb").font(.headline)
                Text(model.active.first?.question ?? (model.readable ? (!model.enabled ? "已关闭反思分析，历史记录保留。" : (model.paused ? "已暂停，停止新读取与分析。" : model.analysisNote)) : "查看近期讨论的依据、判断标准和可能的停滞。"))
                    .lineLimit(3).foregroundStyle(.secondary)
                Text("查看依据与其他解释").font(.caption).foregroundStyle(Color.accentColor)
            }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
        }.buttonStyle(.plain).task { await model.refresh(runner) }
    }
}
