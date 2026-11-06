import Foundation
import Metal
import simd

/// Cancellation belongs to one immutable selection/density request. A newer
/// request cancels the previous traversal before it allocates more chunks.
final class TrajectoryBuildCancellation {
    private let lock = NSLock()
    private var cancelled = false
    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    func check() throws { if isCancelled { throw TrajectoryBuildError.cancelled } }
}

enum TrajectoryBuildError: LocalizedError {
    case cancelled
    case capacity(Int)
    var errorDescription: String? {
        switch self {
        case .cancelled: return nil
        case .capacity(let bytes):
            return L10n.format("trajectory.capacity", "Complete trajectories exceed the preview memory budget (%@ MiB). Reduce trajectory density explicitly; Gaussian rendering retains every identity.", String(bytes / 1_048_576))
        }
    }
}

/// A streaming builder has no T×N Swift object graph and never copies a complete
/// segment array into Metal. Each chunk is filled in place and then immutable.
private final class TrajectoryChunkBuilder {
    let device: MTLDevice
    let cancellation: TrajectoryBuildCancellation
    let segmentsPerChunk: Int
    let budget: Int
    private var chunks = [TrajectoryChunk]()
    private var current: MTLBuffer?
    private var ranges = [TrajectoryTimeRange]()
    private var used = 0
    private var allocated = 0
    private(set) var count = 0

    init(device: MTLDevice, cancellation: TrajectoryBuildCancellation, estimatedSegments: Int) {
        self.device = device; self.cancellation = cancellation
        segmentsPerChunk = max(1, min(estimatedSegments, min(32 * 1_048_576, device.maxBufferLength) / MemoryLayout<TrajectorySegmentGPU>.stride))
        // This is a declared allocation ceiling, never an identity/sample cap.
        // Leave headroom for source frames, three sort buffers and the OS.
        budget = max(32 * 1_048_576, min(2_147_483_648, Int(device.recommendedMaxWorkingSetSize / 3)))
    }
    func append(_ segment: TrajectorySegmentGPU) throws {
        if current == nil || used == segmentsPerChunk {
            try cancellation.check()
            if let current { chunks.append(TrajectoryChunk(buffer: current, segmentCount: used, timeRanges: ranges)) }
            let bytes = segmentsPerChunk * MemoryLayout<TrajectorySegmentGPU>.stride
            guard allocated <= budget - bytes else { throw TrajectoryBuildError.capacity(budget) }
            guard let next = device.makeBuffer(length: bytes, options: .storageModeShared) else { throw SceneError.invalid(L10n.tr("trajectory.allocation", "Cannot allocate a complete trajectory chunk.")) }
            next.label = "Immutable identity trajectories · chunk \(chunks.count)"
            current = next; used = 0; allocated += bytes; ranges.removeAll(keepingCapacity: true)
        }
        if let last = ranges.last,
           last.startTime == segment.startPositionTime.w && last.endTime == segment.endPositionTime.w {
            ranges[ranges.count - 1] = TrajectoryTimeRange(firstSegment: last.firstSegment,
                segmentCount: last.segmentCount + 1, startTime: last.startTime, endTime: last.endTime)
        } else {
            ranges.append(TrajectoryTimeRange(firstSegment: used, segmentCount: 1,
                                              startTime: segment.startPositionTime.w, endTime: segment.endPositionTime.w))
        }
        current!.contents().assumingMemoryBound(to: TrajectorySegmentGPU.self)[used] = segment
        used += 1; count += 1
    }
    func finish() -> [TrajectoryChunk] {
        if let current, used > 0 { chunks.append(TrajectoryChunk(buffer: current, segmentCount: used, timeRanges: ranges)) }
        current = nil
        return chunks
    }
}

enum TrajectoryDensity {
    /// Stable, nested subsets of exact size. Changing the playhead or visibility
    /// does not reshuffle the same object's identities. Explicit element picks
    /// are added after this operation and are never hidden by a density slider.
    static func rows(ids: [Int64], objectID: String, percent: Int) -> [Int] {
        let percent = min(100, max(0, percent))
        if percent == 100 { return Array(ids.indices) }
        let keep = ids.count * percent / 100
        if keep == 0 { return [] }
        var seed: UInt64 = 14_695_981_039_346_656_037
        for byte in objectID.utf8 { seed = (seed ^ UInt64(byte)) &* 1_099_511_628_211 }
        func hash(_ id: Int64) -> UInt64 {
            var x = UInt64(bitPattern: id) ^ seed
            x = (x ^ (x >> 30)) &* 0xbf58476d1ce4e5b9
            x = (x ^ (x >> 27)) &* 0x94d049bb133111eb
            return x ^ (x >> 31)
        }
        var ranked: [(row: Int, hash: UInt64)] = ids.indices.map { (row: $0, hash: hash(ids[$0])) }
        ranked.sort { left, right in
            if left.hash == right.hash { return left.row < right.row }
            return left.hash < right.hash
        }
        return ranked.prefix(keep).map { $0.row }.sorted()
    }
}

