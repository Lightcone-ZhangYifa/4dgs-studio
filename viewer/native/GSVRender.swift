import Foundation
import simd

/// Typed source algorithm policy. Storage validation is distinct from native
/// rendering capability: Metal reports its remaining compositor differences.
struct GSVRenderProfile {
  let object: GSVObject
  let near: Float
  let far: Float?
  let nearInclusive: Bool
  let farInclusive: Bool
  let cameraVolume: Bool
  let pixelOffset: SIMD2<Float>
  let epsilon: Float
  let jacobianMode: String
  let jacobianFactor: Float
  let dilation: Float
  let radiusClip: Float
  let antialias: Bool
  let bounds: String
  let sigma: Float
  let tile: [Int]
  let mahalanobis: Float?
  let alphaMaximum: Float
  let alphaMinimum: Float
  let depthSort: String
  let transmittance: Float
  let transmittanceInclusive: Bool

  init(object: GSVObject) throws {
    let d = object.data
    guard object.type == "render",object.encoding == "gsv.render.profile",d["algorithm"] as? String == "ewa-splat",
      let visibility = d["visibility"] as? [String:Any],let center = visibility["center_depth"] as? [String:Any],
      let cameraVolume = visibility["camera_volume"] as? String,["ignore","gaussian-center"].contains(cameraVolume),
      let projection = d["projection"] as? [String:Any],let jacobian = projection["jacobian"] as? [String:Any],
      let mode = jacobian["mode"] as? String,["symmetric-fov","image-bounds","unclamped"].contains(mode),
      let filter = d["filter"] as? [String:Any],let support = d["support"] as? [String:Any],
      let bounds = support["bounds"] as? String,["tile-major-radius","opacity-aware"].contains(bounds),
      let tile = support["tile_size"] as? [Any],tile.count == 2,
      let alpha = d["alpha"] as? [String:Any],let compositing = d["compositing"] as? [String:Any],
      compositing["direction"] as? String == "front-to-back",compositing["tie_break"] as? String == "source-order",
      let sort = compositing["depth_sort"] as? String,["tile-stable","global-stable"].contains(sort),
      let transmission = compositing["transmittance"] as? [String:Any],transmission["stage"] as? String == "before-contribution" else {throw SceneError.invalid("Malformed EWA render profile.")}
    try GSVDocument.fields(d,["algorithm", "visibility", "projection", "filter", "support", "alpha", "compositing"],context:"render profile d")
    try GSVDocument.fields(visibility,["center_depth", "camera_volume"],context:"render profile visibility")
    try GSVDocument.fields(center,["near", "far", "near_inclusive", "far_inclusive"],context:"render profile center")
    try GSVDocument.fields(projection,["divide_epsilon", "jacobian", "pixel_offset"],context:"render profile projection")
    try GSVDocument.fields(jacobian,["mode", "factor"],context:"render profile jacobian")
    try GSVDocument.fields(filter,["covariance_dilation", "radius_clip", "antialias_compensation"],context:"render profile filter")
    try GSVDocument.fields(support,["bounds", "sigma", "tile_size", "mahalanobis_cutoff"],context:"render profile support")
    try GSVDocument.fields(alpha,["minimum", "maximum"],context:"render profile alpha")
    try GSVDocument.fields(compositing,["direction", "depth_sort", "tie_break", "transmittance"],context:"render profile compositing")
    try GSVDocument.fields(transmission,["threshold", "inclusive", "stage"],context:"render profile transmission")
    func number(_ value:Any?,positive:Bool = false) throws->Float {
      guard let d = GSVDocument.number(value),d >= 0,!positive || d > 0,Float(d).isFinite,!positive || Float(d) > 0 else {throw SceneError.invalid("Render profile numeric value is invalid or outside native range.")}
      return Float(d)
    }
    func flag(_ value:Any?) throws->Bool {
      guard let b = GSVDocument.boolean(value) else {throw SceneError.invalid("Render profile flags must be Boolean.")};return b
    }
    near = try number(center["near"])
    if center["far"] is NSNull {far = nil} else {far = try number(center["far"],positive:true)}
    guard far == nil || far! > near else {throw SceneError.invalid("Render center-depth interval is invalid.")}
    nearInclusive = try flag(center["near_inclusive"]);farInclusive = try flag(center["far_inclusive"])
    self.cameraVolume = cameraVolume == "gaussian-center"
    guard let offset = projection["pixel_offset"] as? [Any],offset.count == 2,
      let x = GSVDocument.number(offset[0]),let y = GSVDocument.number(offset[1]),Float(x).isFinite,Float(y).isFinite else {throw SceneError.invalid("Render pixel offset requires two finite numbers.")}
    pixelOffset = SIMD2(Float(x),Float(y));epsilon = try number(projection["divide_epsilon"])
    jacobianMode = mode;jacobianFactor = try number(jacobian["factor"],positive:true)
    guard mode != "image-bounds" || jacobianFactor >= 1 else {throw SceneError.invalid("Jacobian margin factor must be at least one.")}
    dilation = try number(filter["covariance_dilation"]);radiusClip = try number(filter["radius_clip"])
    antialias = try flag(filter["antialias_compensation"])
    self.bounds = bounds;sigma = try number(support["sigma"],positive:true)
    let tiles = tile.compactMap{GSVDocument.integer($0)}
    guard tiles.count == 2,tiles.allSatisfy({$0 > 0}) else {throw SceneError.invalid("Tile dimensions must be positive integers.")};self.tile = tiles
    mahalanobis = support["mahalanobis_cutoff"] is NSNull ? nil : try number(support["mahalanobis_cutoff"])
    alphaMaximum = try number(alpha["maximum"]);alphaMinimum = try number(alpha["minimum"])
    transmittance = try number(transmission["threshold"]);transmittanceInclusive = try flag(transmission["inclusive"])
    guard alphaMaximum <= 1,alphaMinimum <= alphaMaximum,transmittance <= 1 else {throw SceneError.invalid("Render alpha/transmittance values exceed [0,1].")}
    depthSort = sort;self.object = object
  }

