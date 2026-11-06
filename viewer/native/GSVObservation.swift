import Foundation
import CoreGraphics

/// Calibrated observations reference an exact camera sample and view, never a
/// nearest frame inferred from matching array lengths. Pixels remain lazy.
struct GSVCameraObservation {
  let document: GSVDocument
  let object: GSVObject
  let camera: GSVCamera
  let samples: [Int]
  let times: [Double]
  let views: [Int]
  let images: [String]
  let masks: [String]?
  let colorSpace: String
  let role: String

  init(document: GSVDocument, object: GSVObject, cameras: [GSVCamera]) throws {
    guard object.type == "image", object.encoding == "gsv.observation.camera-rgb",
      let cameraID = (object.descriptor["links"] as? [String:Any])?["camera"] as? String,
      let camera = cameras.first(where: {$0.object.id == cameraID}),
      object.space == camera.object.space, object.clock == camera.object.clock,
      let sampleData = object.data["samples"] as? [String:Any],
      let pixelData = object.data["pixels"] as? [String:Any],
      let samplesID = sampleData["camera_samples"] as? String,
      let timesID = sampleData["times"] as? String,
      let viewsID = sampleData["view_indices"] as? String,
      let images = pixelData["images"] as? [String],
      let color = pixelData["color_space"] as? String, ["srgb","linear-srgb"].contains(color),
      let role = object.data["role"] as? String, ["ground_truth","reference","prediction"].contains(role)
    else { throw SceneError.invalid("Camera RGB observations require a linked pinhole camera, the same space/clock, and explicit color space and role.") }
    try GSVDocument.fields(object.data,["samples","pixels","role"],context:"camera observations")
    try GSVDocument.fields(sampleData,["camera_samples","view_indices","times"],context:"observation samples")
    try GSVDocument.fields(pixelData,["images","color_space"],optional:["masks"],context:"observation pixels")
    let sa = try document.array(samplesID), va = try document.array(viewsID), ta = try document.array(timesID)
    guard sa.dtype == "i64", sa.shape == [images.count], va.dtype == "i32", va.shape == [images.count],ta.dtype == "f64",ta.shape == [images.count] else {
      throw SceneError.invalid("Camera observation indices require i64[M] camera samples and i32[M] view indices.")
    }
    let sb = try document.readArray(samplesID), vb = try document.readArray(viewsID)
    let samples = sb.withUnsafeBytes { raw in (0..<images.count).map { Int(raw.loadUnaligned(fromByteOffset:$0*8,as:Int64.self)) } }
    let views = vb.withUnsafeBytes { raw in (0..<images.count).map { Int(raw.loadUnaligned(fromByteOffset:$0*4,as:Int32.self)) } }
    let times = try document.doubles(timesID)
    guard times.allSatisfy({$0.isFinite}) else {throw SceneError.invalid("Observation times must be finite.")}
    var masks: [String]?
    if let value = pixelData["masks"] {
      guard let parsed = value as? [String], parsed.count == images.count else { throw SceneError.invalid("Observation masks must match the image count.") }
      masks = parsed
    }
    var unique = Set<String>()
    for row in images.indices {
      let sample = samples[row], view = views[row]
      guard camera.times.indices.contains(sample), (0..<camera.viewCount).contains(view), unique.insert("\(times[row] == 0 ? 0 : times[row]):\(view)").inserted,
        camera.times.count == 1 || times[row] == camera.times[sample] else {
        throw SceneError.invalid("Observation camera sample/view indices are invalid or duplicated.")
      }
      let width = Int(camera.imageSizes[view*2]), height = Int(camera.imageSizes[view*2+1])
      let array = try document.array(images[row])
      guard ["u8","f32"].contains(array.dtype), array.shape == [height,width,3] else { throw SceneError.invalid("Observation image dimensions must exactly match its camera view.") }
      if let masks {
        let mask = try document.array(masks[row])
        guard mask.dtype == "bool", mask.shape == [height,width] else { throw SceneError.invalid("Observation masks require bool[H,W].") }
      }
    }
    self.document = document; self.object = object; self.camera = camera
    self.samples = samples; self.times = times; self.views = views; self.images = images; self.masks = masks
    self.colorSpace = color; self.role = role
  }
  func exactRow(time: Double, view: Int) -> Int? {
    samples.indices.first { times[$0] == time && views[$0] == view }
  }
  func read(_ row: Int) throws -> RGBObservationPixels {
    guard images.indices.contains(row) else { throw SceneError.invalid("Observation row is outside the image table.") }
    let array = try document.array(images[row]), width = array.shape[1], height = array.shape[0]
    // Bounded native evaluation: 16 megapixels, with one capture in flight.
    guard width <= 16384, height <= 16384, width*height <= 16_777_216 else { throw SceneError.invalid("This observation exceeds Studio's 16 megapixel comparison limit. Stored data is unchanged.") }
    let bytes = try document.readArray(images[row], limit: 256*1024*1024)
    let rgb: [Float]
    if array.dtype == "u8" { rgb = bytes.map { Float($0)/255 } }
    else { rgb = bytes.withUnsafeBytes { raw in (0..<bytes.count/4).map { raw.loadUnaligned(fromByteOffset:$0*4,as:Float.self) } } }
    guard rgb.allSatisfy({$0.isFinite && $0 >= 0 && $0 <= 1}) else { throw SceneError.invalid("Observation RGB values must be finite within [0,1].") }
    var mask: [Bool]?
    if let masks {
      let bytes = try document.readArray(masks[row], limit: 32*1024*1024)
      guard bytes.allSatisfy({$0 == 0 || $0 == 1}) else { throw SceneError.invalid("Observation masks must contain canonical Boolean values.") }
      mask = bytes.map {$0 != 0}
    }
    return RGBObservationPixels(width:width,height:height,rgb:rgb,mask:mask,colorSpace:colorSpace)
  }
}

