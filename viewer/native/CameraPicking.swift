import simd

/// A visible camera wireframe edge, independent of decorative motion paths.
/// The stable request-local camera ID is resolved by the controller, never
/// interpreted as a Gaussian row. Three float4/uint4 blocks form a 48-byte ABI.
struct CameraPickSegmentGPU {
    let startPosition: SIMD4<Float>
    let endPosition: SIMD4<Float>
    let identity: SIMD4<UInt32>
    var cameraID: UInt32 { identity.x }

    init(startPosition: SIMD4<Float>, endPosition: SIMD4<Float>, cameraID: UInt32) {
        self.startPosition = startPosition
        self.endPosition = endPosition
        identity = SIMD4(cameraID, 0, 0, 0)
    }
}

/// One depth-resolved result from an immutable displayed scene snapshot.
enum ScenePickResult: Equatable {
    case gaussian(UInt32)
    case camera(UInt32)
    case empty
}
