import AppKit
import MetalKit
import CryptoKit

/// Serial offscreen production GPU picking; no visible window or UI automation.
@main struct CameraPickingAcceptance {
    static func main() throws {
        let device=MTLCreateSystemDefaultDevice()!
        let renderer=try GaussianRenderer(view:MTKView(frame:.zero,device:device),shaderURL:URL(fileURLWithPath:"viewer/native/Shaders.metal"))
        let k=simd_float3x3(columns:(SIMD3(100,0,0),SIMD3(0,100,0),SIMD3(128.5,128.5,1)))
        func gaussian(_ z:Float,_ opacity:Float=0.9,_ scale:Float=0.2) -> GaussianGPURecord {
            GaussianGPURecord(mean:SIMD4(0,0,z,opacity),scale:SIMD4(scale,scale,scale,1),quat:SIMD4(1,0,0,0),color:SIMD4(0,0,1,-1))
        }
        func edge(_ a:SIMD3<Float>,_ b:SIMD3<Float>,_ id:UInt32=7) -> CameraPickSegmentGPU {
            CameraPickSegmentGPU(startPosition:SIMD4(a,1),endPosition:SIMD4(b,1),cameraID:id)
        }
        func colored(_ edge:CameraPickSegmentGPU) -> TrajectorySegmentGPU {
            TrajectorySegmentGPU(startPositionTime:edge.startPosition,endPositionTime:edge.endPosition,startColor:SIMD4(0,1,0,1),endColor:SIMD4(0,1,0,1))
        }
        func render(_ edges:[CameraPickSegmentGPU],gaussians:[GaussianGPURecord]=[],pixel:SIMD2<UInt32>=SIMD2(128,128),tolerance:Float=2,
                    near:Float=0.001,far:Float = .infinity,nearInclusive:Bool=false,farInclusive:Bool=true,
                    cameraK:simd_float3x3?=nil,pixelOffset:SIMD2<Float> = .zero,divideEpsilon:Float=0,paths:[TrajectorySegmentGPU]=[]) throws -> GaussianDebugFrame {
            try renderer.debugRender(a:gaussians,intrinsics:cameraK ?? k,width:256,height:256,integerPixelCenters:false,background:SIMD4(0,0,0,1),pick:pixel,nearClip:near,
                                     cameraSegments:edges.map(colored)+paths,cameraPickSegments:edges,scenePickTolerance:tolerance,
                                     farClip:far,nearInclusive:nearInclusive,farInclusive:farInclusive,pixelOffset:pixelOffset,divideEpsilon:divideEpsilon)
        }
        var checks=[[String:Any]]()
        func label(_ result:ScenePickResult)->String {switch result{case .camera(let id):return "camera:\(id)";case .gaussian(let id):return "gaussian:\(id)";case .empty:return "empty"}}
        func check(_ name:String,_ result:ScenePickResult,_ expected:ScenePickResult,_ details:[String:Any]=[:]) {
            var item=details;item["name"]=name;item["passed"]=result==expected;item["actual"]=label(result);item["expected"]=label(expected);checks.append(item)
        }
        let front=edge(SIMD3(-0.2,0,2),SIMD3(0.2,0,2))
        let back=edge(SIMD3(-0.4,0,4),SIMD3(0.4,0,4))
        let cameraOnly=try render([front])
        check("camera-only GSV wire edge is selectable without Gaussian rows",cameraOnly.scenePick,.camera(7),["gaussian_count":cameraOnly.projected.count])
        let greenPixels=stride(from:1,to:cameraOnly.bgra.count,by:4).filter{cameraOnly.bgra[$0]>128}.count
        checks.append(["name":"picked edge uses the actual displayed projection","passed":greenPixels>=19,"green_pixels":greenPixels])
        check("click tolerance picks nearby wire edge",try render([front],pixel:SIMD2(128,130),tolerance:3).scenePick,.camera(7))
        check("click outside tolerance misses",try render([front],pixel:SIMD2(128,132),tolerance:2).scenePick,.empty)
        check("visible wire endpoint is selectable",try render([front],pixel:SIMD2(138,128),tolerance:2).scenePick,.camera(7))
        check("viewport-clipped edge remains selectable near image corner",try render([edge(SIMD3(-2.67,-3.51,2),SIMD3(-2.47,-1.51,2))],pixel:SIMD2(0,0),tolerance:3).scenePick,.camera(7))
        check("camera behind opaque Gaussian surface is unselectable",try render([back],gaussians:[gaussian(3)]).scenePick,.gaussian(0))
        check("camera in front of Gaussian wins unified depth selection",try render([front],gaussians:[gaussian(3)]).scenePick,.camera(7))
        check("occlusion tests the closest wire sample, not the offset cursor",try render([edge(SIMD3(0,-0.4,4),SIMD3(0,0.4,4))],gaussians:[gaussian(3,0.9,0.001)],pixel:SIMD2(130,128),tolerance:3).scenePick,.empty)
        check("nearest of multiple overlapping camera edges wins",try render([back,edge(SIMD3(-0.2,0,2),SIMD3(0.2,0,2),17)]).scenePick,.camera(17))
        check("equal-depth camera tie resolves stable lowest ID",try render([edge(SIMD3(-0.2,0,2),SIMD3(0.2,0,2),17),edge(SIMD3(-0.2,0,2),SIMD3(0.2,0,2),3)]).scenePick,.camera(3))
        let slanted=edge(SIMD3(-1,0,2),SIMD3(3,0,6),21)
        check("slanted edge depth interpolates perspective-correctly",try render([slanted],gaussians:[gaussian(3.5)]).scenePick,.camera(21),["analytic_center_depth":3,"linear_depth_wrong":4])
        check("slanted edge is hidden by physically nearer Gaussian",try render([slanted],gaussians:[gaussian(2.5)]).scenePick,.gaussian(0))
        check("decorative camera motion path is never selectable",try render([],paths:[colored(front)]).scenePick,.empty)
        check("near-crossing wire edge clips before perspective division",try render([edge(SIMD3(-1,0,-1),SIMD3(1,0,3))],near:0.5).scenePick,.camera(7))
        check("edge behind near plane is invisible and unselectable",try render([edge(SIMD3(-0.1,0,0.2),SIMD3(0.1,0,0.2))],near:0.5).scenePick,.empty)
        check("edge beyond far plane is invisible and unselectable",try render([back],far:3).scenePick,.empty)
        check("far-crossing wire edge clips to visible span",try render([edge(SIMD3(-0.2,0,2),SIMD3(0.2,0,4))],pixel:SIMD2(126,128),far:3).scenePick,.camera(7))
        let boundary=edge(SIMD3(-0.1,0,0.5),SIMD3(0.1,0,0.5))
        check("inclusive near boundary follows display convention",try render([boundary],near:0.5,nearInclusive:true).scenePick,.camera(7))
        check("exclusive near boundary follows display convention",try render([boundary],near:0.5).scenePick,.empty)
        check("inclusive far boundary follows display convention",try render([front],far:2).scenePick,.camera(7))
        check("exclusive far boundary follows display convention",try render([front],far:2,farInclusive:false).scenePick,.empty)
        var skewK=k;skewK[1][0]=30
        check("calibrated skew and post-projection offsets share display projection",try render([edge(SIMD3(-0.2,0.2,2),SIMD3(0.2,0.2,2))],pixel:SIMD2(138,135),cameraK:skewK,pixelOffset:SIMD2(7,-3)).scenePick,.camera(7))
        check("camera divide epsilon follows the shared display projection",try render([edge(SIMD3(-0.2,0.4,2),SIMD3(0.2,0.4,2))],pixel:SIMD2(128,141),divideEpsilon:1).scenePick,.camera(7))
        check("weak Gaussian below display surface opacity does not hide visible camera",try render([back],gaussians:[gaussian(3,0.05)]).scenePick,.camera(7))
        check("weak Gaussian alone leaves no selectable surface",
              try render([], gaussians: [gaussian(3, 0.05)]).scenePick, .empty)
        check("Gaussian surface alpha boundary is inclusive",
              try render([], gaussians: [gaussian(3, 0.1)]).scenePick, .gaussian(0))
        let passed=checks.allSatisfy{$0["passed"] as? Bool == true}
        var hashes=[String:String]()
        for path in ["viewer/native/GaussianRenderer.swift","viewer/native/Shaders.metal","viewer/native/CameraPicking.swift","viewer/native/TrajectoryRendering.swift","viewer/native/tests/CameraPickingAcceptance.swift"] {hashes[path]=SHA256.hash(data:try Data(contentsOf:URL(fileURLWithPath:path))).map{String(format:"%02x",$0)}.joined()}
        let report:[String:Any]=["passed":passed,"device":device.name,"checks":checks,"tested_source_sha256":hashes,
                               "scope":"Serial offscreen production Metal rendering and scene picking; no native window, physical input, revision callback or display-FPS claim.",
                               "selection_depth_policy":"Nearest visible frustum edge; perspective-correct camera depth; Gaussian center-depth alpha>=0.1 display surface, two-stage atomic minimum depth then minimum ID; decorative motion paths excluded."]
        try JSONSerialization.data(withJSONObject:report,options:[.prettyPrinted,.sortedKeys]).write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
        for check in checks where check["passed"] as? Bool != true {print(check)}
        print("Camera picking acceptance: \(passed), \(checks.count) checks")
        if !passed{exit(1)}
    }
}
