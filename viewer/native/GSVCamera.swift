import Foundation
import simd

struct GSVCamera {
  let object: GSVObject
  let times: [Double]
  let viewCount: Int
  let intrinsics: [Double]
  let poses: [Double]
  let imageSizes: [Int32]
  let integerPixelCenters: Bool
  var viewIDs: [String] = []
  var distortionModel = "none"
  var depthMapping: [String:Any] = [:]
  var optics: [String:[Double]] = [:]
  var acquisition: [String:[Double]] = [:]
  var rollingDirection: String?
  var volumeNear: [Double]?
  var volumeFar: [Double]?
  var volumeUnbounded: [Bool]?
  var supportsCalibratedRendering: Bool { distortionModel == "none" }
  init(
    object: GSVObject, times: [Double], viewCount: Int, intrinsics: [Double], poses: [Double],
    imageSizes: [Int32], integerPixelCenters: Bool
  ) {
    self.object = object
    self.times = times
    self.viewCount = viewCount
    self.intrinsics = intrinsics
    self.poses = poses
    self.imageSizes = imageSizes
    self.integerPixelCenters = integerPixelCenters
    self.viewIDs = (0..<viewCount).map{"view-" + String($0)}
  }
  init(document: GSVDocument, object: GSVObject) throws {
    guard object.space != nil, object.clock != nil else {
      throw SceneError.invalid(
        "Spatial and temporal objects require explicit space and clock references.")
    }
    guard object.type == "camera", object.encoding == "gsv.camera.pinhole",
      let timesID = object.data["times"] as? String,
      let projection = object.data["projection"] as? [String:Any],
      let pose = object.data["pose"] as? [String:Any],
      let kID = projection["intrinsics"] as? String,
      let poseID = pose["camera_to_world"] as? String,
      let sizeID = projection["image_sizes"] as? String,
      let viewIDs = object.data["view_ids"] as? [String],
      let distortion = projection["distortion"] as? [String:Any],
      let distortionModel = distortion["model"] as? String,
      let pixel = projection["pixel_center"] as? String, ["integer", "half"].contains(pixel)
    else { throw SceneError.invalid("Invalid pinhole camera object: \(object.label)") }
    try GSVDocument.fields(object.data,["times","view_ids","projection","pose"],optional:["view_volume","optics","acquisition"],context:"camera")
    try GSVDocument.fields(projection,["intrinsics","image_sizes","pixel_center","distortion"],optional:["depth_mapping"],context:"camera projection")
    try GSVDocument.fields(pose,["camera_to_world"],context:"camera pose")
    try GSVDocument.fields(distortion,distortionModel == "none" ? ["model"] : ["model","coefficients"],context:"camera distortion")
    let ta = try document.array(timesID)
    let ka = try document.array(kID)
    let pa = try document.array(poseID)
    let sa = try document.array(sizeID)
    guard ta.dtype == "f64", ta.shape.count == 1, ta.shape[0] > 0, ka.dtype == "f64",
      ka.shape.count == 4,
      ka.shape[0] == ta.shape[0], ka.shape[1] > 0, ka.shape[2...3].elementsEqual([3, 3]),
      pa.dtype == "f64", pa.shape == [ta.shape[0], ka.shape[1], 4, 4], sa.dtype == "i32",
      sa.shape == [ka.shape[1], 2]
    else { throw SceneError.invalid("Camera arrays have incompatible shapes: \(object.label)") }
    let t = try document.doubles(timesID)
    let k = try document.doubles(kID)
    let p = try document.doubles(poseID)
    let sizeData = try document.readArray(sizeID)
    let sizes = sizeData.withUnsafeBytes { raw in
      (0..<sizeData.count / 4).map {
        Int32(littleEndian: raw.loadUnaligned(fromByteOffset: $0 * 4, as: Int32.self))
      }
    }
    guard t.allSatisfy({ $0.isFinite }), zip(t.dropFirst(), t).allSatisfy({ $0 > $1 }),
      k.allSatisfy({ $0.isFinite }), p.allSatisfy({ $0.isFinite }), sizes.allSatisfy({ $0 > 0 })
    else {
      throw SceneError.invalid(
        "Camera values must be finite with increasing times and positive image sizes.")
    }
    // The container preserves f64 source values. This evaluator has a float GPU
    // boundary: reject unsupported ranges instead of handing infinity to Metal.
    guard k.allSatisfy({ Float($0).isFinite }), p.allSatisfy({ Float($0).isFinite }) else {
      throw SceneError.invalid(
        "Camera \(object.label) exceeds the native renderer's numeric range. Its stored double-precision data remains unchanged."
      )
    }
    for entry in 0..<t.count * ka.shape[1] {
      let b = entry * 9
      let q = entry * 16
      guard k[b] > 0, k[b + 4] > 0, abs(k[b + 3]) < 1e-9, abs(k[b + 6]) < 1e-9,
        abs(k[b + 7]) < 1e-9, abs(k[b + 8] - 1) < 1e-9,
        abs(p[q + 12]) < 1e-9, abs(p[q + 13]) < 1e-9, abs(p[q + 14]) < 1e-9,
        abs(p[q + 15] - 1) < 1e-9
      else { throw SceneError.invalid("Camera K or pose homogeneous row is invalid.") }
      guard Float(k[b]) > 0, Float(k[b + 4]) > 0 else {
        throw SceneError.invalid(
          "Camera \(object.label) has a focal length below the native renderer's numeric range.")
      }
      var r = matrix_identity_double3x3
      for row in 0..<3 { for col in 0..<3 { r[col][row] = p[q + row * 4 + col] } }
      let error = r.transpose * r - matrix_identity_double3x3
      guard (0..<3).allSatisfy({ simd_length(error[$0]) < 1e-4 }),
        abs(simd_determinant(r) - 1) < 1e-4
      else { throw SceneError.invalid("Camera rotation is not a proper orthonormal rotation.") }
    }
    guard viewIDs.count == ka.shape[1], Set(viewIDs).count == viewIDs.count,
      viewIDs.allSatisfy({!$0.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty}) else {throw SceneError.invalid("Camera view IDs must be unique nonempty strings aligned with the rig.")}
    func readTrack(_ id:Any?,_ label:String) throws->[Double] {
      guard let id = id as? String else {throw SceneError.invalid("Camera " + label + " requires a typed array reference.")}
      let a = try document.array(id)
      guard a.dtype == "f64",a.shape == [t.count,ka.shape[1]] else {throw SceneError.invalid("Camera " + label + " requires f64[T,V].")}
      let values = try document.doubles(id)
      guard values.allSatisfy({$0.isFinite}) else {throw SceneError.invalid("Camera track values must be finite.")}
      return values
    }
    if distortionModel != "none" {
      guard let d = ["opencv-brown":5,"opencv-fisheye":4][distortionModel],let id = distortion["coefficients"] as? String else {throw SceneError.invalid("Unknown camera distortion model.")}
      let a = try document.array(id)
      guard a.dtype == "f64",a.shape == [t.count,ka.shape[1],d],try document.doubles(id).allSatisfy({$0.isFinite}) else {throw SceneError.invalid("Invalid camera distortion coefficient track.")}
    } else if distortion["coefficients"] != nil {throw SceneError.invalid("A distortion-free camera cannot contain distortion coefficients.")}
    if let value = projection["depth_mapping"] {
      guard let d = value as? [String:Any],let near = GSVDocument.number(d["near"]),near > 0,
        d["far"] is NSNull || (GSVDocument.number(d["far"]).map{$0 > near} ?? false),
        ["zero-to-one","minus-one-to-one"].contains(d["ndc_range"] as? String ?? ""),
        ["forward","reversed"].contains(d["direction"] as? String ?? "") else {throw SceneError.invalid("Invalid descriptive projection depth mapping.")}
      try GSVDocument.fields(d,["near","far","ndc_range","direction"],context:"depth mapping")
      depthMapping = d
    }
    if let value = object.data["optics"] {
      guard let values = value as? [String:Any] else {throw SceneError.invalid("Camera optics must be a map.")}
      for (key,id) in values {
        guard ["focal_length_mm","sensor_width_mm","sensor_height_mm","f_number","focus_distance"].contains(key) else {throw SceneError.invalid("Unknown camera optics track.")}
        let values = try readTrack(id,key)
        guard values.allSatisfy({$0 > 0}) else {throw SceneError.invalid("Camera optics must be positive.")}
        optics[key] = values
      }
    }
    if let value = object.data["acquisition"] {
      guard let values = value as? [String:Any] else {throw SceneError.invalid("Camera acquisition must be a map.")}
      for (key,id) in values where key != "rolling_direction" {
        guard ["exposure_seconds","iso","shutter_open","shutter_close","rolling_readout"].contains(key) else {throw SceneError.invalid("Unknown camera acquisition track.")}
        acquisition[key] = try readTrack(id,key)
      }
      for key in ["exposure_seconds","iso"] {if let a = acquisition[key],!a.allSatisfy({$0 > 0}) {throw SceneError.invalid("Exposure and ISO must be positive.")}}
      guard (acquisition["shutter_open"] == nil) == (acquisition["shutter_close"] == nil) else {throw SceneError.invalid("Shutter open/close must be paired.")}
      if let a = acquisition["shutter_open"],let b = acquisition["shutter_close"],!zip(a,b).allSatisfy({$0 <= $1}) {throw SceneError.invalid("Shutter closes before it opens.")}
      if let raw = values["rolling_direction"] {
        guard let v = raw as? String,["top-to-bottom","bottom-to-top","left-to-right","right-to-left"].contains(v) else {throw SceneError.invalid("Invalid rolling readout direction.")}
        rollingDirection = v
      }
      if let readout = acquisition["rolling_readout"] {
        guard readout.allSatisfy({$0 >= 0}),!readout.contains(where:{$0 > 0}) || rollingDirection != nil else {throw SceneError.invalid("Rolling readout requires nonnegative timing and a direction.")}
      }
    }
    if let value = object.data["view_volume"] {
      guard let v = value as? [String:Any],let id = v["far_unbounded"] as? String else {throw SceneError.invalid("Invalid camera view volume.")}
      try GSVDocument.fields(v,["near","far","far_unbounded"],context:"camera volume")
      let near = try readTrack(v["near"],"view_volume.near"),far = try readTrack(v["far"],"view_volume.far"),a = try document.array(id),bytes = try document.readArray(id)
      guard a.dtype == "bool",a.shape == [t.count,ka.shape[1]],bytes.allSatisfy({$0 <= 1}) else {throw SceneError.invalid("Camera far-unbounded mask requires bool[T,V].")}
      let flags = bytes.map{$0 == 1}
      guard near.indices.allSatisfy({near[$0] >= 0 && (flags[$0] ? far[$0] == 0 : far[$0] > near[$0])}) else {throw SceneError.invalid("Camera view volume interval is invalid.")}
      volumeNear = near;volumeFar = far;volumeUnbounded = flags
    }
    self.viewIDs = viewIDs;self.distortionModel = distortionModel
    self.object = object
    times = t
    viewCount = ka.shape[1]
    intrinsics = k
    poses = p
    imageSizes = sizes
    integerPixelCenters = pixel == "integer"
  }
  func at(time: Double, view: Int) -> (pose: simd_float4x4, k: simd_float3x3, size: SIMD2<Float>) {
    var low = 0
    var high = times.count
    while low < high {
      let m = (low + high) / 2
      if times[m] <= time { low = m + 1 } else { high = m }
    }
    let a = max(0, min(low - 1, times.count - 1))
    let b = min(a + 1, times.count - 1)
    let mix: Double
    if a == b || time.isNaN || time <= times[a] {
      mix = 0
    } else if time >= times[b] {
      mix = 1
    } else {
      let span = times[b] - times[a]
      // A finite f64 clock can straddle +/-Double.max. Halving all operands
      // keeps its fraction defined without changing the source time domain.
      mix =
        span.isFinite
        ? (time - times[a]) / span
        : (time * 0.5 - times[a] * 0.5) / (times[b] * 0.5 - times[a] * 0.5)
    }
    func interpolate(_ av: Double, _ bv: Double) -> Float {
      // Validated endpoints fit Float, but bv-av may not. Do all linear
      // interpolation in Double, then cross the rendering boundary once.
      if mix == 0 { return Float(av) }
      if mix == 1 { return Float(bv) }
      return Float(av * (1 - mix) + bv * mix)
    }
    func pose(_ frame: Int) -> simd_float4x4 {
      var value = matrix_identity_float4x4
      let offset = (frame * viewCount + view) * 16
      for r in 0..<4 { for c in 0..<4 { value[c][r] = Float(poses[offset + r * 4 + c]) } }
      return value
    }
    let first = pose(a)
    let second = pose(b)
    let ra = simd_float3x3(
      columns: (
        SIMD3(first[0].x, first[0].y, first[0].z), SIMD3(first[1].x, first[1].y, first[1].z),
        SIMD3(first[2].x, first[2].y, first[2].z)
      ))
    let rb = simd_float3x3(
      columns: (
        SIMD3(second[0].x, second[0].y, second[0].z), SIMD3(second[1].x, second[1].y, second[1].z),
        SIMD3(second[2].x, second[2].y, second[2].z)
      ))
    var out = simd_float4x4(simd_slerp(simd_quatf(ra), simd_quatf(rb), Float(mix)))
    for r in 0..<3 {
      out[3][r] = interpolate(
        poses[(a * viewCount + view) * 16 + r * 4 + 3],
        poses[(b * viewCount + view) * 16 + r * 4 + 3])
    }
    var k = matrix_identity_float3x3
    for r in 0..<3 {
      for c in 0..<3 {
        k[c][r] = interpolate(
          intrinsics[(a * viewCount + view) * 9 + r * 3 + c],
          intrinsics[(b * viewCount + view) * 9 + r * 3 + c])
      }
    }
    return (out, k, SIMD2(Float(imageSizes[view * 2]), Float(imageSizes[view * 2 + 1])))
  }
}
