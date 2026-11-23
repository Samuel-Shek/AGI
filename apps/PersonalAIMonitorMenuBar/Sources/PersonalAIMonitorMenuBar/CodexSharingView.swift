import AppKit
import SwiftUI
import PAMInteractionModel

@MainActor
final class CodexSharingWindow {
    static let shared = CodexSharingWindow()
    private var window: NSWindow?
    private let selection = SharingSelection()

    func show(runner: CoreRunner, consumer: ContextConsumer = .codex) {
        selection.consumer = consumer
        if let window {
            // Reuse the mounted editor and its pending inputs on reopen.
            window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return
        }
        let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 650),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        panel.title = "PSI · 共享上下文"
        panel.isReleasedWhenClosed = false
        panel.contentView = NSHostingView(rootView: CodexSharingView(runner: runner, selection: selection))
        // The first screen contains the menu bar; prefer another physical screen.
        if let screen = NSScreen.screens.dropFirst().first {
            let frame = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: frame.midX - 380, y: frame.midY - 325))
        } else { panel.center() }
        window = panel
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

private struct SharedContext: Decodable, Identifiable {
    let reference: String
    let project: String
    let summary: String
    let workstream: String
    let occurredAt: String
    let expiresAt: String
    let sourceKind: String
    var id: String { reference }
    var origin: String { sourceKind == "owner_manual_note" ? "本人补记 · 用户说明，未由屏幕核实" : "本人确认的活动摘要" }
}
private struct SharedPackage: Decodable, Identifiable {
    let shareKey: String
    let items: [SharedContext]
    let consumerAcknowledged: Bool
    let providedCount: Int
    let ownerHelpfulness: String?
    var id: String { shareKey }
}
private struct PendingContextCard: Decodable, Identifiable {
    let confirmationRef: String
    let revision: String
    let title: String
    let question: String
    let timeLabel: String
    var id: String { confirmationRef }
}
private struct SharingSnapshot: Decodable {
    let status: String
    let contexts: [SharedContext]
    let shares: [SharedPackage]
    let cards: [PendingContextCard]
}

@MainActor
private final class SharingSelection: ObservableObject {
    @Published var consumer: ContextConsumer = .codex
}

