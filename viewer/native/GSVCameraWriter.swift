import CryptoKit
import Foundation
import simd

struct CameraKeyframe {
  let time: Double
  let pose: simd_float4x4
  let intrinsics: simd_float3x3
  var nearClip: Float = 0.001
  var farClip: Float? = nil
}

struct AuthoredCamera {
  let id: String
  var label: String
  let space: String
  let clock: String
  let imageSize: SIMD2<Int32>
  var keyframes: [CameraKeyframe]
  init(importing camera: GSVCamera) throws {
    guard camera.viewCount == 1,camera.supportsCalibratedRendering,camera.optics.isEmpty,camera.acquisition.isEmpty,camera.depthMapping.isEmpty, camera.object.metadata["role"] as? String == "user",
      let space = camera.object.space, let clock = camera.object.clock
    else {
      throw SceneError.invalid(
        "Editable camera import requires one-view user cameras with explicit space and clock.")
    }
    id = camera.object.id
    label = camera.object.label
    self.space = space
    self.clock = clock
    imageSize = SIMD2(camera.imageSizes[0], camera.imageSizes[1])
    keyframes = camera.times.enumerated().map { sample,time in
      let value = camera.at(time: time, view: 0)
      var k = value.k
      if camera.integerPixelCenters {
        k[2][0] += 0.5
        k[2][1] += 0.5
      }
      return CameraKeyframe(time: time, pose: Self.affine(value.pose), intrinsics: k,
        nearClip:Float(camera.volumeNear?[sample] ?? 0.001),farClip:(camera.volumeUnbounded?[sample] ?? true) ? nil : camera.volumeFar.map{Float($0[sample])})
    }
  }
  private static func affine(_ input: simd_float4x4) -> simd_float4x4 {
    var pose = input
    // A viewport inverse is Float32; write its exact affine homogeneous row.
    for c in 0..<3 { pose[c][3] = 0 }
    pose[3][3] = 1
    return pose
  }
  init(
    label: String, space: String?, clock: String?, time: Double, pose: simd_float4x4,
    intrinsics: simd_float3x3, imageSize: SIMD2<Float>, nearClip: Float = 0.001
  ) {
    id = "camera-" + UUID().uuidString.lowercased()
    self.label = label
    self.space = space ?? "world"
    self.clock = clock ?? "timeline"
    self.imageSize = SIMD2(Int32(imageSize.x.rounded()), Int32(imageSize.y.rounded()))
    keyframes = [CameraKeyframe(time: time, pose: Self.affine(pose), intrinsics: intrinsics,nearClip:nearClip)]
  }
  mutating func record(
    time: Double, pose: simd_float4x4, intrinsics: simd_float3x3, imageSize: SIMD2<Float>, nearClip: Float = 0.001
  ) {
    var k = intrinsics
    let scale = SIMD2(Float(self.imageSize.x) / imageSize.x, Float(self.imageSize.y) / imageSize.y)
    for c in 0..<3 {
      k[c][0] *= scale.x
      k[c][1] *= scale.y
    }
    keyframes.removeAll { $0.time == time }
    keyframes.append(CameraKeyframe(time: time, pose: Self.affine(pose), intrinsics: k,nearClip:nearClip))
    keyframes.sort { $0.time < $1.time }
  }
  func camera() -> GSVCamera {
    let object = GSVObject(
      id: id, type: "camera", encoding: "gsv.camera.pinhole", label: label, space: space,
      clock: clock, data: [:],
      metadata: [
        "role": "user", "interpolation": "translation/K linear; rotation shortest-arc SLERP",
      ], descriptor: [:])
    let ks = keyframes.flatMap { frame in
      (0..<3).flatMap { r in (0..<3).map { Double(frame.intrinsics[$0][r]) } }
    }
    let ps = keyframes.flatMap { frame in
      (0..<4).flatMap { r in (0..<4).map { Double(frame.pose[$0][r]) } }
    }
    var camera = GSVCamera(
      object: object, times: keyframes.map { $0.time }, viewCount: 1, intrinsics: ks, poses: ps,
      imageSizes: [imageSize.x, imageSize.y], integerPixelCenters: false)
    camera.volumeNear = keyframes.map{Double($0.nearClip)}
    camera.volumeFar = keyframes.map{Double($0.farClip ?? 0)}
    camera.volumeUnbounded = keyframes.map{$0.farClip == nil}
    return camera
  }
}

