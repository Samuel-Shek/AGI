import SwiftUI
import AppKit
import PAMInteractionModel

@MainActor
final class AttentionCenterModel: ObservableObject {
    @Published var snapshot = AttentionSnapshot()
    @Published var loading = false
    @Published var snapshotStale = true
    @Published var selected: AttentionIssue?
    @Published var presentation: AttentionPresentation?
    @Published var detailMessage = ""
    @Published var detailLoading = false
    @Published var detailReadID = UUID()
    @Published var submissions = AttentionSubmissions()
    @Published var actionMessage = ""
    @Published var openingClosureID: String?
    @Published var closureOpenMessages: [String: String] = [:]
    @Published var handoffBusy: Set<String> = []
    @Published var handoffUncertain: Set<String> = []
    private var handoffEpochs: [String: UInt64] = [:]
    @Published var handoffDrafts = AttentionHandoffDraftCache()
    weak var runner: CoreRunner?
    private var selectionGeneration = UUID()
    private var refreshReads = AttentionRefreshReads()
    private var retryTask: Task<Void, Never>?
    private var readRetryDelay: UInt64 = 5

    func connect(_ runner: CoreRunner) { self.runner = runner }
    func refresh(coalescingIfLoading: Bool = false) async {
        guard let runner else { return }
        guard let generation = refreshReads.request(coalescingIfLoading: coalescingIfLoading) else { return }
        retryTask?.cancel(); retryTask = nil
        loading = true
        runner.clearRevealedMemory()
        let next = AttentionSnapshot(jsonText: await runner.runCore(["attention-center", "snapshot"]))
        guard refreshReads.isCurrent(generation) else { return }
        snapshotStale = next.status != "ok"
        let scopeChanged = !next.displayScopeRevision.isEmpty && next.displayScopeRevision != snapshot.displayScopeRevision
        if next.displayScopeWithdrawn || scopeChanged || (!next.displayScopeRevision.isEmpty && snapshotStale) {
            close()
            handoffDrafts.withdrawAll(); closureOpenMessages = [:]
            snapshot = next
        } else {
            snapshot = next.retainingUnreadRows(from: snapshot)
        }
        if let context = presentation {
            if snapshotStale {
                selectionGeneration = UUID(); detailLoading = false
                detailMessage = "部分资料暂不可读取；已读内容保留，核对完成后才能处理。"
                detailReadID = UUID()
            } else {
                await reload(context)
            }
        }
        guard refreshReads.isCurrent(generation) else { return }
        let followUp = refreshReads.finish(generation)
        loading = false
        if !snapshotStale { readRetryDelay = 5 }
        if followUp {
            await refresh()
            return
        }
        if snapshotStale && NSApp.isActive {
            let delay = readRetryDelay
            readRetryDelay = min(30, delay * 2)
            // Retry only the read. Pending decisions retain their state and
            // require the owner's actual reply regardless of elapsed time.
            retryTask = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: delay * 1_000_000_000) }
                catch { return }
                guard !Task.isCancelled, let self else { return }
                self.retryTask = nil
                guard NSApp.isActive else { return }
                await self.refresh()
            }
        }
    }
    func cancelReadRetry() {
        retryTask?.cancel(); retryTask = nil
    }
    func handoffReadEpoch(_ id: String) -> UInt64 { handoffEpochs[id, default: 0] }
    func beginHandoff(_ id: String) -> Bool {
        guard !handoffBusy.contains(id), !handoffUncertain.contains(id) else { return false }
        handoffBusy.insert(id); handoffEpochs[id, default: 0] += 1
        return true
    }
    @discardableResult func finishHandoff(_ id: String, response: [String: Any]) -> Bool {
        handoffBusy.remove(id); handoffEpochs[id, default: 0] += 1
        let verified = AttentionHandoffReceipt.verified(response)
        if verified { handoffUncertain.remove(id) } else { handoffUncertain.insert(id) }
        if AttentionHandoffReceipt.sourceWithdrawn(response) { withdrawSource(id) }
        return verified
    }
    func acceptHandoffRead(_ id: String, epoch: UInt64, response: [String: Any]) -> Bool {
        if AttentionHandoffReceipt.sourceWithdrawn(response) { withdrawSource(id); return false }
        guard handoffReadEpoch(id) == epoch, !handoffBusy.contains(id) else { return false }
        let verified = AttentionHandoffReceipt.verified(response)
        if verified { handoffUncertain.remove(id) } else { handoffUncertain.insert(id) }
        return verified
    }
    private func withdrawSource(_ id: String) {
        // An already-generated snapshot may still be in transit. It must not
        // put the withdrawn source back after this command receipt arrives.
        refreshReads.invalidate(); loading = false
        cancelReadRetry()
        handoffDrafts.withdraw(id); closureOpenMessages.removeValue(forKey: id)
        handoffEpochs[id, default: 0] += 1
        let selectedWasWithdrawn = selected.map { item in
            var selectedSnapshot = AttentionSnapshot(); selectedSnapshot.decisions = [item]
            return selectedSnapshot.workbenchItem(id) != nil
        } ?? false
        snapshot = snapshot.withdrawingItem(id); snapshotStale = true
        if selectedWasWithdrawn { close() }
    }
    func canInteract(_ context: AttentionPresentation) -> Bool {
        isCurrent(context) && !loading && !snapshotStale && !detailLoading && detailMessage.isEmpty
            && selected?.closureCurrentVerification != "unknown"
    }
    func isCurrent(_ context: AttentionPresentation) -> Bool {
        presentation?.owns(context) == true && selected?.id == context.issueID && selected?.revision == context.revision
    }
    func select(_ item: AttentionIssue, mode: AttentionPresentationMode = .sheet, anchorID: String? = nil) async {
        let context = AttentionPresentation(issueID: item.id, revision: item.revision, mode: mode, anchorID: anchorID)
        runner?.clearRevealedMemory()
        presentation = context; selected = item; actionMessage = ""
        await reload(context)
    }
    func openClosureCodex(_ item: AttentionIssue, assistance: Bool = false) async {
        guard openingClosureID == nil, !loading, !snapshotStale,
              let context = presentation, context.matches(issueID: item.id, revision: item.revision), canInteract(context) else { return }
        openingClosureID = item.id; closureOpenMessages[item.id] = "正在打开 Codex…"
        defer { openingClosureID = nil }
        let response = await command("closure-open", id: item.id,
            payload: ["revision": item.revision, "thread_id": assistance ? item.closureAssistanceThreadID : item.closureThreadID, "assistance": assistance])
        guard isCurrent(context) else { return }
        if response["status"] as? String == "ok" {
            closureOpenMessages[item.id] = assistance ? "已请求打开协助聊天。" : "已请求打开负责聊天。"
        } else {
            closureOpenMessages[item.id] = response["error"] as? String ?? "Codex 暂时无法打开；事项仍保留。"
            if let value = response["item"] as? [String: Any], let updated = AttentionIssue(value),
               updated.id == item.id, updated.revision == item.revision {
                snapshot.decisions = snapshot.decisions.map { $0.id == updated.id ? updated : $0 }
                snapshot.closures = snapshot.closures.map { $0.id == updated.id ? updated : $0 }
                if selected?.id == updated.id && selected?.revision == updated.revision { selected = updated }
            }
        }
    }
    func toggleInline(_ item: AttentionIssue) async {
        if let current = presentation, current.mode == .inline, current.anchorID == item.id {
            close(presentationID: current.id)
        } else { await select(item, mode: .inline) }
    }
    func reload(_ context: AttentionPresentation) async {
        guard isCurrent(context) else { return }
        let token = UUID(); selectionGeneration = token
        detailMessage = ""; detailLoading = true
        guard let runner else { detailLoading = false; detailMessage = "服务尚未就绪，请重新读取。"; return }
        let raw = await runner.runCore(["attention-center", "detail", "--id", context.issueID])
        guard selectionGeneration == token, isCurrent(context) else { return }
        if let value = Self.object(raw), value["status"] as? String == "changed" || ["display_scope_unavailable", "source_not_in_display_scope"].contains(value["reason"] as? String ?? "") {
            withdrawSource(context.issueID)
            return
        }
        if let value = Self.object(raw), value["status"] as? String == "ok",
           let object = value["item"] as? [String: Any], let updated = AttentionIssue(object), updated.id == context.issueID {
            if let listed = snapshot.workbenchItem(updated.id), listed.revision != updated.revision {
                detailLoading = false
                detailMessage = "列表与详情版本尚未一致；已读内容保留，请重新读取后处理。"
                detailReadID = UUID()
                return
            }
            var revised = context; revised.revision = updated.revision
            presentation = revised; selected = updated
            let cardState = (object["card"] as? [String: Any])?["state"] as? String ?? ""
            let deferredMembers = Set((object["members"] as? [[String: Any]] ?? []).compactMap { member -> String? in
                guard updated.kind == "memory", member["current_decision"] as? String == "review_later" else { return nil }
                return member["id"] as? String
            })
            submissions.verifiedRead(id: updated.id, revision: updated.revision,
                unresolvedActivity: updated.kind == "activity" && ["pending", "deferred"].contains(cardState), reviewLaterMembers: deferredMembers)
            detailReadID = UUID()
        } else {
            detailMessage = "事项已经变化或暂不可读取，请重新读取后再处理。"
        }
        detailLoading = false
    }
    func close(presentationID: UUID? = nil) {
        if let presentationID, presentation?.id != presentationID { return }
        runner?.clearRevealedMemory()
        selectionGeneration = UUID(); presentation = nil; selected = nil
        detailMessage = ""; detailLoading = false
    }
    func beginSubmission(_ context: AttentionPresentation) -> Bool {
        guard canInteract(context) else { return false }
        return submissions.begin(AttentionSubmissions.key(context.issueID, context.revision))
    }
    func finishSubmission(_ context: AttentionPresentation, raw: String, outcome: AttentionWriteOutcome, deferred: Bool = false, reviewLaterMembers: Set<String> = []) {
        submissions.finish(AttentionSubmissions.key(context.issueID, context.revision), receipt: raw, outcome: outcome, deferred: deferred, reviewLaterMembers: reviewLaterMembers)
        guard isCurrent(context) else { return }
        switch outcome {
        case .recorded: actionMessage = "处理请求已有回执，请以最新事项状态为准。"
        case .rejected: actionMessage = "本次操作未执行，正在核对最新资料；需要你重新选择。"
        case .unknown: actionMessage = "操作回执暂不可读取，请先核对状态；不会自动重发。"
        }
    }
    static func object(_ raw: String) -> [String: Any]? {
        guard let data = raw.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }
    func command(_ action: String, id: String = "", payload: [String: Any]? = nil) async -> [String: Any] {
        (await commandResult(action, id: id, payload: payload)).value
    }
    func commandResult(_ action: String, id: String = "", payload: [String: Any]? = nil) async -> (raw: String, value: [String: Any]) {
        guard let runner else { return ("", ["error":"服务尚未就绪。", "outcome_unknown":true]) }
        var args = ["attention-center", action]
        if !id.isEmpty { args += ["--id", id] }
        let input = payload.flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { String(data: $0, encoding: .utf8) }
        let raw = await runner.runCore(args, standardInput: input)
        let value = Self.object(raw) ?? ["error":"操作结果暂不可读取，请先核对状态。", "outcome_unknown":true]
        if AttentionHandoffReceipt.sourceWithdrawn(value) { withdrawSource(id) }
        return (raw, value)
    }
}