struct RGBObservationPixels {
  let width: Int
  let height: Int
  let rgb: [Float]
  let mask: [Bool]?
  let colorSpace: String
  func image() throws -> CGImage {
    let bytes = Data(rgb.map { UInt8((min(1,max(0,$0))*255).rounded()) })
    let colorName = colorSpace == "linear-srgb" ? CGColorSpace.linearSRGB : CGColorSpace.sRGB
    guard let space = CGColorSpace(name:colorName), let provider = CGDataProvider(data:bytes as CFData),
      let image = CGImage(width:width,height:height,bitsPerComponent:8,bitsPerPixel:24,bytesPerRow:width*3,space:space,bitmapInfo:[],provider:provider,decode:nil,shouldInterpolate:false,intent:.relativeColorimetric) else {
      throw SceneError.invalid("Cannot construct a color-managed observation preview.")
    }
    return image
  }
}

struct RGBQualityMetrics {
  let mse: Double?
  let mae: Double?
  let psnr: Double?
  let ssim: Double?
  let pixelCount: Int
  let ssimWindowCount: Int
}

enum RGBImageQuality {
  /// RGB [0,1], no gamma conversion, masked MSE/MAE and valid-window SSIM.
  /// SSIM uses an 11x11 Gaussian sigma 1.5 and population covariance; mask
  /// windows are eligible only when all 121 pixels are valid.
  static func compare(_ prediction: [Float], _ reference: [Float], width: Int, height: Int,
                      mask: [Bool]? = nil, cancelled: () -> Bool = {false}) throws -> RGBQualityMetrics {
    let n = width*height
    guard width > 0, height > 0, prediction.count == n*3, reference.count == n*3,
      mask == nil || mask!.count == n else { throw SceneError.invalid("Metric image or mask dimensions differ.") }
    var mse = 0.0, mae = 0.0, count = 0
    for i in 0..<n {
      if i % max(1,width*16) == 0, cancelled() { throw CancellationError() }
      for c in 0..<3 {
        let a = Double(prediction[i*3+c]), b = Double(reference[i*3+c])
        guard a.isFinite, b.isFinite, (0...1).contains(a), (0...1).contains(b) else { throw SceneError.invalid("Metrics require finite RGB values in [0,1].") }
        if mask?[i] ?? true { let d = a-b; mse += d*d; mae += abs(d) }
      }
      if mask?[i] ?? true { count += 1 }
    }
    guard count > 0 else { return RGBQualityMetrics(mse:nil,mae:nil,psnr:nil,ssim:nil,pixelCount:0,ssimWindowCount:0) }
    mse /= Double(count*3); mae /= Double(count*3)
    let psnr = mse == 0 ? Double.infinity : 10*log10(1/mse)
    guard width >= 11, height >= 11 else { return RGBQualityMetrics(mse:mse,mae:mae,psnr:psnr,ssim:nil,pixelCount:count,ssimWindowCount:0) }
    let outW = width-10, outH = height-10
    var windows = [Bool](repeating:true,count:outW*outH)
    if let mask {
      // Integral valid-pixel counts avoid O(121) mask work per window.
      var integral = [Int32](repeating:0,count:(width+1)*(height+1))
      for y in 0..<height {
        var row: Int32 = 0
        for x in 0..<width { row += mask[y*width+x] ? 1:0; integral[(y+1)*(width+1)+x+1] = integral[y*(width+1)+x+1]+row }
      }
      for y in 0..<outH { for x in 0..<outW {
        let stride = width+1
        let total = integral[(y+11)*stride+x+11]-integral[y*stride+x+11]-integral[(y+11)*stride+x]+integral[y*stride+x]
        windows[y*outW+x] = total == 121
      } }
    }
    let windowCount = windows.filter{$0}.count
    guard windowCount > 0 else { return RGBQualityMetrics(mse:mse,mae:mae,psnr:psnr,ssim:nil,pixelCount:count,ssimWindowCount:0) }
    var kernel = (-5...5).map { exp(-Double($0*$0)/(2*1.5*1.5)) }
    let sum = kernel.reduce(0,+); kernel = kernel.map {$0/sum}
    var ssimSum = 0.0
    // Five moments are filtered together, one channel at a time; bounded to
    // 5 * (W-10) * H doubles rather than multiple full RGB float64 images.
    for channel in 0..<3 {
      var horizontal = [Double](repeating:0,count:outW*height*5)
      for y in 0..<height {
        if y % 8 == 0, cancelled() { throw CancellationError() }
        for x in 0..<outW {
          let dst = (y*outW+x)*5
          for k in 0..<11 {
            let src = (y*width+x+k)*3+channel, a = Double(prediction[src]), b = Double(reference[src]), w = kernel[k]
            horizontal[dst] += a*w; horizontal[dst+1] += b*w
            horizontal[dst+2] += a*a*w; horizontal[dst+3] += b*b*w; horizontal[dst+4] += a*b*w
          }
        }
      }
      for y in 0..<outH {
        if y % 8 == 0, cancelled() { throw CancellationError() }
        for x in 0..<outW where windows[y*outW+x] {
          var u = 0.0, v = 0.0, aa = 0.0, bb = 0.0, ab = 0.0
          for k in 0..<11 {
            let src = ((y+k)*outW+x)*5, w = kernel[k]
            u += horizontal[src]*w; v += horizontal[src+1]*w; aa += horizontal[src+2]*w; bb += horizontal[src+3]*w; ab += horizontal[src+4]*w
          }
          let va = max(0,aa-u*u), vb = max(0,bb-v*v), cov = ab-u*v
          ssimSum += ((2*u*v+0.0001)*(2*cov+0.0009))/((u*u+v*v+0.0001)*(va+vb+0.0009))
        }
      }
    }
    return RGBQualityMetrics(mse:mse,mae:mae,psnr:psnr,ssim:ssimSum/Double(windowCount*3),pixelCount:count,ssimWindowCount:windowCount)
  }
}