  func centerInterval(camera:GSVCamera,sample:Int,view:Int) throws->(near:Float,far:Float?,nearInclusive:Bool,farInclusive:Bool) {
    var n = near,f = far,ni = nearInclusive,fi = farInclusive
    if cameraVolume,let ns = camera.volumeNear,let fs = camera.volumeFar,let unbounded = camera.volumeUnbounded {
      let index = sample*camera.viewCount+view
      guard Float(ns[index]).isFinite,unbounded[index] || Float(fs[index]).isFinite else {throw SceneError.invalid("Camera volume exceeds native numeric range.")}
      let cn = Float(ns[index]);if cn > n {n = cn;ni = true}
      if !unbounded[index] {let cf = Float(fs[index]);if f == nil || cf < f! {f = cf;fi = true}}
    }
    return (n,f,ni,fi)
  }
  var capabilityDetails:String {
    // Never label source policy execution exact: hardware blending is a distinct
    // back-to-front reference implementation, including support/tie behavior.
    let source = object.label + ((object.metadata["backend_id"] as? String).map{" · " + $0} ?? "")
    return L10n.tr("render.nativeApproximation","Renderer: 4DGS Studio Metal · reference approximation") + "\n"
      + L10n.format("render.profileName","Source profile: %@",source) + "\n"
      + L10n.tr("render.profileApplied","Applied: center-depth interval, camera-center volume, pixel offset, divide epsilon, Jacobian and covariance dilation.") + "\n"
      + L10n.format("render.profileDifferences","Native differences: global back-to-front hardware blend; 3σ elliptical support; alpha cap 0.999, cutoff 1/255; no transmittance early stop, radius clip or antialias compensation. Source bounds %@, σ %@, alpha [%@, %@], T %@, sort %@.",bounds,L10n.number(Double(sigma)),L10n.number(Double(alphaMinimum)),L10n.number(Double(alphaMaximum)),L10n.number(Double(transmittance)),depthSort)
  }
}

struct GSVRenderView {
  let object:GSVObject
  let camera:GSVCamera
  let fieldIDs:[String]
  let profile:GSVRenderProfile
  let appearance:GSVCameraRadiometry?
  let observationIDs:[String]
  let cameraView:Int
  let imageSize:SIMD2<Int>
  let transform:simd_float3x3
  let identityOutput:Bool
  let background:SIMD3<Float>
  let colorSpace:String
  let clamp:String
  let temporalMode:String