struct AttentionCenterView: View {
    @ObservedObject var model: AttentionCenterModel
    let openReferences: () -> Void
    @State private var scope: AttentionWorkbenchScope = .decisions
    @State private var listPosition: String?
    private var rows: [AttentionIssue] { scope.rows(in: model.snapshot) }
    private var paneSelected: Bool { model.presentation?.mode == .pane }

    var body: some View {
        GeometryReader { geometry in
            let compact = geometry.size.width < 760
            let width = max(0, geometry.size.width - 40)
            let listWidth = compact ? (paneSelected ? 0 : width) : 280.0
            let detailWidth = compact ? (paneSelected ? width : 0) : max(0, width - listWidth - 1)
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("需要你决定").font(.largeTitle.weight(.semibold))
                        Text("选择事项，查看背景和处理方式。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { Task { await model.refresh() } } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }.accessibilityIdentifier("attention-workbench-refresh")
                        .help("重新核对事项及当前详情").disabled(model.loading)
                }
                HStack(spacing: 6) {
                    ForEach(AttentionWorkbenchScope.allCases) { item in
                        Button {
                            model.close(); scope = item; listPosition = nil
                        } label: {
                            Text(item.title + " " + (model.snapshotStale ? "—" : String(item.rows(in: model.snapshot).count)))
                                .font(.callout.weight(scope == item ? .semibold : .regular))
                                .padding(.horizontal, 10).padding(.vertical, 8)
                                .background(scope == item ? Color.accentColor.opacity(0.14) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
                                .overlay(RoundedRectangle(cornerRadius: 8).stroke(scope == item ? Color.accentColor.opacity(0.35) : Color.secondary.opacity(0.2)))
                        }.buttonStyle(.plain).accessibilityIdentifier("attention-scope-" + item.rawValue)
                            .accessibilityValue(scope == item ? "已选中" : "")
                    }
                    Spacer(minLength: 0)
                }
                if model.loading {
                    HStack(spacing: 8) { ProgressView().controlSize(.small); Text("正在核对事项…").font(.callout).foregroundStyle(.secondary) }
                } else if model.snapshotStale {
                    Text("资料暂未完整读取，已读事项保留；数量和处理操作待核对。")
                        .font(.callout).foregroundStyle(.orange)
                }
                // Both scroll containers remain mounted while the window changes size.
                HStack(spacing: 0) {
                    masterPane
                        .frame(width: listWidth).clipped()
                        .opacity(listWidth == 0 ? 0 : 1).allowsHitTesting(listWidth > 0).accessibilityHidden(listWidth == 0)
                    Rectangle().fill(Color.secondary.opacity(0.2)).frame(width: compact ? 0 : 1)
                    detailPane(compact: compact)
                        .frame(width: detailWidth).clipped()
                        .opacity(detailWidth == 0 ? 0 : 1).allowsHitTesting(detailWidth > 0).accessibilityHidden(detailWidth == 0)
                }.frame(maxHeight: .infinity)
                    .background(Color(NSColor.controlBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.secondary.opacity(0.2)))
                Button("查看历史待答复与资料", action: openReferences).buttonStyle(.link)
            }.padding(20)
        }
        .task { await model.refresh() }
        .onDisappear {
            if let context = model.presentation, context.mode == .pane { model.close(presentationID: context.id) }
        }
    }
    private var masterPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text(scope.title).font(.headline)
                Text(model.snapshotStale ? "已读 \(rows.count) 项 · 状态待核对" : "共 \(rows.count) 项")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(16)
            Divider()
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(rows) { issue in
                        Button { Task { await model.select(issue, mode: .pane) } } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(issue.workbenchKindLabel + " · " + issue.workbenchStateLabel)
                                    .font(.caption).foregroundStyle(.secondary)
                                Text(issue.title).font(.callout.weight(.semibold)).foregroundStyle(.primary).lineLimit(2)
                                Text(issue.workbenchExcerpt).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
                                .background(model.presentation?.anchorID == issue.id ? Color.accentColor.opacity(0.13) : Color.clear)
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain).id(issue.id)
                            .accessibilityIdentifier("attention-row-" + issue.id)
                            .accessibilityValue(model.presentation?.anchorID == issue.id ? "已选中" : "")
                            .accessibilityHint("在详情区查看背景与处理选项")
                        Divider()
                    }
                }.scrollTargetLayout()
                if rows.isEmpty {
                    Text(model.snapshotStale ? "正在等待可读取的事项" : scope == .decisions ? "当前没有需要你决定的事项" : "当前分类没有事项")
                        .font(.callout).foregroundStyle(.secondary).padding(20)
                }
            }.scrollPosition(id: $listPosition, anchor: .top)
        }.accessibilityIdentifier("attention-workbench-list")
    }
    private func detailPane(compact: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if let context = model.presentation, context.mode == .pane, let runner = model.runner {
                HStack {
                    Button(compact ? "返回列表" : "取消选中") { model.close(presentationID: context.id) }
                        .accessibilityIdentifier("attention-pane-back").keyboardShortcut(.cancelAction)
                    if context.anchorID != context.issueID,
                       let parent = model.snapshot.workbenchItem(context.anchorID) {
                        Button("返回主题") { Task { await model.select(parent, mode: .pane) } }
                            .accessibilityIdentifier("attention-pane-parent")
                    }
                    Spacer()
                }.padding(.horizontal, 20).padding(.top, 16)
                if !rows.contains(where: { $0.id == context.anchorID }), !model.snapshotStale {
                    Text("这项事项的状态已变化，当前分类中已不再列出；详情仍保留供核对。")
                        .font(.callout).foregroundStyle(.secondary).padding(.horizontal, 20).padding(.top, 10)
                }
                ScrollView {
                    AttentionDetailView(model: model, runner: runner, presentationID: context.id, pane: true)
                        .id(context.id)
                }.accessibilityIdentifier("attention-workbench-detail-scroll")
            } else {
                VStack(spacing: 10) {
                    Image(systemName: "sidebar.left").font(.largeTitle).foregroundStyle(.secondary)
                    Text("选择一个事项").font(.title3.weight(.semibold))
                    Text("先看清背景、影响和建议，再选择处理方式。")
                        .font(.callout).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(24)
            }
        }.accessibilityIdentifier("attention-workbench-detail")
    }
}

