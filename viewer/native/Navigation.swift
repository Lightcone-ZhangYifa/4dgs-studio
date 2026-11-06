import Foundation
import simd

/// Navigation is view state. It never modifies GSV geometry or calibrated cameras.
enum ViewportTool: String, CaseIterable { case select, orbit, pan, dolly }
enum NavigationOperation: Equatable { case select, orbit, pan, dolly }
struct NavigationModifiers: OptionSet {
  let rawValue: Int
  static let option = Self(rawValue: 1), shift = Self(rawValue: 2), control = Self(rawValue: 4)
}

/// One input policy shared by mouse buttons, toolbar tools and trackpad gestures.
/// Option+L/M/R follows Maya; MMB/Shift+MMB also supports Blender-style navigation.
enum NavigationInputPolicy {
  static func operation(button: Int, modifiers: NavigationModifiers, tool: ViewportTool)
    -> NavigationOperation
  {
    if modifiers.contains(.option) {
      if button == 1 || modifiers.contains(.control) { return .dolly }
      if button == 2 || modifiers.contains(.shift) { return .pan }
      return .orbit
    }
    if button == 2 { return modifiers.contains(.shift) ? .pan : .orbit }
    if button == 1 { return .pan }
    if modifiers.contains(.shift) { return .pan }
    return NavigationOperation(tool)
  }
  static func scrollOperation(precise: Bool, modifiers: NavigationModifiers) -> NavigationOperation
  {
    if !precise || modifiers.contains(.control) { return .dolly }
    return modifiers.contains(.option) ? .orbit : .pan
  }
}
extension NavigationOperation {
  fileprivate init(_ tool: ViewportTool) {
    switch tool {
    case .select: self = .select
    case .orbit: self = .orbit
    case .pan: self = .pan
    case .dolly: self = .dolly
    }
  }
}

/// Drag slop prevents tiny involuntary pointer movements from moving the camera.
struct NavigationDrag {
  var origin = SIMD2<Float>.zero
  var previous = SIMD2<Float>.zero
  private(set) var crossedThreshold = false
  mutating func begin(at point: SIMD2<Float>) {
    origin = point
    previous = point
    crossedThreshold = false
  }
  mutating func move(to point: SIMD2<Float>, threshold: Float = 4) -> SIMD2<Float>? {
    defer { previous = point }
    if !crossedThreshold {
      guard simd_length(point - origin) >= threshold else { return nil }
      crossedThreshold = true
      // Coalesced events can cross the threshold by hundreds of points. Consume
      // only the slop, not the complete first displacement.
      let delta = point - origin
      return delta * max(0, 1 - threshold / simd_length(delta))
    }
    return point - previous
  }
}

/// Pure camera calculations make projection and navigation invariants testable.
enum NavigationGeometry {
  /// World-up turntable with a fixed world-space pivot. Preserve the source
  /// camera's roll; never synthesize a new look-at matrix when navigation begins.
  static func turntable(
    _ pose: simd_float4x4, pivot: SIMD3<Float>, up: SIMD3<Float>, delta: SIMD2<Float>
  ) -> simd_float4x4 {
    let yaw = simd_quatf(angle: -delta.x * 0.006, axis: up)
    var result = rotate(pose, around: pivot, rotation: yaw)
    let forward = simd_normalize(vector(result[2]))
    let elevation = asin(max(-1, min(1, simd_dot(forward, up))))
    let limit = Float.pi / 2 - 0.0087
    let target = max(
      min(-limit, elevation), min(max(limit, elevation), elevation + delta.y * 0.006))
    var right = simd_cross(forward, up)
    if simd_length_squared(right) < 1e-8 {
      right = vector(result[0]) - up * simd_dot(vector(result[0]), up)
    }
    if simd_length_squared(right) > 1e-8 {
      result = rotate(
        result, around: pivot,
        rotation: simd_quatf(angle: target - elevation, axis: simd_normalize(right)))
    }
    return result
  }
  static func vector(_ value: SIMD4<Float>) -> SIMD3<Float> { SIMD3(value.x, value.y, value.z) }
  static func rigidPose(_ pose: simd_float4x4) -> simd_float4x4 {
    let rotation = simd_float3x3(columns: (vector(pose[0]), vector(pose[1]), vector(pose[2])))
    let clean = simd_float3x3(simd_normalize(simd_quatf(rotation)))
    return simd_float4x4(
      columns: (
        SIMD4(clean[0], 0), SIMD4(clean[1], 0), SIMD4(clean[2], 0), SIMD4(vector(pose[3]), 1)
      ))
  }
  static func rotate(_ pose: simd_float4x4, around pivot: SIMD3<Float>, rotation: simd_quatf)
    -> simd_float4x4
  {
    var value = pose
    for column in 0..<3 { value[column] = SIMD4(rotation.act(vector(pose[column])), 0) }
    value[3] = SIMD4(pivot + rotation.act(vector(pose[3]) - pivot), 1)
    return rigidPose(value)
  }
  /// Pixel deltas are expressed in the same viewport dimensions as K.
  static func panShift(pose: simd_float4x4, delta: SIMD2<Float>, depth: Float, k: simd_float3x3)
    -> SIMD3<Float>
  {
    let y = delta.y / k[1, 1]
    let x = (delta.x - k[1, 0] * y) / k[0, 0]
    return -(vector(pose[0]) * x + vector(pose[1]) * y) * depth
  }
  static func pointOnPlane(pose: simd_float4x4, pixel: SIMD2<Float>, depth: Float, k: simd_float3x3)
    -> SIMD3<Float>
  {
    let y = (pixel.y - k[2, 1]) / k[1, 1]
    let x = (pixel.x - k[2, 0] - k[1, 0] * y) / k[0, 0]
    return vector(pose[3]) + (vector(pose[0]) * x + vector(pose[1]) * y + vector(pose[2])) * depth
  }
  static func upAxis(_ name: String) -> SIMD3<Float>? {
    switch name.lowercased() {
    case "x", "+x": return SIMD3(1, 0, 0)
    case "-x": return SIMD3(-1, 0, 0)
    case "y", "+y": return SIMD3(0, 1, 0)
    case "-y": return SIMD3(0, -1, 0)
    case "z", "+z": return SIMD3(0, 0, 1)
    case "-z": return SIMD3(0, 0, -1)
    default: return nil
    }
  }
  /// A proper orientation for every signed world-up axis. Data is untouched.
  static func worldBasis(up: SIMD3<Float>) -> simd_float3x3 {
    let back: SIMD3<Float> = abs(up.z) > 0.9 ? SIMD3(0, -1, 0) : SIMD3(0, 0, 1)
    let right = simd_normalize(simd_cross(up, back))
    return simd_float3x3(columns: (right, up, simd_cross(right, up)))
  }
}
