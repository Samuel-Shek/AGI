import SwiftUI

struct SystemStatusLine: View {
    let title: String
    let value: String
    let symbol: String

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Label(title, systemImage: symbol).foregroundStyle(.secondary).fixedSize()
                Spacer(minLength: 12)
                Text(value).font(.callout.weight(.medium)).fixedSize()
            }
            VStack(alignment: .leading, spacing: 4) {
                Label(title, systemImage: symbol).foregroundStyle(.secondary)
                Text(value).font(.callout.weight(.medium)).fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct SystemStatusMetric: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title2.monospacedDigit().weight(.semibold))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .accessibilityElement(children: .combine)
    }
}

/// Four, two or one columns without a fixed card height or a nested scroll view.
struct SystemStatusMetricLayout: Layout {
    private let spacing: CGFloat = 12

    private func columns(_ width: CGFloat) -> Int {
        width >= 800 ? 4 : (width >= 400 ? 2 : 1)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 800
        let count = columns(width)
        let itemWidth = max(0, (width - CGFloat(count - 1) * spacing) / CGFloat(count))
        let heights = subviews.map { $0.sizeThatFits(.init(width: itemWidth, height: nil)).height }
        let rows = stride(from: 0, to: heights.count, by: count).map {
            heights[$0..<min($0 + count, heights.count)].max() ?? 0
        }
        return .init(width: width, height: rows.reduce(0, +) + CGFloat(max(0, rows.count - 1)) * spacing)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let count = columns(bounds.width)
        let itemWidth = max(0, (bounds.width - CGFloat(count - 1) * spacing) / CGFloat(count))
        var y = bounds.minY
        for row in stride(from: 0, to: subviews.count, by: count) {
            let end = min(row + count, subviews.count)
            let height = subviews[row..<end].map { $0.sizeThatFits(.init(width: itemWidth, height: nil)).height }.max() ?? 0
            for index in row..<end {
                subviews[index].place(at: .init(x: bounds.minX + CGFloat(index - row) * (itemWidth + spacing), y: y),
                    anchor: .topLeading, proposal: .init(width: itemWidth, height: height))
            }
            y += height + spacing
        }
    }
}