struct QuietEmptyState: View {
    let title: String
    let detail: String
    let symbol: String
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol).font(.title2).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 7) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
    }
}

struct AttentionIssueRow: View {
    let issue: AttentionIssue
    var expanded = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(alignment: .center, spacing: 16) {
                Image(systemName: issue.systemImage).font(.title2).foregroundStyle(.secondary).frame(width: 30)
                VStack(alignment: .leading, spacing: 6) {
                    Text(issue.title).font(.headline).foregroundStyle(.primary)
                    Text(issue.summary).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if issue.kind == "closure" {
                        TimelineView(.periodic(from: .now, by: 1)) { timeline in
                            Text(issue.runtimeLabel(at: timeline.date)).font(.callout).foregroundStyle(.secondary)
                        }
                    }
                }
                Spacer(minLength: 12)
                Image(systemName: expanded ? "chevron.down" : "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
                .contentShape(Rectangle())
        }.buttonStyle(.plain)
            .accessibilityIdentifier("attention-row-" + issue.id)
            .accessibilityHint(expanded ? "收起详情" : "展开背景、影响与处理选项")
    }
}

struct AttentionDetailView: View {
    @ObservedObject var model: AttentionCenterModel
    @ObservedObject var runner: CoreRunner
    let presentationID: UUID
    var inline = false
    var pane = false
    @State private var decisions: [String: String] = [:]
    @State private var batchAction = ""
    @State private var preview: [String: Any] = [:]
    @State private var message = ""
    @State private var busy = false
    @State private var bodyRequest: MemoryBodyRequest?
    @State private var activityChoice = ""
    @State private var workstream = ""
    @State private var completed = false
    @State private var closureChoiceToken = ""
    @State private var closureChoiceGeneration = UUID()
    @State private var closureChoiceLoading = false
    @State private var closureChoiceError = ""
    private var context: AttentionPresentation? { model.presentation?.id == presentationID ? model.presentation : nil }
    var issue: AttentionIssue? { context == nil ? nil : model.selected }

