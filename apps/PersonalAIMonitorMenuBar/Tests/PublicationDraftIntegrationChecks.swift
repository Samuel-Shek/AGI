// Compile with the actual PublicationDrafts.swift and PAMInteractionModel.
import Foundation
import PAMInteractionModel

@main struct PublicationDraftIntegrationChecks {
    @MainActor static func main() async {
        let registry = PublicationDrafts.shared
        assert(PublicationDrafts.mutation("{\"status\":\"rejected\",\"raw_access\":false,\"save_state\":\"unknown\"}") == .unknown)
        assert(PublicationDrafts.mutation("{\"status\":\"rejected\",\"raw_access\":false,\"save_state\":\"save_failed\"}") == .saveFailed)
        let scope = "fixture-" + UUID().uuidString
        let first = RetainedDraft(wrappedValue: "", scope, "text")
        first.wrappedValue = "local pending input"
        // A remount uses the same owner; unrelated screens do not clear it.
        let remount = RetainedDraft(wrappedValue: "", scope, "text")
        assert(remount.wrappedValue == "local pending input")
        assert(!registry.isClean(scope))
        let save = registry.begin(scope)
        remount.wrappedValue = "new edit during save"
        registry.finish(save, .clean)
        assert(!registry.isClean(scope))
        registry.discard(scope)
        assert(remount.wrappedValue == "local pending input")
        assert(registry.isClean(scope))
        remount.wrappedValue = "awaiting exact receipt"
        let unknown = registry.begin(scope)
        registry.finish(unknown, .unknown)
        assert(registry.needsReadback(scope))
        var resolved = 0
        registry.whenResolved(unknown) { result in if result == .clean { resolved += 1 } }
        await registry.verify(unknown) { .unknown }
        assert(resolved == 0)
        registry.discard(scope)
        assert(registry.needsReadback(scope))
        await registry.verify(unknown) { .clean }
        assert(resolved == 1)
        // The discarded old value is different from the now-confirmed save.
        assert(!registry.isClean(scope))
        registry.discard(scope)
        assert(registry.isClean(scope))
        registry.resetSaved(scope)
        assert(remount.wrappedValue == "")
        var completed = 0
        remount.wrappedValue = "delayed confirmed form"
        let delayed = registry.begin(scope)
        registry.finish(delayed, .unknown)
        registry.completeAfterReadback(delayed, scope:scope) { completed += 1 }
        await registry.verify(delayed) { .clean }
        assert(completed == 1 && remount.wrappedValue.isEmpty && registry.isClean(scope))
        remount.wrappedValue = "same text later"
        let stale = registry.begin(scope)
        registry.finish(stale, .unknown)
        registry.completeAfterReadback(stale, scope:scope) { completed += 1 }
        remount.wrappedValue = "intervening edit"
        remount.wrappedValue = "same text later"
        await registry.verify(stale) { .clean }
        assert(completed == 1 && remount.wrappedValue == "same text later" && !registry.isClean(scope))
        registry.discard(scope)
        registry.resetSaved(scope)
        let direct = registry.begin(scope + ".no-edits")
        assert(!registry.isClean(scope + ".no-edits"))
        registry.finish(direct, .clean)
        assert(registry.isClean(scope + ".no-edits"))
        let other = RetainedDraft(wrappedValue: "", scope + ".other", "text")
        other.wrappedValue = "other pending form"
        let one = registry.begin(scope)
        registry.finish(one, .clean)
        assert(!registry.isClean(scope + ".other"))
        // Withdrawal must drop values without allowing a late callback to restore them.
        remount.wrappedValue = "withdrawn source value"
        let old = registry.begin(scope)
        registry.withdraw(scope)
        assert(remount.wrappedValue.isEmpty)
        assert(other.wrappedValue.isEmpty)
        let fresh = RetainedDraft(wrappedValue: "new baseline", scope, "text")
        registry.finish(old, .clean)
        assert(fresh.wrappedValue == "new baseline")
        assert(registry.isClean(scope))
        print("Publication draft lifecycle checks passed: remount, concurrent edit, unknown readback, exact form completion, stale completion, discard, empty submit, independent forms, withdrawal.")
    }
}