/// Camera-indexed appearance is a peer object: it transforms a completed
/// rendering in its encoded RGB domain, never a camera pose or a GT image.
struct GSVCameraRadiometry {
  let object: GSVObject
  let camera: GSVCamera
  let colorSpace: String
  let gain: [Float]
  let bias: [Float]
  init(document:GSVDocument,object:GSVObject,cameras:[GSVCamera]) throws {
    guard object.type == "appearance", object.encoding == "gsv.appearance.camera-affine",
      let cameraID = (object.descriptor["links"] as? [String:Any])?["camera"] as? String,
      let camera = cameras.first(where:{$0.object.id == cameraID}),
      object.space == camera.object.space, object.clock == camera.object.clock,
      let gainID = object.data["gain"] as? String, let biasID = object.data["bias"] as? String,
      let color = object.data["color_space"] as? String,["srgb","linear-srgb"].contains(color) else {
      throw SceneError.invalid("Camera affine appearance requires an exact linked pinhole camera and its space/clock/color domain.")
    }
    let ga = try document.array(gainID), ba = try document.array(biasID)
    let shape = [camera.times.count,camera.viewCount,3]
    guard ga.dtype == "f32",ba.dtype == "f32",ga.shape == shape,ba.shape == shape else {
      throw SceneError.invalid("Camera affine gain and bias require f32[T,V,3] aligned with the camera table.")
    }
    func values(_ id:String) throws->[Float] {
      let data = try document.readArray(id)
      return data.withUnsafeBytes{raw in (0..<data.count/4).map{raw.loadUnaligned(fromByteOffset:$0*4,as:Float.self)}}
    }
    let gain = try values(gainID),bias = try values(biasID)
    guard gain.allSatisfy({$0.isFinite && $0 >= 0}),bias.allSatisfy({$0.isFinite}) else {
      throw SceneError.invalid("Camera affine parameters must be finite with nonnegative gains.")
    }
    self.object = object; self.camera = camera; self.colorSpace = color; self.gain = gain; self.bias = bias
  }
  func at(sample:Int,view:Int)->(gain:SIMD3<Float>,bias:SIMD3<Float>) {
    let offset = (sample*camera.viewCount+view)*3
    return (SIMD3(gain[offset],gain[offset+1],gain[offset+2]),SIMD3(bias[offset],bias[offset+1],bias[offset+2]))
  }
}