    var body: some View {
        Group {
            if inline || pane {
                VStack(alignment: .leading, spacing: 0) { header; Divider(); detailContents }
                    .background(Color(NSColor.controlBackgroundColor).opacity(0.55), in: RoundedRectangle(cornerRadius: 14))
            } else {
                VStack(spacing: 0) { header; Divider(); ScrollView { detailContents } }
                    .frame(minWidth: 620, idealWidth: 760, minHeight: 560, idealHeight: 720)
            }
        }
        .sheet(item: $bodyRequest) { request in MemoryBodySheet(runner: runner, attention: model, request: request) }
        .onChange(of: model.detailReadID) { _, _ in resetChoices(clearMessage: false) }
        .onChange(of: issue?.revision) { _, _ in resetChoices(clearMessage: false) }
        .onChange(of: issue?.id) { _, _ in resetChoices() }
        .task(id: "\(presentationID):\(issue?.revision ?? ""): \(model.detailReadID):\(model.detailLoading):\(model.detailMessage):\(model.loading):\(model.snapshotStale):\(busy)") {
            if let issue, issue.kind == "closure", issue.closureState == "decision", !interactionDisabled {
                await loadClosureChoices(issue)
            }
        }
        .onDisappear {
            guard !pane, model.presentation?.id == presentationID else { return }
            runner.clearRevealedMemory(); model.close(presentationID: presentationID)
        }
    }
    private var header: some View {
        HStack(alignment: .top, spacing: 14) {
            if !inline {
                VStack(alignment: .leading, spacing: 5) {
                    Text(issue?.title ?? "事项详情").font(.title2.weight(.semibold))
                    Text(pane ? ((issue?.workbenchKindLabel ?? "事项") + " · " + (issue?.workbenchStateLabel ?? "状态待核对")) : (issue?.summary ?? "")).font(.callout).foregroundStyle(.secondary)
                }
            } else { Text("背景与处理选项").font(.headline) }
            Spacer(minLength: 14)
            if !pane {
                Button(inline ? "收起" : "完成") { model.close(presentationID: presentationID) }
                    .keyboardShortcut(.cancelAction)
            }
        }.padding(inline ? 20 : 24)
    }
    private var detailContents: some View {
        VStack(alignment: .leading, spacing: 20) {
            if model.detailLoading { ProgressView("正在核对最新资料…") }
            if !model.detailMessage.isEmpty {
                Text(model.detailMessage).foregroundStyle(.orange)
                Button("重新读取") { if let context { Task { await model.reload(context) } } }
                    .disabled(model.detailLoading)
            }
            if let issue {
                if issue.kind == "closure", (issue.closureThreadID.isEmpty && issue.closureCanHandoff) || issue.closureCanAssist || !issue.closureAssistanceThreadID.isEmpty,
                   context != nil {
                    CodexHandoffComposer(model: model, issueID: issue.id, presentationID: presentationID, closure: true, assistance: issue.closureCanAssist || !issue.closureAssistanceThreadID.isEmpty).id(issue.id)
                    Divider()
                }
                detailBlock("发生了什么", issue.background)
                if issue.kind == "closure" {
                    TimelineView(.periodic(from: .now, by: 1)) { timeline in
                        detailBlock("现在是否在执行", issue.runtimeLabel(at: timeline.date))
                    }
                    detailBlock("恢复安排", issue.closureRecoveryLabel)
                }
                detailBlock("为什么需要核对", issue.reason)
                detailBlock("对你的影响", issue.impact)
                detailBlock("暂不处理会怎样", issue.inaction)
                detailBlock("建议", issue.recommendation)
                DisclosureGroup("核查记录与完成条件") {
                    VStack(alignment: .leading, spacing: 12) {
                        if pane { detailBlock("事项登记", issue.summary) }
                        detailBlock("核查范围与依据", issue.attempts)
                        detailBlock("怎样算完成", issue.completionRule)
                    }.padding(.top, 12)
                }
                let key = AttentionSubmissions.key(issue.id, issue.revision)
                if model.submissions.pending.contains(key) {
                    ProgressView("本次处理仍在等待回执…")
                    Text("你可以收起或查看其他事项；本次请求不会重复提交。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if model.submissions.uncertain.contains(key) || model.submissions.completed.contains(key) || model.submissions.rejected.contains(key) {
                    Text(model.submissions.uncertain.contains(key) ? "上次操作回执不明。先核对状态，不能重复提交。" : model.submissions.rejected.contains(key) ? "本次操作未执行。重新读取最新状态后，可重新选择。" : "这项操作已有回执，请重新读取最新状态。")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("重新读取最新状态") { if let context { Task { await refreshCurrent(context) } } }
                }
                if issue.kind == "memory" { memoryChoices(issue) }
                if issue.kind == "activity" { activityChoices(issue) }
                if issue.kind == "feedback" {
                    Button("查看批评与改进") { model.close(presentationID: presentationID); AppWindowPresenter.shared.show(runner: runner, tab: .evolution) }
                        .disabled(interactionDisabled)
                }
                if issue.kind == "closure" {
                    if issue.closureState == "decision" {
                        if closureChoiceLoading { ProgressView("正在读取本次决定选项…") }
                        if !closureChoiceError.isEmpty {
                            Text(closureChoiceError).foregroundStyle(.orange)
                            Button("重新读取详情及选项") { if let context { Task { await model.reload(context) } } }
                                .disabled(interactionDisabled || closureChoiceLoading)
                        }
                        if !closureChoiceToken.isEmpty {
                            VStack(alignment: .leading, spacing: 10) {
                                Text("仅适用于上方建议与当前范围。费用、新账号权限和对外操作仍需具体核对。")
                                    .font(.callout).foregroundStyle(.secondary)
                                Button("同意建议，按所示范围继续") { Task { await closureChoice("accept_recommendation", issue) } }
                                Button("取消此事项") { Task { await closureChoice("cancel", issue) } }
                                Button("保留等待，暂不决定") { Task { await closureChoice("keep_waiting", issue) } }
                            }.disabled(interactionDisabled || closureChoiceLoading)
                        }
                    }
                    if !issue.closureThreadID.isEmpty {
                        Button("在 Codex 中处理") { Task { await model.openClosureCodex(issue) } }
                            .disabled(interactionDisabled || model.openingClosureID != nil)
                        if let openingMessage = model.closureOpenMessages[issue.id] {
                            if model.openingClosureID == issue.id { ProgressView("正在打开 Codex…") }
                            else { Text(openingMessage).font(.callout).foregroundStyle(.secondary) }
                        }
                    }
                    if !issue.closureAssistanceThreadID.isEmpty {
                        Button("打开协助聊天") { Task { await model.openClosureCodex(issue, assistance: true) } }
                            .disabled(interactionDisabled || model.openingClosureID != nil)
                    } else if issue.closureCanAssist && inline {
                        Button("让 Codex 协助核对") { Task { await model.select(issue, mode: .sheet) } }
                            .disabled(interactionDisabled || model.openingClosureID != nil)
                    }
                    Button("核对进度") { if let context { Task { await reconcileCurrent(context, issue) } } }
                        .disabled(interactionDisabled)
                    Text("验收依据由执行者核对；会话结束不代表事项已解决。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if !issue.children.isEmpty {
                    ForEach(issue.children) { child in
                        AttentionIssueRow(issue: child) { Task { await model.select(child, mode: pane ? .pane : inline ? .inline : .sheet, anchorID: context?.anchorID) } }
                    }
                }
                if !message.isEmpty { Text(message).font(.callout).textSelection(.enabled) }
                if !completed && issue.kind != "feedback" && issue.kind != "closure" {
                    Divider()
                    CodexHandoffComposer(model: model, issueID: issue.id, presentationID: presentationID).id(issue.id)
                }
            }
        }.padding(inline ? 20 : 24).frame(maxWidth: .infinity, alignment: .leading)
    }
    private func resetChoices(clearMessage: Bool = true) {
        bodyRequest = nil
        if context != nil { runner.clearRevealedMemory() }
        decisions = [:]; preview = [:]; batchAction = ""; activityChoice = ""; workstream = ""; completed = false
        closureChoiceToken = ""; closureChoiceGeneration = UUID(); closureChoiceLoading = false; closureChoiceError = ""
        if clearMessage { message = "" }
    }
    private func loadClosureChoices(_ issue: AttentionIssue) async {
        guard let context, context.matches(issueID: issue.id, revision: issue.revision), model.isCurrent(context), !interactionDisabled, !closureChoiceLoading, closureChoiceToken.isEmpty else { return }
        let generation = closureChoiceGeneration, readID = model.detailReadID
        closureChoiceLoading = true; closureChoiceError = ""
        let id = String(issue.id.dropFirst("closure:".count))
        let raw = await runner.runCore(["task-closure", "choice-preview", "--id", id])
        guard generation == closureChoiceGeneration, readID == model.detailReadID, model.isCurrent(context) else { return }
        closureChoiceLoading = false
        if let result = AttentionCenterModel.object(raw), result["issue_id"] as? String == id,
           result["revision"] as? Int == Int(issue.revision), result["default"] is NSNull,
           let token = result["token"] as? String, !token.isEmpty {
            closureChoiceToken = token
        } else { closureChoiceToken = ""; closureChoiceError = "决定选项暂不可读取，或事项已变化。请重新读取。" }
    }
    private func refreshCurrent(_ context: AttentionPresentation) async {
        guard model.isCurrent(context) else { return }
        await model.refresh()
    }
    private func reconcileCurrent(_ context: AttentionPresentation, _ issue: AttentionIssue) async {
        guard context.matches(issueID: issue.id, revision: issue.revision), model.isCurrent(context), !interactionDisabled else { return }
        busy = true; defer { busy = false }
        _ = await runner.runCore(["task-closure", "reconcile", "--id", String(issue.id.dropFirst("closure:".count))])
        guard model.isCurrent(context) else { return }
        await refreshCurrent(context)
    }
    private func closureChoice(_ choice: String, _ issue: AttentionIssue) async {
        guard let context, context.matches(issueID: issue.id, revision: issue.revision), !closureChoiceToken.isEmpty, !interactionDisabled, model.beginSubmission(context) else { return }
        let token = closureChoiceToken; closureChoiceToken = ""
        busy = true; defer { busy = false }
        let data = try? JSONSerialization.data(withJSONObject: ["token": token, "issue_id": String(issue.id.dropFirst("closure:".count)), "revision": Int(issue.revision) ?? -1, "choice": choice])
        let input = data.flatMap { String(data: $0, encoding: .utf8) }
        let raw = await runner.runCore(["task-closure", "decide"], standardInput: input)
        let outcome = AttentionWriteOutcome.closure(AttentionCenterModel.object(raw), id: String(issue.id.dropFirst("closure:".count)), revision: issue.revision)
        model.finishSubmission(context, raw: raw, outcome: outcome)
        guard model.isCurrent(context) else { return }
        switch outcome {
        case .recorded: message = "已记录本次选择；实际处理进度以重新读取后的状态为准。"
        case .rejected: message = "本次选择未执行。事项或依据已变化，请重新查看后选择。"
        case .unknown: message = "选择回执暂不可读取，请先核对状态；不会自动重发。"
        }
        await refreshCurrent(context)
    }
    @ViewBuilder private func detailBlock(_ title: String, _ text: String) -> some View {
        if !text.isEmpty {
            VStack(alignment: .leading, spacing: 7) {
                Text(title).font(.headline)
                Text(text).font(.body).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
    }
    private var actions: [(String, String)] {
        [("keep_active","继续使用"),("mark_legacy","留作历史"),("mark_expired","标为过期"),("review_later","稍后复核"),("quarantine_wrong_subject","隔离")]
    }
    private var interactionDisabled: Bool {
        guard let context, model.isCurrent(context) else { return true }
        return busy || completed || !model.canInteract(context) ||
            model.submissions.blocked(AttentionSubmissions.key(context.issueID, context.revision))
    }
    @ViewBuilder private func memoryChoices(_ issue: AttentionIssue) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("本组记忆 · \(issue.memberCount) 条").font(.headline)
            Text("先看正文，再选择处理方式。相同范围、相同影响的成员可以一起预览；范围不明确的记录请一次处理一条。")
                .font(.callout).foregroundStyle(.secondary)
            if issue.members.count > 1 {
                HStack {
                    Menu("为本组选择同一处理方式") {
                        ForEach(actions, id: \.0) { action in
                            Button(action.1) {
                                batchAction = action.0
                                decisions = Dictionary(uniqueKeysWithValues: issue.members.map { ($0.id, action.0) })
                                preview = [:]
                            }
                        }
                    }.disabled(interactionDisabled)
                    Spacer()
                    Button("清空选择") { decisions = [:]; preview = [:]; batchAction = "" }
                }
            }
            ForEach(Array(issue.members.enumerated()), id: \.element.id) { index, member in
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("\(index + 1). \(member.title)").font(.body.weight(.medium))
                        Spacer()
                        Button("看正文") { bodyRequest = MemoryBodyRequest(memoryID: member.id) }
                    }
                    Text(member.reason).font(.caption).foregroundStyle(.secondary)
                    Picker("处理方式", selection: Binding(get: { decisions[member.id] ?? "" }, set: { value in
                        if value.isEmpty { decisions.removeValue(forKey: member.id) } else { decisions[member.id] = value }
                        preview = [:]; batchAction = ""
                    })) {
                        Text("尚未选择").tag("")
                        ForEach(actions, id: \.0) { action in Text(action.1).tag(action.0) }
                    }.disabled(interactionDisabled)
                }.padding(16).background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            }
            if !decisions.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("已选择 \(decisions.count) 条").font(.headline)
                    Button("预览具体影响") { Task { await previewChoices(issue) } }.disabled(interactionDisabled)
                    if let changes = preview["changes"] as? [[String: Any]], let token = preview["preview_digest"] as? String {
                        ForEach(Array(changes.enumerated()), id: \.offset) { _, change in
                            let index = issue.members.firstIndex { $0.id == change["memory_id"] as? String }.map { $0 + 1 } ?? 0
                            Text("第 \(index) 条 · \(change["action_title"] as? String ?? "")：\((change["becomes_retrievable"] as? Bool == true) ? "AI 可以调用当前版本" : "AI 不再自动调用，原记录保留")")
                                .font(.callout)
                        }
                        Button("确认这些修改") { Task { await applyChoices(issue, token: token) } }
                            .buttonStyle(.borderedProminent).disabled(interactionDisabled)
                    }
                }
            }
        }
    }
    private func payload(_ issue: AttentionIssue) -> [String: Any] {
        ["issue_id":issue.id, "revision":issue.revision, "batch_action":!batchAction.isEmpty,
         "decisions": decisions.keys.sorted().map { ["memory_id":$0, "action":decisions[$0]!, "note":"AGI 决策详情"] }]
    }
    private func previewChoices(_ issue: AttentionIssue) async {
        guard let context, context.matches(issueID: issue.id, revision: issue.revision), !interactionDisabled else { return }
        busy = true; defer { busy = false }; message = ""
        let submittedChoices = decisions
        let result = await model.command("preview", payload: payload(issue))
        guard model.isCurrent(context) else { return }
        guard decisions == submittedChoices, AttentionMemoryPreview.matches(result, revision: issue.revision, decisions: submittedChoices) else {
            preview = [:]; message = "预览暂不可读取，或事项与选择已变化。请重新预览。"; return
        }
        preview = result; message = ""
    }
    private func applyChoices(_ issue: AttentionIssue, token: String) async {
        guard let context, context.matches(issueID: issue.id, revision: issue.revision), !interactionDisabled,
              preview["preview_digest"] as? String == token, AttentionMemoryPreview.matches(preview, revision: issue.revision, decisions: decisions),
              model.beginSubmission(context) else { return }
        busy = true; defer { busy = false }
        let submittedChoices = decisions
        var value = payload(issue); value["confirm_digest"] = token
        let response = await model.commandResult("apply", payload: value)
        let result = response.value, raw = response.raw
        let outcome = AttentionWriteOutcome.memory(result)
        let repeatableLater = outcome == .recorded && result["applied_count"] as? Int == 0 &&
            result["unchanged_count"] as? Int == submittedChoices.count && submittedChoices.values.allSatisfy { $0 == "review_later" }
        model.finishSubmission(context, raw: raw, outcome: outcome,
            reviewLaterMembers: repeatableLater ? Set(submittedChoices.keys) : [])
        guard model.isCurrent(context) else { return }
        preview = [:]
        if outcome == .rejected { message = "本次修改未执行，重新核对后请再次预览。"; await refreshCurrent(context) }
        else if outcome == .recorded {
            message = "处理结果已写入，正在核对最新状态。"; decisions = [:]
            await runner.refreshMemoryReview(force: true)
            guard model.isCurrent(context) else { return }
            await model.refresh()
            guard model.isCurrent(context) else { return }
            if model.snapshot.decisions.contains(where: { $0.id == issue.id }) {
                await model.reload(context)
            } else if model.snapshot.memoryPending != nil {
                completed = true; message = "已核对处理结果，这组事项已处理。"
            } else { message = "已提交处理，最新审核状态暂不可读取，请稍后核对。" }
        } else { message = "处理回执暂不可读取，请重新核对状态。" }
    }
    @ViewBuilder private func activityChoices(_ issue: AttentionIssue) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("你的判断", selection: $activityChoice) {
                Text("尚未选择").tag("")
                Text("确定发生过").tag("occurred")
                Text("确定没有发生").tag("not_occurred")
                Text("暂缓，保留待答复").tag("defer")
            }.disabled(interactionDisabled)
            if !activityChoice.isEmpty {
                if activityChoice != "defer" {
                    TextField("这属于哪项工作或项目", text: $workstream)
                        .textFieldStyle(.roundedBorder).disabled(interactionDisabled)
                    Text("用于放入正确的记录；不确定时可以暂缓或交给 Codex 核查。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text(activityChoice == "defer" ? "保留待答复，不会改写为已经完成。" : "只修改这个事项的活动记录，不会发送消息或写入长期记忆。")
                    .font(.caption).foregroundStyle(.secondary)
                Button("确认这个判断") {
                    Task {
                        guard let context, context.matches(issueID: issue.id, revision: issue.revision), !interactionDisabled, model.beginSubmission(context) else { return }
                        busy = true; defer { busy = false }
                        let submittedAnswer = activityChoice, submittedWorkstream = workstream
                        let input = try? JSONSerialization.data(withJSONObject: ["workstream":submittedWorkstream,"outcome":"unknown","note":""])
                        let raw = await runner.runCore(["activity-confirmation","answer-group","--group-key",issue.groupKey,
                            "--member-revision",issue.revision,"--answer",submittedAnswer,"--details-stdin"],
                            standardInput: input.flatMap { String(data: $0, encoding: .utf8) })
                        let result = AttentionCenterModel.object(raw)
                        let outcome = AttentionWriteOutcome.activity(result, groupKey: issue.groupKey)
                        model.finishSubmission(context, raw: raw, outcome: outcome, deferred: submittedAnswer == "defer")
                        guard model.isCurrent(context) else { return }
                        switch outcome {
                        case .recorded: message = "本次判断已记录，请以最新事项状态为准。"
                        case .rejected: message = "本次判断未写入，重新核对后请再次选择。"
                        case .unknown: message = "判断回执暂不可读取，请先核对状态；不会自动重发。"
                        }
                        await refreshCurrent(context)
                    }
                }.buttonStyle(.borderedProminent).disabled(interactionDisabled || (activityChoice != "defer" && workstream.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
            }
        }
    }
}

struct CodexHandoffComposer: View {
    @ObservedObject var model: AttentionCenterModel
    let issueID: String
    let presentationID: UUID
    var closure = false
    var assistance = false
    @State private var text = ""
    @State private var loaded = false
    @State private var sending = false
    @State private var statusUnreadable = false
    @State private var messageID = UUID().uuidString.lowercased()
    @State private var submittedText = ""
    @State private var result: [String: Any] = [:]
    @State private var error = ""
    @State private var draftSaveTask: Task<Void, Never>?
    @State private var draftRevision: Int64 = 0
    @State private var draftLease: AttentionHandoffDraftCache.Lease?
    private var draftSourceCurrent: Bool { draftLease.map(model.handoffDrafts.isCurrent) ?? false }
    private var context: AttentionPresentation? {
        guard let context = model.presentation, context.id == presentationID, context.issueID == issueID else { return nil }
        return context
    }
    private var canAct: Bool { !statusUnreadable && !model.handoffBusy.contains(issueID) && !model.handoffUncertain.contains(issueID) && context.map(model.canInteract) == true }
    private var canSubmit: Bool { canAct && loaded && !sending && !active && !["failed","uncertain"].contains(status) }
    private func current(_ bound: AttentionPresentation) -> Bool { model.isCurrent(bound) && bound.id == presentationID }
    private var handoff: [String: Any] { result["handoff"] as? [String: Any] ?? [:] }
    private var status: String { handoff["status"] as? String ?? "" }
    private var resultVerified: Bool { (handoff["verification"] as? [String: Any])?["state"] as? String == "resolved" }
    private var active: Bool { ["queued","preparing","opening","sending","verifying"].contains(status) }
    private var submitTitle: String {
        if status == "delivered" && !resultVerified && (handoff["content"] as? String) == text {
            return "继续处理并打开 Codex"
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "交给 Codex 梳理" : "发送补充并打开 Codex"
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(assistance ? "让 Codex 协助核对" : "交给 Codex 继续处理").font(.headline)
            Text(assistance ? "上次打开时未能读取原聊天，发送前会再次核对。发送后创建独立的协助聊天，仅请求核对事项登记和处理路径，保留原负责人及待决定状态。本次请求不授权重跑原任务、读取原记忆正文或写长期记忆。" : closure ? "在事项原项目中交给 Codex 核查，可补充说明。原负责人和待决定状态保持；这里不会替你同意建议、取消事项或开始修复。" : "只转交通用背景、影响和完成条件，正文不随本页转交。Codex 按任务范围沿用已有授权核查，遵守你补充的具体限制；记忆审核结果不会自动改变。")
                .font(.callout).foregroundStyle(.secondary)
            TextEditor(text: Binding(get: { text }, set: { value in text = value; saveDraft(value) })).font(.body).frame(minHeight: 90, maxHeight: 150)
                .padding(6).overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.25)))
                .accessibilityLabel("给 Codex 的补充，可留空")
                .disabled(!loaded || sending || active)
            Text("可留空直接交接，也可以补充你的想法。草稿仅保留在本机。")
                .font(.caption).foregroundStyle(.secondary)
            if !status.isEmpty {
                Label(resultVerified ? "已核验 · 问题已解决" : status == "delivered" ? "Codex 已接收 · 结果待核验" : active ? "正在转交…" : "转交尚未完成",
                      systemImage: status == "delivered" ? "checkmark.circle" : "arrow.up.right.square")
                    .font(.callout.weight(.medium))
                if let message = handoff["error"] as? String, !message.isEmpty { Text(message).font(.callout) }
            }
            if !error.isEmpty { Text(error).font(.callout).foregroundStyle(.orange) }
            if let verification = handoff["verification"] as? [String: Any], let message = verification["message"] as? String {
                Text(message).font(.callout).textSelection(.enabled)
            }
            HStack {
                Button(submitTitle) {
                    guard let bound = context, let issue = model.selected, canSubmit else { return }
                    Task { await submit(bound, issue: issue) }
                }.buttonStyle(.borderedProminent).disabled(!canSubmit || text.count > 4000)
                    .keyboardShortcut(.return, modifiers: .command)
                if let thread = handoff["thread_id"] as? String, !thread.isEmpty {
                    Button("在 Codex 中继续") {
                        guard let bound = context, canAct else { return }
                        Task {
                            let response = closure ? await model.command("closure-open", id: issueID,
                                payload: ["revision":bound.revision, "thread_id":thread, "assistance":assistance]) : await model.command("handoff-open", id: issueID)
                            guard current(bound) else { return }
                            if let message = response["error"] as? String { error = message }
                            else if response["status"] as? String == "ok" { error = "" }
                            else { error = "Codex 暂时无法打开，请核对接手状态后再打开。" }
                        }
                    }.disabled(!canAct)
                }
                if status == "delivered" {
                    Button("核对处理结果") {
                        guard let bound = context, canAct else { return }
                        Task {
                            await performHandoffAction("handoff-check", bound: bound, refresh: true)
                        }
                    }.disabled(!canAct)
                }
                if ["failed","uncertain"].contains(status), let identifier = handoff["submission_id"] as? String {
                    Button("核对并重试") {
                        guard let bound = context, canAct else { return }
                        Task {
                            await performHandoffAction("handoff-retry", bound: bound, payload: ["submission_id":identifier])
                        }
                    }.disabled(!canAct)
                }
            }
        }
        .task(id: context?.revision) {
            guard let bound = context else { return }
            draftLease = model.handoffDrafts.lease(for: issueID)
            let hadDraft = loaded
            let readEpoch = model.handoffReadEpoch(issueID)
            let response = await model.command("handoff-status", id: issueID)
            guard !Task.isCancelled, current(bound) else { return }
            let accepted = model.acceptHandoffRead(issueID, epoch: readEpoch, response: response)
            if AttentionHandoffReceipt.sourceWithdrawn(response) { clearWithdrawnHandoff(); return }
            if !accepted {
                statusUnreadable = true
                error = "交接状态暂不可读取；原请求和草稿保留，正在核对。"
            }
            if accepted {
                result = response; error = ""; statusUnreadable = false
                if !hadDraft { restoreDraft(result) }
                draftRevision = max(draftRevision, (result["draft_revision"] as? NSNumber)?.int64Value ?? 0)
                loaded = true
            }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard !Task.isCancelled else { break }
                if !loaded || active || statusUnreadable || model.handoffBusy.contains(issueID) || model.handoffUncertain.contains(issueID) {
                    let readEpoch = model.handoffReadEpoch(issueID)
                    let response = await model.command("handoff-status", id: issueID)
                    guard !Task.isCancelled, current(bound) else { return }
                    let accepted = model.acceptHandoffRead(issueID, epoch: readEpoch, response: response)
                    if AttentionHandoffReceipt.sourceWithdrawn(response) { clearWithdrawnHandoff(); return }
                    guard accepted else {
                        statusUnreadable = true; error = "交接状态暂不可读取；原请求保留，正在核对。"; continue
                    }
                    if !loaded {
                        restoreDraft(response); loaded = true
                    }
                    result = response; statusUnreadable = false; error = ""
                    if closure && status == "delivered" { await model.refresh() }
                }
            }
        }
        .onChange(of: draftSourceCurrent) { _, current in
            if !current { clearWithdrawnHandoff() }
        }
        .onDisappear {
            draftSaveTask?.cancel()
            let draft = text
            let revision = nextDraftRevision()
            if loaded, let lease = draftLease, model.handoffDrafts.save(draft, revision: revision, using: lease) {
                Task {
                    guard model.handoffDrafts.isCurrent(lease) else { return }
                    _ = await model.command("draft", id: issueID, payload: ["content":draft,"draft_revision":revision])
                }
            } else if !draftSourceCurrent {
                clearWithdrawnHandoff()
            }
        }
    }
    private func performHandoffAction(_ action: String, bound: AttentionPresentation, payload: [String: Any]? = nil, refresh: Bool = false) async {
        guard loaded, canAct, current(bound), model.beginHandoff(issueID) else { return }
        let response = await model.command(action, id: issueID, payload: payload)
        model.finishHandoff(issueID, response: response)
        guard current(bound) else { return }
        if receiveHandoff(response), refresh { await model.refresh() }
    }
    @discardableResult private func receiveHandoff(_ response: [String: Any]) -> Bool {
        if AttentionHandoffReceipt.sourceWithdrawn(response) { clearWithdrawnHandoff(); return false }
        guard AttentionHandoffReceipt.verified(response) else {
            statusUnreadable = true
            error = response["error"] as? String ?? "交接回执暂不可读取；原请求和草稿保留，正在核对，不会自动重发。"
            return false
        }
        result = response; statusUnreadable = false; error = ""
        return true
    }
    private func restoreDraft(_ response: [String: Any]) {
        guard let lease = draftLease, model.handoffDrafts.isCurrent(lease) else { clearWithdrawnHandoff(); return }
        let serverRevision = (response["draft_revision"] as? NSNumber)?.int64Value ?? 0
        if let cached = model.handoffDrafts.draft(using: lease), cached.revision >= serverRevision {
            text = cached.text; draftRevision = max(draftRevision, cached.revision)
        } else {
            text = response["draft"] as? String ?? ""; draftRevision = max(draftRevision, serverRevision)
        }
    }
    private func saveDraft(_ value: String) {
        guard loaded, let lease = draftLease, model.handoffDrafts.isCurrent(lease) else { return }
        draftSaveTask?.cancel()
        let revision = nextDraftRevision()
        // Cache synchronously before a quick A -> B -> A navigation can read the
        // older server draft. Delayed disk saves retain the same monotonic version.
        guard model.handoffDrafts.save(value, revision: revision, using: lease) else { return }
        draftSaveTask = Task {
            try? await Task.sleep(nanoseconds: 400_000_000)
            guard !Task.isCancelled, model.handoffDrafts.isCurrent(lease) else { return }
            _ = await model.command("draft", id: issueID, payload: ["content":value,"draft_revision":revision])
        }
    }
    private func clearWithdrawnHandoff() {
        draftSaveTask?.cancel(); draftSaveTask = nil
        text = ""; submittedText = ""; result = [:]; draftRevision = 0
        loaded = false; statusUnreadable = true; error = "资料范围已变化，请重新读取事项。"
    }
    private func nextDraftRevision() -> Int64 {
        draftRevision = max(draftRevision + 1, Int64(Date().timeIntervalSince1970 * 1_000_000))
        return draftRevision
    }
    private func submit(_ bound: AttentionPresentation, issue: AttentionIssue) async {
        guard canSubmit, current(bound), model.canInteract(bound), bound.matches(issueID: issue.id, revision: issue.revision), model.beginHandoff(issueID) else { return }
        sending = true; error = ""
        defer { sending = false }
        // A deliberate follow-up after confirmed delivery gets a fresh ID.
        // Failed/uncertain calls retain their ID for receipt-safe retry.
        let currentRequestDelivered = status == "delivered" && (handoff["submission_id"] as? String) == messageID
        if submittedText != text || currentRequestDelivered {
            messageID = UUID().uuidString.lowercased(); submittedText = text
        }
        draftSaveTask?.cancel()
        var payload: [String: Any] = ["content":text,"client_message_id":messageID,"draft_revision":nextDraftRevision(),"issue_revision":bound.revision]
        if assistance {
            payload["assistance_token"] = issue.closureAssistanceToken
            payload["origin_thread_id"] = issue.closureThreadID
        }
        let response = await model.command("handoff-submit", id: issueID, payload: payload)
        model.finishHandoff(issueID, response: response)
        guard current(bound) else { return }
        _ = receiveHandoff(response)
    }
}