extension GeometryScene {
    /// Extract native source samples, including every visibility break. Frames
    /// of a static field do not acquire invented motion from composite clocks.
    /// Complete objects and explicitly picked elements share this one pathway.
    func buildTrajectories(objectIDs: Set<String>, elementIndices: [Int], densityPercent: Int,
                           cancellation: TrajectoryBuildCancellation,
                           progress: ((Double) -> Void)? = nil) throws -> TrajectoryGeometry {
        try cancellation.check(); try checkUnchanged()
        let children: [StreamingScene]
        if let composite = self as? CompositeScene { children = composite.children }
        else if let streaming = self as? StreamingScene { children = [streaming] }
        else { throw SceneError.invalid(L10n.tr("trajectory.unsupported", "This geometry source cannot expose identity trajectories.")) }
        guard let device = children.first?.device else { throw SceneError.invalid(L10n.tr("trajectory.noSource", "No geometry source for trajectories.")) }
        var offset = 0
        var sources = [(scene: StreamingScene, rows: [Int])]()
        for child in children {
            try cancellation.check()
            var rows = Set<Int>()
            if objectIDs.contains(child.object.id) {
                let ids = (0..<child.count).map { child.id(at: $0)! }
                rows.formUnion(TrajectoryDensity.rows(ids: ids, objectID: child.object.id, percent: densityPercent))
            }
            for index in elementIndices where index >= offset && index < offset + child.count { rows.insert(index - offset) }
            if !rows.isEmpty { sources.append((child, rows.sorted())) }
            offset += child.count
        }
        let dynamic = sources.filter { $0.scene.frameCount > 1 }
        let origin = dynamic.compactMap { $0.scene.times.first }.min() ?? 0
        let end = dynamic.compactMap { $0.scene.times.last }.max() ?? origin
        let span = end > origin ? end - origin : 1
        guard span.isFinite else { throw SceneError.invalid(L10n.tr("trajectory.clockSpan", "Trajectory clock span exceeds the native preview arithmetic range.")) }
        // Preserve positive native intervals before allocating any immutable
        // chunks. A valid Double clock can still collapse after whole-clip
        // Float32 normalization (for example 0, 1e9, 1e9+1). Publishing that
        // geometry would silently lose its final trail while the splat moves.
        for (child, _) in dynamic {
            try cancellation.check()
            var previousTime = Float((child.times[0] - origin) / span)
            for frame in 1..<child.frameCount {
                let nextTime = Float((child.times[frame] - origin) / span)
                guard previousTime.isFinite, nextTime.isFinite, nextTime > previousTime else {
                    throw SceneError.invalid(L10n.format("trajectory.clockPrecision", "Trajectory interval %@ in %@ cannot preserve its source timing at the native preview's Float32 time resolution.", "\(frame - 1)–\(frame)", child.object.label))
                }
                previousTime = nextTime
            }
        }
        // Only an allocation hint: actual segment extraction still traverses
        // every requested source sample. A single pick uses a tiny buffer.
        let maximumChunkSegments = 32 * 1_048_576 / MemoryLayout<TrajectorySegmentGPU>.stride
        let estimate = dynamic.reduce(0) { result, source in
            let frames = source.scene.frameCount - 1
            if source.rows.count > maximumChunkSegments / max(1, frames) { return maximumChunkSegments }
            return min(maximumChunkSegments, result + source.rows.count * frames)
        }
        let builder = TrajectoryChunkBuilder(device: device, cancellation: cancellation, estimatedSegments: estimate)
        let identityCount = sources.reduce(0) { $0 + $1.rows.count }
        let steps = dynamic.reduce(0) { $0 + $1.scene.frameCount - 1 }
        var completed = 0
        for (child, rows) in dynamic {
            var previous = try child.frame(0)
            for frame in 1..<child.frameCount {
                try cancellation.check()
                let next = try child.frame(frame)
                let x = previous.contents().assumingMemoryBound(to: SIMD4<Float>.self)
                let y = next.contents().assumingMemoryBound(to: SIMD4<Float>.self)
                let t0 = Float((child.times[frame - 1] - origin) / span)
                let t1 = Float((child.times[frame] - origin) / span)
                for (ordinal, row) in rows.enumerated() {
                    if ordinal & 2047 == 0 { try cancellation.check() }
                    let base = row * 4
                    guard x[base + 1].w > 0.5, y[base + 1].w > 0.5 else { continue }
                    let start = SIMD3(x[base].x, x[base].y, x[base].z)
                    let stop = SIMD3(y[base].x, y[base].y, y[base].z)
                    // An unchanged identity has no spatial path to draw. This
                    // retains its identity coverage without overdraw at a point.
                    if start == stop { continue }
                    try builder.append(TrajectorySegmentGPU(
                        startPositionTime: SIMD4(start, t0), endPositionTime: SIMD4(stop, t1),
                        startColor: SIMD4(x[base + 3].x, x[base + 3].y, x[base + 3].z, 1),
                        endColor: SIMD4(y[base + 3].x, y[base + 3].y, y[base + 3].z, 1)))
                }
                previous = next; completed += 1
                progress?(Double(completed) / Double(max(1, steps)))
            }
        }
        try cancellation.check(); try checkUnchanged()
        return TrajectoryGeometry(chunks: builder.finish(), timeOrigin: origin, timeSpan: span,
                                  identityCount: identityCount, segmentCount: builder.count)
    }
}
