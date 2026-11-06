import Foundation
import Metal
import simd

@main struct TrajectoryExtractionProbe {
    static func main() throws {
        let device = MTLCreateSystemDefaultDevice()!
        let url = URL(fileURLWithPath: CommandLine.arguments[1])
        let document = try GSVDocument(url: url)
        let children = try document.objects.filter(StreamingScene.supports).map {
            try StreamingScene(document: document, object: $0, device: device)
        }
        let scene = try CompositeScene(children: children, device: device)
        var checks = [[String: Any]]()
        func check(_ passed: Bool, _ name: String) { checks.append(["name": name, "passed": passed]) }
        let full = try scene.buildTrajectories(objectIDs: ["colored", "stationary"], elementIndices: [],
                                               densityPercent: 100, cancellation: TrajectoryBuildCancellation())
        check(full.identityCount == 7 && full.segmentCount == 33, "100% identities, all native samples, stationary support without zero-length lines")
        var colors = Set<String>()
        var crossesGap = false
        var attributesAligned = true
        for chunk in full.chunks {
            let segments = chunk.buffer.contents().assumingMemoryBound(to: TrajectorySegmentGPU.self)
            for index in 0..<chunk.segmentCount {
                let segment = segments[index]
                colors.insert(String(segment.startColor.x))
                attributesAligned = attributesAligned && segment.startColor.y == segment.startPositionTime.w && segment.endColor.y == segment.endPositionTime.w
                if abs(segment.startColor.x - Float(1.0 / 6.0)) < 1e-6,
                   (segment.startPositionTime.w == Float(0.08) || segment.startPositionTime.w == Float(0.17)) { crossesGap = true }
            }
        }
        check(attributesAligned, "Source RGB endpoints and normalized native times stay aligned")
        check(colors.count == 5 && !crossesGap, "Per-identity colors and validity gaps remain distinct")
        let one = try scene.buildTrajectories(objectIDs: [], elementIndices: [1], densityPercent: 0,
                                              cancellation: TrajectoryBuildCancellation())
        check(one.identityCount == 1 && one.segmentCount == 5 && one.byteCount == 7 * 64,
              "Explicit pick survives 0% density and uses a small immutable allocation")
        let ids = (0..<1000).map { Int64($0 - 500) }
        let fifty = Set(TrajectoryDensity.rows(ids: ids, objectID: "source", percent: 50))
        let ten = Set(TrajectoryDensity.rows(ids: ids, objectID: "source", percent: 10))
        check(fifty.count == 500 && ten.count == 100 && ten.isSubset(of: fifty), "Exact, deterministic nested identity density")
        check(TrajectoryDensity.rows(ids: ids, objectID: "source", percent: 100) == Array(ids.indices), "Default density retains every identity")
        let cancel = TrajectoryBuildCancellation(); cancel.cancel()
        var rejected = false
        do { _ = try scene.buildTrajectories(objectIDs: ["colored"], elementIndices: [], densityPercent: 100, cancellation: cancel) }
        catch TrajectoryBuildError.cancelled { rejected = true }
        check(rejected, "Cancelled request cannot publish stale path geometry")
        let precision = try StreamingScene(url: url.deletingLastPathComponent().appendingPathComponent("trajectory_time_precision.gsv"), device: device)
        check(precision.frameCount == 3 && precision.times == [0, 1e9, 1e9 + 1],
              "Nonuniform Double clock is valid source data with all three native samples")
        var precisionRejected = false
        var precisionError = ""
        do {
            _ = try precision.buildTrajectories(objectIDs: [precision.object.id], elementIndices: [],
                densityPercent: 100, cancellation: TrajectoryBuildCancellation())
        } catch SceneError.invalid(let message) {
            precisionError = message
            precisionRejected = message.contains("Float32") && message.contains("clock-track")
        }
        check(precisionRejected, "A native interval collapsed by Float32 normalization fails explicitly before publication")
        let epoch = try StreamingScene(url: url.deletingLastPathComponent().appendingPathComponent("trajectory_epoch_offset.gsv"), device: device)
        let epochGeometry = try epoch.buildTrajectories(objectIDs: [epoch.object.id], elementIndices: [],
            densityPercent: 100, cancellation: TrajectoryBuildCancellation())
        check(epochGeometry.identityCount == 1 && epochGeometry.segmentCount == 2
              && epochGeometry.timeOrigin == 1.7e9 && epochGeometry.timeSpan == 2,
              "Large absolute epoch retains every native interval after origin normalization")
        // External editors may replace or truncate a scene during playback.
        // A frame already submitted to Metal must remain readable in either case.
        let copyURL = url.deletingLastPathComponent().appendingPathComponent("mutated-input.gsv")
        try FileManager.default.copyItem(at: url, to: copyURL)
        let mutableSource = try StreamingScene(url: copyURL, device: device)
        let retained = try mutableSource.frame(0)
        let expected = Data(bytes: retained.contents(), count: retained.length)
        let handle = try FileHandle(forWritingTo: copyURL)
        try handle.truncate(atOffset: 0)
        try handle.close()
        let queue = device.makeCommandQueue()!
        let command = queue.makeCommandBuffer()!
        let blit = command.makeBlitCommandEncoder()!
        let output = device.makeBuffer(length: retained.length, options: .storageModeShared)!
        blit.copy(from: retained, sourceOffset: 0, to: output, destinationOffset: 0, size: retained.length)
        blit.endEncoding()
        command.commit()
        command.waitUntilCompleted()
        check(command.status == .completed && Data(bytes: output.contents(), count: output.length) == expected,
              "GPU can read an already loaded frame after external file truncation")
        do {
            _ = try mutableSource.frame(0)
            check(false, "Source change invalidates even a cached frame")
        } catch SceneError.invalid {
            check(true, "Source change invalidates even a cached frame")
        }
        let report: [String: Any] = ["checks": checks, "passed": checks.allSatisfy { $0["passed"] as? Bool == true },
                                    "identity_count": full.identityCount, "segment_count": full.segmentCount,
                                    "trajectory_bytes": full.byteCount,
                                    "precision_rejection": ["stored_times": precision.times, "message": precisionError],
                                    "epoch_offset": ["stored_times": epoch.times, "segment_count": epochGeometry.segmentCount]]
        let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
        print(String(data: data, encoding: .utf8)!)
        if !(report["passed"] as! Bool) { exit(1) }
    }
}