struct MemoryLibraryView: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var attention: AttentionCenterModel
    @State private var filter = "pending"
    @State private var bodyRequest: MemoryBodyRequest?
    private var items: [MemoryReviewCandidate] {
        runner.memoryReviewList.items.filter { item in
            let pending = item.reviewState == "pending" || item.decision == "review_later"
            return filter == "all" || (filter == "pending" ? pending : !pending)
        }
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("记忆库").font(.largeTitle.weight(.semibold))
                        Text("看清记住了什么，以及哪些内容仍可被 AI 使用。")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("重新读取") { Task { await runner.refreshMemoryReview(force: true); await attention.refresh() } }
                        .disabled(runner.memoryReviewLoading)
                }
                if runner.memoryReviewLoading { ProgressView("正在读取记忆清单…") }
                if runner.memoryReviewStatus.ok {
                    Text("共 \(runner.memoryReviewStatus.total) 条审核候选 · \(attention.snapshot.memoryPending.map(String.init) ?? "待核对") 条待复核")
                        .font(.headline)
                    Picker("查看范围", selection: $filter) {
                        Text("待复核").tag("pending")
                        Text("处理历史").tag("decided")
                        Text("全部").tag("all")
                    }.pickerStyle(.segmented).frame(maxWidth: 420)
                    if items.isEmpty { QuietEmptyState(title: "这里暂时没有记忆", detail: "切换范围可以查看其他记录。", symbol: "brain") }
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        Button {
                            bodyRequest = MemoryBodyRequest(memoryID: item.memoryID)
                        } label: {
                            HStack(spacing: 16) {
                                Image(systemName: item.retrievable ? "checkmark.circle" : "brain").font(.title2).foregroundStyle(.secondary)
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(MemoryReasonPresentation.title(item.reasonCodes)).font(.headline)
                                    Text(item.reviewState == "pending" ? "待复核 · \(item.retrievable ? "保持原有调用状态" : "当前不自动调用")" : "\(item.decisionTitle) · \(item.retrievable ? "AI 可用" : "AI 不自动调用")")
                                        .font(.callout).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text("看正文").foregroundStyle(Color.accentColor)
                                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                            }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
                                .background(Color(NSColor.controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain).accessibilityLabel("第 \(index + 1) 条，\(MemoryReasonPresentation.title(item.reasonCodes))，看正文")
                    }
                } else if !runner.memoryReviewLoading {
                    QuietEmptyState(title: "记忆审核资料暂不可读取", detail: "请重新读取，或前往系统状态查看影响和 Codex 接手入口。", symbol: "externaldrive.badge.questionmark")
                }
            }.padding(24).frame(maxWidth: 960).frame(maxWidth: .infinity)
        }.task { await runner.refreshMemoryReview() }
            .sheet(item: $bodyRequest) { request in MemoryBodySheet(runner: runner, attention: attention, request: request) }
            .onDisappear { runner.clearRevealedMemory() }
    }
}