/// Camera authoring produces a separate standards-conforming camera document.
/// It never edits or repacks the source reconstruction.
enum GSVCameraWriter {
  static func write(
    _ tracks: [AuthoredCamera], to destination: URL, source: GSVDocument?,
    spaces additionalSpaces: [String: Any] = [:], clocks additionalClocks: [String: Any] = [:]
  ) throws {
    let manager = FileManager.default
    if let source,
      destination.resolvingSymlinksInPath().standardizedFileURL
        == source.url.resolvingSymlinksInPath().standardizedFileURL
    {
      throw SceneError.invalid(
        "Save camera animation to a separate GSV file. The source document cannot be overwritten.")
    }
    let temporary = destination.deletingLastPathComponent().appendingPathComponent(
      ".gsv-camera-" + UUID().uuidString)
    guard manager.createFile(atPath: temporary.path, contents: nil) else {
      throw SceneError.invalid("Cannot create camera export.")
    }
    defer { try? manager.removeItem(at: temporary) }
    let handle = try FileHandle(forWritingTo: temporary)
    defer { try? handle.close() }
    try handle.write(contentsOf: Data(count: 64))
    var offset = 64
    var buffers = [String: [String: Any]]()
    var arrays = [String: [String: Any]]()
    var objects = [[String: Any]]()
    func add(_ name: String, _ data: Data, _ dtype: String, _ shape: [Int]) throws -> String {
      let aligned = ((offset + 16383) / 16384) * 16384
      try handle.write(contentsOf: Data(count: aligned - offset))
      try handle.write(contentsOf: data)
      buffers[name] = ["offset": aligned, "length": data.count, "sha256": GSVDocument.digest(data)]
      arrays[name] = ["buffer": name, "dtype": dtype, "shape": shape]
      offset = aligned + data.count
      return name
    }
    func doubles(_ values: [Double]) -> Data {
      var data = Data(capacity: values.count * 8)
      for value in values {
        var bits = value.bitPattern.littleEndian
        withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
      }
      return data
    }
    func ints(_ values: [Int32]) -> Data {
      var data = Data(capacity: values.count * 4)
      for value in values {
        var bits = value.littleEndian
        withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
      }
      return data
    }
    for track in tracks {
      let camera = track.camera()
      let prefix = track.id + "/"
      let t = camera.times.count
      let times = try add(prefix + "times", doubles(camera.times), "f64", [t])
      let k = try add(prefix + "intrinsics", doubles(camera.intrinsics), "f64", [t, 1, 3, 3])
      let pose = try add(prefix + "c2w", doubles(camera.poses), "f64", [t, 1, 4, 4])
      let size = try add(prefix + "image_sizes", ints(camera.imageSizes), "i32", [1, 2])
      let near = try add(prefix + "volume_near",doubles(camera.volumeNear!),"f64",[t,1])
      let far = try add(prefix + "volume_far",doubles(camera.volumeFar!),"f64",[t,1])
      let unbounded = try add(prefix + "volume_unbounded",Data(camera.volumeUnbounded!.map{$0 ? UInt8(1):UInt8(0)}),"bool",[t,1])
      objects.append([
        "id": track.id, "type": "camera", "encoding": "gsv.camera.pinhole", "label": track.label,
        "space": track.space, "clock": track.clock,
        "data": [
          "times": times,"view_ids":camera.viewIDs,
          "projection":["intrinsics":k,"image_sizes":size,"pixel_center":"half","distortion":["model":"none"]],
          "pose":["camera_to_world":pose],
          "view_volume":["near":near,"far":far,"far_unbounded":unbounded],
        ],
        "metadata": [
          "role": "user", "created_by": "4DGS Studio",
          "interpolation": "translation/K linear; rotation shortest-arc SLERP",
        ],
      ])
    }
    var spaces = source?.manifest["spaces"] as? [String: Any] ?? [:]
    var clocks = source?.manifest["clocks"] as? [String: Any] ?? [:]
    spaces.merge(additionalSpaces) { existing, _ in existing }
    clocks.merge(additionalClocks) { existing, _ in existing }
    for track in tracks {
      if spaces[track.space] == nil {
        spaces[track.space] = ["unit": "unitless", "handedness": "right", "axes": ["x", "y", "z"]]
      }
      if clocks[track.clock] == nil { clocks[track.clock] = ["unit": "second"] }
    }
    var metadata: [String: Any] = [
      "created_by": "4DGS Studio",
      "purpose": "Authored cameras; reconstruction source remains unchanged.",
    ]
    if let source {
      if source.objects.allSatisfy({ $0.type == "camera" }),
        let inherited = (source.manifest["metadata"] as? [String: Any])?["source_document"]
          as? [String: Any]
      {
        metadata["source_document"] = inherited
      } else {
        metadata["source_document"] = [
          "name": source.url.lastPathComponent, "manifest_sha256": source.manifestSHA256,
        ]
      }
    }
    let manifest: [String: Any] = [
      "format": "gsv.scene", "objects": objects, "buffers": buffers, "arrays": arrays,
      "spaces": spaces, "clocks": clocks, "metadata": metadata,
    ]
    let data = try JSONSerialization.data(
      withJSONObject: manifest, options: [.sortedKeys, .withoutEscapingSlashes])
    guard data.count <= 64 * 1024 * 1024 else {
      throw SceneError.invalid("Camera manifest exceeds the GSV limit.")
    }
    let manifestOffset = ((offset + 16383) / 16384) * 16384
    try handle.write(contentsOf: Data(count: manifestOffset - offset))
    try handle.write(contentsOf: data)
    var header = Data([0x47, 0x53, 0x56, 0, 0x0d, 0x0a, 0x1a, 0x0a])
    for value in [manifestOffset + data.count, manifestOffset, data.count] {
      var v = UInt64(value).littleEndian
      withUnsafeBytes(of: &v) { header.append(contentsOf: $0) }
    }
    header.append(contentsOf: SHA256.hash(data: data))
    try handle.seek(toOffset: 0)
    try handle.write(contentsOf: header)
    try handle.synchronize()
    try handle.close()
    let verify = try GSVDocument(url: temporary)
    for object in verify.objects { _ = try GSVCamera(document: verify, object: object) }
    if manager.fileExists(atPath: destination.path) {
      _ = try manager.replaceItemAt(destination, withItemAt: temporary)
    } else {
      try manager.moveItem(at: temporary, to: destination)
    }
  }
}