  init(document:GSVDocument,object:GSVObject,cameras:[GSVCamera],profiles:[GSVRenderProfile],radiometry:[GSVCameraRadiometry],observations:[GSVCameraObservation]) throws {
    guard object.type == "render",object.encoding == "gsv.render.view",
      let links = object.descriptor["links"] as? [String:Any],let cameraID = links["camera"] as? String,
      let camera = cameras.first(where:{$0.object.id == cameraID}),let fields = links["fields"] as? [String],!fields.isEmpty,Set(fields).count == fields.count,
      let profileID = links["profile"] as? String,let profile = profiles.first(where:{$0.object.id == profileID}),
      let view = GSVDocument.integer(object.data["camera_view"]),(0..<camera.viewCount).contains(view),
      let output = object.data["output"] as? [String:Any],let size = output["image_size"] as? [Any],size.count == 2,
      let width = GSVDocument.integer(size[0]),let height = GSVDocument.integer(size[1]),width > 0,height > 0,
      let matrix = output["camera_to_output"] as? [[Any]],matrix.count == 3,matrix.allSatisfy({$0.count == 3}),
      let rgb = output["background"] as? [Any],rgb.count == 3,
      let color = output["color_space"] as? String,["srgb","linear-srgb"].contains(color),
      let clamp = output["clamp"] as? String,["unit","none"].contains(clamp),
      let temporal = object.data["temporal"] as? [String:Any],let mode = temporal["mode"] as? String,["instantaneous","integrated"].contains(mode) else {throw SceneError.invalid("Malformed render-view binding.")}
    try GSVDocument.fields(object.data,["camera_view","output","temporal"],context:"render view")
    try GSVDocument.fields(links,["camera","fields","profile"],optional:["radiometry","observations"],context:"render-view links")
    try GSVDocument.fields(output,["image_size","camera_to_output","color_space","background","clamp"],context:"render output")
    try GSVDocument.fields(temporal,mode == "instantaneous" ? ["mode"] : ["mode","shutter_open","shutter_close","samples","sampling"],context:"render time")
    if object.space != camera.object.space {throw SceneError.invalid("Render view and camera spaces differ.")}
    if object.clock != camera.object.clock {throw SceneError.invalid("Render view and camera clocks differ.")}
    if profile.cameraVolume && camera.volumeNear == nil {throw SceneError.invalid("Profile requests a camera view volume that is not stored.")}
    for id in fields {
      guard let field = document.objects.first(where:{$0.id == id}),field.type == "gaussian",field.space == camera.object.space,
        field.clock == camera.object.clock || (field.encoding == "gsv.gaussian.static" && field.clock == nil) else {throw SceneError.invalid("Render fields require the camera space and clock (static fields may be clock-free).")}
    }
    for id in fields {
      if let field = document.objects.first(where:{$0.id == id}),let fieldColor = field.data["color_space"] as? String,fieldColor != color {throw SceneError.invalid("Render field and output color domains differ; implicit conversion is forbidden.")}
    }
    let values = matrix.flatMap{$0}.compactMap{GSVDocument.number($0)}
    guard values.count == 9,values.allSatisfy({Float($0).isFinite}),values[0] > 0,values[4] > 0,
      values[1] == 0,values[3] == 0,values[6] == 0,values[7] == 0,values[8] == 1 else {throw SceneError.invalid("camera_to_output requires a positive scale and translation transform.")}
    var transform = matrix_identity_float3x3
    for row in 0..<3 {for col in 0..<3 {transform[col][row] = Float(values[row*3+col])}}
    let colors = rgb.compactMap{GSVDocument.number($0)}
    guard colors.count == 3,colors.allSatisfy({Float($0).isFinite}) else {throw SceneError.invalid("Render background must be finite RGB.")}
    var appearance:GSVCameraRadiometry?
    if let raw = links["radiometry"] {
      guard let id = raw as? String,let p = radiometry.first(where:{$0.object.id == id}),p.camera.object.id == cameraID,p.colorSpace == color else {throw SceneError.invalid("Render-view appearance must match its camera and RGB domain.")}
      appearance = p
    }
    var observationIDs:[String] = []
    if let raw = links["observations"] {
      guard let ids = raw as? [String],Set(ids).count == ids.count else {throw SceneError.invalid("Render-view observation links must be a unique list.")}
      for id in ids {
        guard let observation = observations.first(where:{$0.object.id == id}),observation.camera.object.id == cameraID else {throw SceneError.invalid("Render-view observations require its camera.")}
      };observationIDs = ids
    }
    if mode == "integrated" {
      guard let a = GSVDocument.number(temporal["shutter_open"]),let b = GSVDocument.number(temporal["shutter_close"]),b >= a,
        let samples = GSVDocument.integer(temporal["samples"]),samples > 0,temporal["sampling"] as? String == "uniform-midpoint" else {throw SceneError.invalid("Invalid integrated temporal sampling.")}
    }
    self.object = object;self.camera = camera;fieldIDs = fields;self.profile = profile;self.appearance = appearance
    self.observationIDs = observationIDs;cameraView = view;imageSize = SIMD2(width,height);self.transform = transform
    identityOutput = values == [1,0,0,0,1,0,0,0,1] && width == Int(camera.imageSizes[view*2]) && height == Int(camera.imageSizes[view*2+1])
    background = SIMD3(Float(colors[0]),Float(colors[1]),Float(colors[2]));colorSpace = color;self.clamp = clamp;temporalMode = mode
  }
  func comparisonIssue(_ observation:GSVCameraObservation)->String? {
    if !camera.supportsCalibratedRendering {return L10n.tr("render.distortionUnsupported","This camera's distortion is stored and inspectable, but calibrated rendering is unsupported.")}
    if temporalMode != "instantaneous" {return L10n.tr("render.integrationUnsupported","Temporal shutter integration is stored but not implemented by this renderer.")}
    if !identityOutput || clamp != "unit" || colorSpace != observation.colorSpace {return L10n.tr("render.outputUnmatched","Comparison requires the exact native camera raster, identity output transform, unit clamp and matching RGB domain.")}
    return nil
  }
}