struct MemoryBodyRequest: Identifiable {
    let id = UUID()
    let memoryID: String
}

struct MemoryBodySheet: View {
    @ObservedObject var runner: CoreRunner
    @ObservedObject var attention: AttentionCenterModel
    let request: MemoryBodyRequest
    @Environment(\.dismiss) private var dismiss
    @State private var choice = ""
    @State private var message = ""
    private var ownsReveal: Bool { runner.memoryRevealSession.presentationID == request.id }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("记忆正文").font(.title2.weight(.semibold))
                Spacer()
                Button("关闭") { runner.clearRevealedMemory(presentationID: request.id); dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("正文只在当前面板临时显示，关闭后清除，不会自动分享。")
                .font(.callout).foregroundStyle(.secondary)
            Divider()
            Group {
                switch ownsReveal ? runner.memoryRevealSession.state : "loading" {
                case "loading": ProgressView("正在读取正文…").frame(maxWidth: .infinity, minHeight: 180)
                case "ready":
                    ScrollView {
                        Text(runner.memoryRevealSession.text).font(.body).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                    }.background(Color(NSColor.textBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
                    Text(runner.memoryRevealSession.scope.isEmpty ? "适用范围尚未明确" : "适用范围：\(runner.memoryRevealSession.scope)")
                        .font(.caption).foregroundStyle(.secondary)
                case "empty": QuietEmptyState(title: "这条记录没有正文", detail: "记录仍保留。可重新读取确认来源状态。", symbol: "doc")
                case "failed": QuietEmptyState(title: "正文暂不可读取", detail: runner.memoryRevealSession.text, symbol: "doc.badge.ellipsis")
                default: QuietEmptyState(title: "正文已关闭", detail: "返回列表重新选择需要查看的记录。", symbol: "lock.doc")
                }
            }
            if ownsReveal && ["failed","empty"].contains(runner.memoryRevealSession.state) {
                Button("重试读取") { Task { await runner.revealMemory(request.memoryID, presentationID: request.id) } }
            }
            if ownsReveal && runner.memoryRevealSession.state == "ready" {
                DisclosureGroup("调整这条记忆的使用方式") {
                    VStack(alignment: .leading, spacing: 10) {
                        Picker("处理方式", selection: $choice) {
                            Text("尚未选择").tag("")
                            Text("继续有效").tag("keep_active")
                            Text("留作历史").tag("mark_legacy")
                            Text("标为过期").tag("mark_expired")
                            Text("稍后复核").tag("review_later")
                            Text("隔离").tag("quarantine_wrong_subject")
                        }
                        if !choice.isEmpty {
                            Text(choice == "keep_active" ? "允许 AI 调用当前版本；原记录保留。" : "AI 不再自动调用；原记录保留。")
                                .font(.caption).foregroundStyle(.secondary)
                            HStack {
                                Button("预览影响") { Task { await runner.previewMemoryReviewDecisions([runner.memoryRevealSession.identifier:choice]) } }
                                if runner.memoryReviewPreview.isReady {
                                    Button("确认修改") {
                                        Task {
                                            if await runner.applyMemoryReviewDecisions([runner.memoryRevealSession.identifier:choice]) {
                                                message = "处理结果已写入。"; choice = ""; await attention.refresh()
                                            } else { message = runner.memoryReviewMessage }
                                        }
                                    }.buttonStyle(.borderedProminent)
                                }
                            }.disabled(runner.memoryReviewLoading)
                            Text(runner.memoryReviewPreview.errorMessage).font(.caption)
                        }
                        if !message.isEmpty { Text(message).font(.callout) }
                    }.padding(.top, 8)
                }
            }
        }.padding(24).frame(minWidth: 560, idealWidth: 680, minHeight: 460, idealHeight: 620)
            .task(id: request.id) {
                guard !Task.isCancelled else { return }
                await runner.revealMemory(request.memoryID, presentationID: request.id)
            }
            .onChange(of: choice) { _, _ in runner.memoryReviewPreview = MemoryReviewPreview() }
            .onChange(of: runner.memoryRevealSession.identifier) { old, new in
                choice = ""; runner.memoryReviewPreview = MemoryReviewPreview()
                if !old.isEmpty && new.isEmpty && runner.memoryRevealSession.identifier.isEmpty { dismiss() }
            }
            .onChange(of: runner.memoryRevealSession.presentationID) { _, current in
                if current != request.id && runner.memoryRevealSession.identifier.isEmpty { dismiss() }
            }
            .onDisappear {
                if runner.clearRevealedMemory(presentationID: request.id) { runner.memoryReviewPreview = MemoryReviewPreview() }
            }
    }
}
