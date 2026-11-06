import AppKit
import MetalKit
import CryptoKit

/// Analytic display-metadata cases. These do not evaluate a paper method.
@main struct TrajectoryRendererAcceptance {
    static func main() throws {
        let device = MTLCreateSystemDefaultDevice()!
        let renderer = try GaussianRenderer(view: MTKView(frame: .zero,device:device),shaderURL:URL(fileURLWithPath:"viewer/native/Shaders.metal"))
        let k = simd_float3x3(columns:(SIMD3(100,0,0),SIMD3(0,100,0),SIMD3(128,128,1)))
        let black = GaussianGPURecord(mean:SIMD4(0,0,50,1),scale:SIMD4(0.001,0.001,0.001,1),quat:SIMD4(1,0,0,0),color:SIMD4(0,0,0,-1))
        let blue = GaussianGPURecord(mean:SIMD4(0,0,3,0.9),scale:SIMD4(0.2,0.2,0.2,1),quat:SIMD4(1,0,0,0),color:SIMD4(0,0,1,-1))
        func segment(_ a:SIMD3<Float>,_ b:SIMD3<Float>,_ color:SIMD4<Float>) -> TrajectorySegmentGPU {
            TrajectorySegmentGPU(startPositionTime:SIMD4(a,0),endPositionTime:SIMD4(b,1),startColor:color,endColor:color)
        }
        func geometry(_ segments:[TrajectorySegmentGPU]) -> TrajectoryGeometry {
            let buffer = segments.withUnsafeBytes { device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)! }
            return TrajectoryGeometry(chunks:[TrajectoryChunk(buffer:buffer,segmentCount:segments.count)],timeOrigin:1_000_000_000,timeSpan:1,identityCount:segments.count,segmentCount:segments.count)
        }
        var options = TrajectoryDisplayOptions();options.showTrails=false;options.pathOpacity = 1
        func render(_ records:[GaussianGPURecord],_ geometry:TrajectoryGeometry?,_ options:TrajectoryDisplayOptions,
                    time:Double=1_000_000_001,start:Double=1_000_000_000,selected:[UInt32]=[],second:[GaussianGPURecord]?=nil,alpha:Float=0,cameras:[TrajectorySegmentGPU]=[],exposure:Float=0)throws->GaussianDebugFrame {
            try renderer.debugRender(a:records,b:second,interpolation:alpha,intrinsics:k,width:256,height:256,integerPixelCenters:false,background:SIMD4(0,0,0,1),exposure:exposure,trajectory:geometry,trajectoryTime:time,trailStartTime:start,trajectoryOptions:options,selectedIndices:selected,cameraSegments:cameras)
        }
        func colorPixels(_ frame:GaussianDebugFrame, channel:Int, minValue:Int=10) -> [(Int,Int)] {
            var result=[(Int,Int)]()
            for y in 0..<256 { for x in 0..<256 {
                let i=(y*256+x)*4
                if Int(frame.bgra[i+channel])>minValue {result.append((x,y))}
            } }
            return result
        }
        let front = geometry([segment(SIMD3(-0.2,0,2),SIMD3(0.2,0,2),SIMD4(1,0,0,1))])
        let back = geometry([segment(SIMD3(-0.4,0,4),SIMD3(0.4,0,4),SIMD4(1,0,0,1))])
        let frontImage=try render([blue],front,options),backImage=try render([blue],back,options)
        var checks=[[String:Any]]()
        func check(_ name:String,_ passed:Bool,_ details:[String:Any]=[:]) {var value=details;value["name"]=name;value["passed"]=passed;checks.append(value)}
        let frontCount=colorPixels(frontImage,channel:2).count,backCount=colorPixels(backImage,channel:2).count
        check("front path visible, back path occluded by Gaussian display surface",frontCount>=15 && backCount==0,["front_red_pixels":frontCount,"back_red_pixels":backCount])
        options.pathOpacity = 0.2
        let translucent=try render([black],front,options)
        let maximumRed=stride(from:2,to:translucent.bgra.count,by:4).map{Int(translucent.bgra[$0])}.max()!
        check("full path respects opacity",maximumRed>=45 && maximumRed<=56,["maximum_red":maximumRed])
        let red=segment(SIMD3(-0.6,0,2),SIMD3(0.6,0,2),SIMD4(1,0,0,1))
        let green=segment(SIMD3(-0.75,0,2.5),SIMD3(0.75,0,2.5),SIMD4(0,1,0,1))
        let orderA=try render([black],geometry([red,green]),options),orderB=try render([black],geometry([green,red]),options)
        let difference=zip(orderA.bgra,orderB.bgra).map{abs(Int($0)-Int($1))}.max()!
        check("mixed-color translucent paths independent of extraction order",difference<=1,["maximum_byte_difference":difference])
        let denseHDR=segment(SIMD3(-0.2,0,2),SIMD3(0.2,0,2),SIMD4(1e20,-1e20,1e20,1))
        let denseImage=try render([black],geometry(Array(repeating:denseHDR,count:100_000)),options)
        let denseRed=stride(from:2,to:denseImage.bgra.count,by:4).map{Int(denseImage.bgra[$0])}.max()!
        let denseBlue=stride(from:0,to:denseImage.bgra.count,by:4).map{Int(denseImage.bgra[$0])}.max()!
        let denseGreen=stride(from:1,to:denseImage.bgra.count,by:4).map{Int(denseImage.bgra[$0])}.max()!
        check("100k coincident HDR paths remain finite and preserve clipped display color",denseRed>=250 && denseBlue>=250 && denseGreen==0,["maximum_rgb":[denseRed,denseGreen,denseBlue]])
        options.showPaths=false;options.showTrails=true
        let firstHalf=try render([black],geometry([red]),options,time:1_000_000_000.5,start:1_000_000_000)
        let secondHalf=try render([black],geometry([red]),options,time:1_000_000_001,start:1_000_000_000.5)
        let firstPixels=colorPixels(firstHalf,channel:2),secondPixels=colorPixels(secondHalf,channel:2)
        let firstMax=firstPixels.map{$0.0}.max() ?? -1,secondMin=secondPixels.map{$0.0}.min() ?? -1
        check("opaque trail clips exactly to interpolated playhead and history window",firstPixels.count>=25 && secondPixels.count>=25 && firstMax<=128 && secondMin>=127,["first_max_x":firstMax,"second_min_x":secondMin,"first_pixels":firstPixels.count,"second_pixels":secondPixels.count])
        options.showTrails=false
        let hidden=try render([black],front,options)
        check("path and trail toggles independently hide all geometry",colorPixels(hidden,channel:2).isEmpty)
        let camera=segment(SIMD3(-0.2,0,2),SIMD3(0.2,0,2),SIMD4(0,1,0,0.18))
        let cameraImage=try render([black],nil,options,cameras:[camera])
        let maximumGreen=stride(from:1,to:cameraImage.bgra.count,by:4).map{Int(cameraImage.bgra[$0])}.max()!
        check("camera line color and transparency retained",maximumGreen>=40 && maximumGreen<=50,["maximum_green":maximumGreen])
        let exposedCamera=try render([black],nil,options,cameras:[camera],exposure:-1)
        let exposedCameraGreen=stride(from:1,to:exposedCamera.bgra.count,by:4).map{Int(exposedCamera.bgra[$0])}.max()!
        check("precomputed preview exposure preserves translucent camera radiometry",exposedCameraGreen>=20 && exposedCameraGreen<=25,["maximum_green":exposedCameraGreen])
        var opaqueExposureOptions=options;opaqueExposureOptions.showPaths=true;opaqueExposureOptions.pathOpacity=1
        let exposedPath=try render([black],front,opaqueExposureOptions,exposure:-1)
        let exposedPathRed=stride(from:2,to:exposedPath.bgra.count,by:4).map{Int(exposedPath.bgra[$0])}.max()!
        check("opaque path without blending retains exposure and alpha1 color",exposedPathRed>=126 && exposedPathRed<=129,["maximum_red":exposedPathRed])
        let left=GaussianGPURecord(mean:SIMD4(-0.4,0,3,1),scale:SIMD4(0.01,0.01,0.01,1),quat:SIMD4(1,0,0,0),color:SIMD4(0,0,1,-1))
        var right=left;right.mean.x = 0.4
        var leftNext=left,rightNext=right;leftNext.mean.x += 0.2;rightNext.mean.x += 0.2
        let selected=try render([left,right],nil,options,selected:[0,1],second:[leftNext,rightNext],alpha:0.5)
        let gold=colorPixels(selected,channel:2,minValue:220)
        let goldLeft=gold.filter{$0.0<128},goldRight=gold.filter{$0.0>128}
        check("multiple selected Gaussian markers follow GPU interpolation",goldLeft.count>15 && goldRight.count>15 && selected.projected.allSatisfy{$0.info.z==1},["left_gold_pixels":goldLeft.count,"right_gold_pixels":goldRight.count])
        let passed=checks.allSatisfy{$0["passed"] as? Bool==true}
        var hashes=[String:String]()
        for path in ["viewer/native/GaussianRenderer.swift","viewer/native/Shaders.metal","viewer/native/TrajectoryRendering.swift","viewer/native/tests/TrajectoryRendererAcceptance.swift"] {hashes[path]=SHA256.hash(data:try Data(contentsOf:URL(fileURLWithPath:path))).map{String(format:"%02x",$0)}.joined()}
        let output:[String:Any]=["passed":passed,"device":device.name,"checks":checks,"source_sha256":hashes,"depth_policy":"Gaussian center-depth alpha0.1 display surface; weighted blended OIT paths; exact depth-tested opaque tails. This is a display approximation, not a volumetric/teacher renderer."]
        try JSONSerialization.data(withJSONObject:output,options:[.prettyPrinted,.sortedKeys]).write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
        print("Trajectory renderer acceptance: \(passed)");if !passed{exit(1)}
    }
}
