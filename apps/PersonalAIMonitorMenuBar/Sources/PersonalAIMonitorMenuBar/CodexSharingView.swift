import AppKit
import SwiftUI

@MainActor
final class CodexSharingWindow {
    static let shared = CodexSharingWindow()
    private var window: NSWindow?

    func show(runner: CoreRunner, consumer: ContextConsumer = .codex) {
        if let window {
            window.contentView = NSHostingView(rootView: CodexSharingView(runner: runner, consumer: consumer))
            window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return
        }
        let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 650),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        panel.title = "AGI · 共享上下文"
        panel.isReleasedWhenClosed = false
        panel.contentView = NSHostingView(rootView: CodexSharingView(runner: runner, consumer: consumer))
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
private struct CodexSharingView: View {
    let runner: CoreRunner
    @State var consumer: ContextConsumer = .codex
    @State private var snapshot: SharingSnapshot?
    @State private var busy = false
    @State private var message = ""
    @State private var project = ""
    @State private var summary = ""
    @State private var workstream = ""
    @State private var occurred = Date()
    @State private var ownStatement = false
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
            Picker("接收方", selection: $consumer) {
                ForEach(ContextConsumer.allCases) { Text($0.label).tag($0) }
            }.pickerStyle(.segmented).disabled(busy)
            Text(consumer == .agi ? "逐条选择‘供 AGI 自主进化使用’。仅用于本地线索整理，不向模型发送。" : "确认后还需点击‘允许 Codex 使用’。与 AGI 授权分别管理。")
                .font(.callout).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    GroupBox("本人补记") {
                        VStack(alignment: .leading, spacing: 9) {
                            TextField("项目", text: $project)
                            TextField("供共享的脱敏摘要（最多 80 字）", text: $summary)
                            TextField("工作主题", text: $workstream)
                            DatePicker("发生时间", selection: $occurred, in: ...Date(), displayedComponents: [.date, .hourAndMinute])
                            Toggle("这是我本人的说明，已去除敏感信息", isOn: $ownStatement)
                            Button("确认补记") { Task { await saveNote() } }
                                .disabled(busy || !ownStatement || project.trimmingCharacters(in: .whitespaces).isEmpty
                                          || summary.trimmingCharacters(in: .whitespaces).isEmpty || summary.count > 80)
                            Text("补记是新的用户说明，不证明历史屏幕活动或客户进展。保存后可在下面逐条授权。")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding(6)
                    }
                    if let snapshot {
                        if !snapshot.cards.isEmpty {
                            GroupBox("待本人核实") {
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
                        GroupBox("已确认 · 等待选择") {
                            VStack(alignment: .leading, spacing: 12) {
                                if snapshot.contexts.isEmpty { Text("暂无可共享的本人摘要。可以先补记，或核实已有卡片。").foregroundStyle(.secondary) }
                                ForEach(snapshot.contexts) { item in
                                    contextRow(item)
                                    Button(consumer == .agi ? "供 AGI 自主进化使用" : "允许 Codex 使用这一条") {
                                        Task { await action("share", ["references": [item.reference], "confirm_owner": true]) }
                                    }.disabled(busy)
                                    Divider()
                                }
                            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        GroupBox("已允许 \(consumer.label) 使用") {
                            VStack(alignment: .leading, spacing: 12) {
                                if snapshot.shares.isEmpty { Text("尚未授权任何摘要。").foregroundStyle(.secondary) }
                                ForEach(snapshot.shares) { pack in
                                    ForEach(pack.items) { item in contextRow(item) }
                                    Text("已提供 \(pack.providedCount) 次 · \(pack.consumerAcknowledged ? "实际引用已回执" : "尚无引用回执") · \(helpfulness(pack.ownerHelpfulness))")
                                        .font(.caption).foregroundStyle(.secondary)
                                    TextField("查找或续接用时（秒，可留空）", text: Binding(
                                        get: { lookupSeconds[pack.shareKey] ?? "" },
                                        set: { lookupSeconds[pack.shareKey] = $0 }))
                                        .frame(maxWidth: 250)
                                    HStack {
                                        Button("撤回") { Task { await action("withdraw", ["share_key": pack.shareKey, "confirm_owner": true]) } }
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
        .task { await refresh() }
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
    private func refresh() async { guard !busy else { return }; busy = true; defer { busy = false }; await load() }
    @discardableResult private func action(_ name: String, _ payload: [String: Any]) async -> Bool {
        guard !busy else { return false }; busy = true; defer { busy = false }
        let raw = await runner.contextSharing(consumer: consumer, action: name, payload: payload)
        let data = raw.data(using: .utf8) ?? Data()
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let status = result?["status"] as? String ?? ""
        let ok = ["shared", "confirmed", "withdrawn", "feedback_recorded"].contains(status)
        message = ok ? ["shared": "已允许 \(consumer.label) 按需使用所选版本。", "confirmed": "已确认，尚未共享。",
                         "withdrawn": "已撤回后续读取权限。", "feedback_recorded": "已记录你的评价。 "][status]! : "未完成：内容可能已变化、证据不足或服务正忙。请刷新后重试。"
        await load()
        if consumer == .agi && ok { NotificationCenter.default.post(name: Notification.Name("AGISharingChanged"), object: nil) }
        return ok
    }
    private func saveNote() async {
        let ok = await action("manual-note", ["summary": summary, "project": project, "workstream": workstream,
            "started_at": ISO8601DateFormatter().string(from: occurred), "request_id": requestID, "confirm_owner": ownStatement])
        if ok { summary = ""; ownStatement = false; requestID = UUID().uuidString }
    }
    private func correct(_ pack: SharedPackage) async {
        guard await action("withdraw", ["share_key": pack.shareKey, "confirm_owner": true]), let old = pack.items.first else { return }
        project = old.project; summary = old.summary; workstream = old.workstream; ownStatement = false
        requestID = UUID().uuidString
        message = "旧共享已撤回。请在上方修改说明，重新确认并授权；已发送到历史会话的副本无法自动追回。"
    }
    private func feedback(_ pack: SharedPackage, useful: Bool) async {
        var payload: [String: Any] = ["share_key": pack.shareKey, "useful": useful, "confirm_owner": true]
        let value = (lookupSeconds[pack.shareKey] ?? "").trimmingCharacters(in: .whitespaces)
        if !value.isEmpty {
            guard let seconds = Double(value), seconds.isFinite, seconds >= 0, seconds <= 86400 else {
                message = "用时请填写有效秒数，也可以留空。"; return
            }
            payload["resume_seconds"] = seconds
        }
        await action("feedback", payload)
    }
}
