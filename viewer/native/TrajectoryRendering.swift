import Metal
import simd

/// Independent line segments preserve gaps and per-identity endpoint colors.
/// Times are normalized against the geometry's Double origin/span, keeping
/// large source clocks out of Float32 shader arithmetic. Exactly64 bytes.
struct TrajectorySegmentGPU {
    var startPositionTime: SIMD4<Float>
    var endPositionTime: SIMD4<Float>
    var startColor: SIMD4<Float>
    var endColor: SIMD4<Float>
}

struct TrajectoryTimeRange {
    let firstSegment: Int
    var segmentCount: Int
    let startTime: Float
    let endTime: Float
}

struct TrajectoryChunk {
    let buffer: MTLBuffer
    let segmentCount: Int
    let timeRanges: [TrajectoryTimeRange]
    init(buffer: MTLBuffer, segmentCount: Int, timeRanges: [TrajectoryTimeRange] = []) {
        self.buffer = buffer; self.segmentCount = segmentCount; self.timeRanges = timeRanges
    }
    var hasCompleteTimeRanges: Bool {
        if timeRanges.isEmpty { return true }
        var next = 0
        for range in timeRanges {
            guard range.firstSegment == next, range.segmentCount > 0,
                  range.segmentCount <= segmentCount-next else { return false }
            next += range.segmentCount
        }
        return next == segmentCount
    }
}

/// Immutable chunks can be shared by three in-flight frames without copying.
struct TrajectoryGeometry {
    let chunks: [TrajectoryChunk]
    let timeOrigin: Double
    let timeSpan: Double
    let identityCount: Int
    let segmentCount: Int
    var byteCount: Int { chunks.reduce(0) { $0 + $1.buffer.length } }
    func normalizedTime(_ time: Double) -> Float {
        guard time.isFinite, timeOrigin.isFinite, timeSpan.isFinite, timeSpan > 0 else { return 0 }
        return Float(min(1, max(0, (time-timeOrigin)/timeSpan)))
    }
}

/// Preview-only state, independent of source data and scientific captures.
struct TrajectoryDisplayOptions: Equatable {
    var showPaths = true
    var showTrails = true
    var pathOpacity: Float = 0.18
    var trailFrames = 8
    var densityPercent = 100
}

/// Native intervals, not an inferred capture rate. Before the window fills,
/// its tail remains at the first sample; afterwards both boundaries advance
/// continuously with the committed playhead's interval phase.
enum TrajectoryPlayback {
    static func trailStart(time: Double, times: [Double], frames: Int) -> Double {
        guard let first = times.first else { return time }
        guard frames > 0 else { return time }
        var low = 0, high = times.count - 1
        while low < high {
            let mid = (low + high + 1) / 2
            if times[mid] <= time { low = mid } else { high = mid - 1 }
        }
        let lower = low
        guard lower >= frames else { return first }
        let start = lower - frames
        guard lower + 1 < times.count else { return times[start] }
        let span = times[lower + 1] - times[lower]
        let fraction = span.isFinite ? (time - times[lower]) / span
            : (time * 0.5 - times[lower] * 0.5) / (times[lower + 1] * 0.5 - times[lower] * 0.5)
        let phase = min(1, max(0, fraction))
        return times[start] * (1 - phase) + times[start + 1] * phase
    }
}