@MainActor
private struct CodexSharingView: View {
    let runner: CoreRunner
    @ObservedObject var selection: SharingSelection
    private var consumer: ContextConsumer { selection.consumer }
    @State private var snapshot: SharingSnapshot?
    @State private var busy = false
    @State private var message = ""
    @RetainedDraft("sharing-note", "project") private var project = ""
    @RetainedDraft("sharing-note", "summary") private var summary = ""
    @RetainedDraft("sharing-note", "workstream") private var workstream = ""
    @RetainedDraft("sharing-note", "occurred") private var occurred = Date()
    @RetainedDraft("sharing-note", "ownStatement") private var ownStatement = false
    @State private var requestID = UUID().uuidString
    @State private var lookupSeconds: [String: String] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("只共享你选中的摘要").font(.title2.bold())
                Spacer()
                if busy { ProgressView().controlSize(.small) }
                Button("刷新") { Task { await refresh() } }.disabled(busy)
            }
            Picker("接收方", selection: $selection.consumer) {
                ForEach(ContextConsumer.allCases) { Text($0.label).tag($0) }
            }.pickerStyle(.segmented).disabled(busy)
            Text(consumer == .agi ? "逐条选择‘供 PSI 自主进化使用’。仅用于本地线索整理，不向模型发送。" : "确认后还需点击‘允许 Codex 使用’。与 PSI 授权分别管理。")
                .font(.callout).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    AGISectionBox("本人补记") {
                        VStack(alignment: .leading, spacing: 9) {
                            TextField("项目", text: $project)
                            TextField("供共享的脱敏摘要（最多 80 字）", text: $summary)
                            TextField("工作主题", text: $workstream)
                            DatePicker("发生时间", selection: $occurred, in: ...Date(), displayedComponents: [.date, .hourAndMinute])
                            Toggle("这是我本人的说明，已去除敏感信息", isOn: $ownStatement)
                            Button("放弃未提交的补记") { PublicationDrafts.shared.discard("sharing-note") }.disabled(busy)
                            Button("确认补记") { Task { await saveNote() } }
                                .disabled(busy || !ownStatement || project.trimmingCharacters(in: .whitespaces).isEmpty
                                          || summary.trimmingCharacters(in: .whitespaces).isEmpty || summary.count > 80)
                            Text("补记是新的用户说明，不证明历史屏幕活动或客户进展。保存后可在下面逐条授权。")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding(6)
                    }
                    if let snapshot {
                        if !snapshot.cards.isEmpty {
                            AGISectionBox("待本人核实") {
                                VStack(alignment: .leading, spacing: 12) {
                                    ForEach(snapshot.cards) { card in
                                        VStack(alignment: .leading, spacing: 4) {
                                            Text(card.title).font(.headline)
                                            Text(card.question)
                                            Text(card.timeLabel).font(.caption).foregroundStyle(.secondary)
                                            Button("确认是我本人的活动") {
                                                Task { await action("confirm-card", ["confirmation_ref": card.confirmationRef,
                                                    "expected_revision": card.revision, "confirm_owner": true]) }
                                            }.disabled(busy)
                                            Text("信息不足时继续待核实；确认本身不授权共享。")
                                                .font(.caption).foregroundStyle(.secondary)
                                        }
                                        Divider()
                                    }
                                }.padding(6)
                            }
                        }
                        AGISectionBox("已确认 · 等待选择") {
                            VStack(alignment: .leading, spacing: 12) {
                                if snapshot.contexts.isEmpty { Text("暂无可共享的本人摘要。可以先补记，或核实已有卡片。").foregroundStyle(.secondary) }
                                ForEach(snapshot.contexts) { item in
                                    contextRow(item)
                                    Button(consumer == .agi ? "供 PSI 自主进化使用" : "允许 Codex 使用这一条") {
                                        Task { await action("share", ["references": [item.reference], "confirm_owner": true]) }
                                    }.disabled(busy)
                                    Divider()
                                }
                            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        AGISectionBox("已允许 \(consumer.label) 使用") {
                            VStack(alignment: .leading, spacing: 12) {
                                if snapshot.shares.isEmpty { Text("尚未授权任何摘要。").foregroundStyle(.secondary) }
                                ForEach(snapshot.shares) { pack in
                                    ForEach(pack.items) { item in contextRow(item) }
                                    Text("已提供 \(pack.providedCount) 次 · \(pack.consumerAcknowledged ? "实际引用已回执" : "尚无引用回执") · \(helpfulness(pack.ownerHelpfulness))")
                                        .font(.caption).foregroundStyle(.secondary)
                                    TextField("查找或续接用时（秒，可留空）", text: Binding(
                                        get: { lookupSeconds[feedbackScope(pack)] ?? "" },
                                        set: {
                                            let scope = feedbackScope(pack)
                                            lookupSeconds[scope] = $0
                                            PublicationDrafts.shared.update(scope + "/seconds", initial: "", value: $0)
                                        }))
                                        .frame(maxWidth: 250)
                                    Button("放弃未提交的评价用时") {
                                        let scope = feedbackScope(pack)
                                        lookupSeconds[scope] = PublicationDrafts.shared.discardValue(scope + "/seconds", initial: "")
                                    }.disabled(busy)
                                    HStack {
                                        Button("撤回") { Task { await withdraw(pack) } }
                                        Button("纠正…") { Task { await correct(pack) } }
                                        Button("有帮助") { Task { await feedback(pack, useful: true) } }
                                        Button("没帮助") { Task { await feedback(pack, useful: false) } }
                                    }.disabled(busy)
                                    Divider()
                                }
                            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            if !message.isEmpty { Text(message).font(.callout).textSelection(.enabled) }
            Text("撤回会阻止再次读取；已进入 Codex 历史对话的副本无法自动收回。纠正先撤回旧共享，再保存新说明并重新授权。")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20).frame(minWidth: 650, minHeight: 500)
        .task {
            if PublicationDrafts.shared.isClean("sharing-note") { PublicationDrafts.shared.initialize("sharing-note/occurred", value: Date()) }
            await PublicationDrafts.shared.retry("sharing-note")
            await refresh()
        }
        .onChange(of: consumer) { _, _ in snapshot = nil; message = ""; Task { await refresh() } }
    }

    private func helpfulness(_ value: String?) -> String {
        switch value { case "helpful": return "你认为有帮助"; case "not_helpful": return "你认为无帮助"; default: return "是否有帮助待你评价" }
    }
    private func contextRow(_ item: SharedContext) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(item.project + " · " + item.summary).font(.headline)
            Text(item.origin).font(.caption).foregroundStyle(.secondary)
            Text("发生：\(displayTime(item.occurredAt))  ·  有效至：\(displayTime(item.expiresAt))")
                .font(.caption).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
    private func displayTime(_ value: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value) else { return "待确认" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
    private func load() async {
        let raw = await runner.contextSharing(consumer: consumer, action: "snapshot", payload: [:])
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let data = raw.data(using: .utf8), let value = try? decoder.decode(SharingSnapshot.self, from: data), value.status == "ok" else {
            snapshot = nil; message = "暂时无法读取，请稍后刷新。未改变共享授权。"; return
        }
        snapshot = value
    }
    private func refresh() async {
        guard !busy else { return }; busy = true; defer { busy = false }
        await PublicationDrafts.shared.retry("sharing-note")
        for name in ["confirm-card", "share", "withdraw"] { await PublicationDrafts.shared.retry("sharing-action." + consumer.rawValue + "." + name) }
        for pack in snapshot?.shares ?? [] { await PublicationDrafts.shared.retry(feedbackScope(pack)) }
        await load()
    }
    @discardableResult private func action(_ name: String, _ payload: [String: Any], ticket: PublicationDrafts.Ticket? = nil) async -> Bool {
        guard !busy else { return false }; busy = true; defer { busy = false }
        let recipient = consumer
        let scope = "sharing-action." + recipient.rawValue + "." + name
        if ticket == nil && PublicationDrafts.shared.needsReadback(scope) {
            await PublicationDrafts.shared.retry(scope); return false
        }
        let operation = ticket ?? PublicationDrafts.shared.begin("sharing-action." + recipient.rawValue + "." + name)
        let raw = await runner.contextSharing(consumer: recipient, action: name, payload: payload)
        let data = raw.data(using: .utf8) ?? Data()
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let status = result?["status"] as? String ?? ""
        var ok = ["shared", "confirmed", "withdrawn", "feedback_recorded"].contains(status) && result?["raw_access"] as? Bool == false
        let outcome: PublicationDraftState.Status = ok ? .clean : PublicationDrafts.mutation(raw)
        PublicationDrafts.shared.finish(operation, outcome)
        if outcome == .unknown && ["manual-note", "feedback", "confirm-card", "share", "withdraw"].contains(name) {
            await PublicationDrafts.shared.verify(operation) {
                let receipt = await runner.contextSharing(consumer: recipient, action: name + "-receipt", payload: payload)
                return PublicationDrafts.readback(receipt, interface: ["manual-note", "confirm-card"].contains(name) ? "owner-action-receipt" : name == "feedback" ? "sharing-feedback-receipt" : "sharing-action-receipt")
            }
        }
        ok = ticket == nil ? operation.outcome == .clean : ticket?.outcome == .clean
        message = ok ? ["shared": "已允许 \(consumer.label) 按需使用所选版本。", "confirmed": "已确认，尚未共享。",
                         "withdrawn": "已撤回后续读取权限。", "feedback_recorded": "已记录你的评价。 "][status, default: "已核对本次保存回执。"] : "未完成：内容可能已变化、证据不足或服务正忙。请刷新后重试。"
        await load()
        if consumer == .agi && ok { NotificationCenter.default.post(name: Notification.Name("AGISharingChanged"), object: nil) }
        return ok
    }
    private func saveNote() async {
        guard !busy else { return }
        if PublicationDrafts.shared.needsReadback("sharing-note") { await PublicationDrafts.shared.retry("sharing-note"); return }
        requestID = UUID().uuidString
        let ticket = PublicationDrafts.shared.begin("sharing-note")
        let ok = await action("manual-note", ["summary": summary, "project": project, "workstream": workstream,
            "started_at": ISO8601DateFormatter().string(from: occurred), "request_id": requestID, "confirm_owner": ownStatement], ticket: ticket)
        let current = PublicationDrafts.shared.isCurrent(ticket)
        if !ok {
            PublicationDrafts.shared.completeAfterReadback(ticket, scope: "sharing-note") { message = "已核对补记保存成功，尚未共享。" }
        }
        if ok && current {
            PublicationDrafts.shared.resetSaved("sharing-note")
            PublicationDrafts.shared.initialize("sharing-note/occurred", value: Date())
            requestID = UUID().uuidString
        }
    }
    @discardableResult private func withdraw(_ pack: SharedPackage) async -> Bool {
        guard PublicationDrafts.shared.isClean(feedbackScope(pack)) else {
            message = "这条共享还有未提交的评价用时或待核对的保存。请先提交、明确放弃用时，或刷新核对后再撤回。"; return false
        }
        return await action("withdraw", ["share_key":pack.shareKey, "confirm_owner":true])
    }
    private func correct(_ pack: SharedPackage) async {
        guard PublicationDrafts.shared.isClean("sharing-note") else {
            message = "上方还有未处理的补记。请先保存或明确放弃，再修改这条共享。"; return
        }
        guard await withdraw(pack), let old = pack.items.first else { return }
        project = old.project; summary = old.summary; workstream = old.workstream; ownStatement = false
        requestID = UUID().uuidString
        message = "旧共享已撤回。请在上方修改说明，重新确认并授权；已发送到历史会话的副本无法自动追回。"
    }
    private func feedbackScope(_ pack: SharedPackage) -> String { "sharing-feedback." + consumer.rawValue + "." + pack.shareKey }
    private func feedback(_ pack: SharedPackage, useful: Bool) async {
        guard !busy else { return }
        let scope = feedbackScope(pack)
        if PublicationDrafts.shared.needsReadback(scope) { await PublicationDrafts.shared.retry(scope); return }
        var payload: [String: Any] = ["share_key": pack.shareKey, "useful": useful, "confirm_owner": true]
        let value = (lookupSeconds[scope] ?? "").trimmingCharacters(in: .whitespaces)
        if !value.isEmpty {
            guard let seconds = Double(value), seconds.isFinite, seconds >= 0, seconds <= 86400 else {
                message = "用时请填写有效秒数，也可以留空。"; return
            }
            payload["resume_seconds"] = seconds
        }
        let ticket = PublicationDrafts.shared.begin(scope)
        _ = await action("feedback", payload, ticket: ticket)
    }
}
