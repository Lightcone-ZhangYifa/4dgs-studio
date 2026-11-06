import AppKit
import MetalKit
import simd

@main struct CameraRenderContractProbe {
  static func require(_ condition:Bool,_ message:String) throws {if !condition {throw SceneError.invalid(message)}}
  static func wait(_ predicate:()->Bool) throws {
    let end = Date().addingTimeInterval(30)
    while Date()<end {if predicate(){return};RunLoop.current.run(until:Date().addingTimeInterval(0.01))}
    throw SceneError.invalid("Controller state timed out")
  }
  static func main() throws {
    _ = NSApplication.shared
    let folder = URL(fileURLWithPath:CommandLine.arguments[1]),output = URL(fileURLWithPath:CommandLine.arguments[2])
    let fixture = try JSONSerialization.jsonObject(with:Data(contentsOf:folder.appendingPathComponent("expectations.json"))) as! [String:Any]
    let device = MTLCreateSystemDefaultDevice()!
    var records = [[String:Any]]()
    for item in fixture["cases"] as! [[String:Any]] {
      let name = item["name"] as! String
      var accepted = false,supported = false,errorText = ""
      do {
        let doc = try GSVDocument(url:folder.appendingPathComponent(item["file"] as! String))
        let cameras = try doc.objects.filter{$0.encoding == "gsv.camera.pinhole"}.map{try GSVCamera(document:doc,object:$0)}
        let observations = try doc.objects.filter{$0.encoding == "gsv.observation.camera-rgb"}.map{try GSVCameraObservation(document:doc,object:$0,cameras:cameras)}
        let appearances = try doc.objects.filter{$0.encoding == "gsv.appearance.camera-affine"}.map{try GSVCameraRadiometry(document:doc,object:$0,cameras:cameras)}
        let profiles = try doc.objects.filter{$0.encoding == "gsv.render.profile"}.map{try GSVRenderProfile(object:$0)}
        let views = try doc.objects.filter{$0.encoding == "gsv.render.view"}.map{try GSVRenderView(document:doc,object:$0,cameras:cameras,profiles:profiles,radiometry:appearances,observations:observations)}
        accepted = true
        if let view = views.first,let obs = observations.first(where:{view.observationIDs.contains($0.object.id)}) {
          let fields = try view.fieldIDs.map {id in try StreamingScene(document:doc,object:doc.objects.first{$0.id == id}!,device:device)}
          supported = view.comparisonIssue(obs) == nil && fields.allSatisfy{$0.object.encoding == "gsv.gaussian.static" || $0.times.contains(obs.times[0])}
          for row in obs.times.indices {try require(obs.exactRow(time:obs.times[row],view:obs.views[row]) == row,"Observation independent time lookup failed")}
          try require(profiles[0].capabilityDetails.contains("reference approximation"),"Capability description missing")
        }
      } catch {errorText = String(describing:error)}
      try require(accepted == item["expected_native_accept"] as! Bool,"\(name): accept mismatch \(errorText)")
      try require(supported == item["expected_comparison_supported"] as! Bool,"\(name): calibrated capability mismatch \(errorText)")
      records.append(["name":name,"accepted":accepted,"supported":supported,"error":errorText])
    }
    print("Parser fixtures passed");fflush(stdout)
    let metal = MTKView(frame:NSRect(x:0,y:0,width:64,height:48),device:device)
    let renderer = try GaussianRenderer(view:metal,shaderURL:URL(fileURLWithPath:"viewer/native/Shaders.metal"))
    var k = matrix_identity_float3x3;k[0][0] = 45;k[1][1] = 50;k[2][0] = 32;k[2][1] = 24
    let record = GaussianGPURecord(mean:SIMD4(0,0,2,0.8),scale:SIMD4(0.1,0.1,0.1,1),quat:SIMD4(1,0,0,0),color:SIMD4(1,0.5,0.2,-1))
    let buffer = [record].withUnsafeBytes{device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)!}
    func capture(near:Float = 0,far:Float? = nil,ni:Bool = true,fi:Bool = true,offset:SIMD2<Float> = .zero)->[Float] {
      let sem = DispatchSemaphore(value:0);var result:Result<[Float],Error>?
      renderer.captureComparison(a:buffer,b:buffer,count:1,interpolation:0,pose:matrix_identity_float4x4,intrinsics:k,width:64,height:48,integerPixelCenters:false,background:.zero,nearClip:near,farClip:far,nearInclusive:ni,farInclusive:fi,pixelOffset:offset,jacobianMode:"image-bounds",cancellation:ComparisonCancellation()){result = $0;sem.signal()}
      precondition(sem.wait(timeout:.now()+30) == .success);return try! result!.get()
    }
    let inclusive = capture(far:2),exclusive = capture(far:2,fi:false),empty = capture(near:3,far:1),nearInclusive = capture(near:2),nearExclusive = capture(near:2,ni:false),shifted = capture(offset:SIMD2(3,-2))
    try require(inclusive.max()! > 0.1 && nearInclusive == inclusive,"Inclusive center boundary was removed")
    try require(exclusive.allSatisfy{$0 == 0} && nearExclusive.allSatisfy{$0 == 0} && empty.allSatisfy{$0 == 0},"Exclusive or empty clipping interval retained a Gaussian")
    func centroid(_ rgb:[Float])->SIMD2<Double> {
      var sum = 0.0,v = SIMD2<Double>.zero
      for i in 0..<64*48 {let a = Double(rgb[i*3]);sum += a;v += a*SIMD2(Double(i%64),Double(i/64))};return v/sum
    }
    let delta = centroid(shifted)-centroid(inclusive)
    try require(simd_length(delta-SIMD2(3,-2)) < 0.002,"Profile pixel offset applied incorrectly")
    // Exercise actual controller for static calibration + independent image times.
    print("Metal policy tests passed");fflush(stdout)
    let workspace = WorkspaceController(),controller = NativeDocumentController(workspace:workspace)
    controller.open(folder.appendingPathComponent("static_camera_observation_times.gsv"))
    try wait{workspace.scene != nil && workspace.compareButton.isEnabled}
    print("First document loaded: " + workspace.statusLabel.stringValue);fflush(stdout)
    workspace.actions.onToggleComparison?(true);try wait{workspace.comparisonMetrics != nil || workspace.comparisonMessage.hasPrefix("Comparison unavailable")}
    try require(workspace.comparisonMetrics != nil,workspace.comparisonMessage)
    let later = workspace.comparisonEntries.last!.time
    workspace.actions.onScrubTime?(later);try wait{workspace.comparisonMetrics != nil && workspace.comparisonSelection.map{workspace.comparisonEntries[$0].time == later} == true}
    try require(later > 0,"Static camera fixture failed to supply independent time")
    workspace.actions.onToggleComparison?(false)
    // Explicitly preview a cropped render view; no GT or metrics are fabricated.
    let croppedWorkspace = WorkspaceController(),croppedController = NativeDocumentController(workspace:croppedWorkspace)
    croppedController.open(folder.appendingPathComponent("cropped_unsupported.gsv"));try wait{croppedWorkspace.scene != nil}
    croppedWorkspace.actions.onActivateRenderView?("view")
    // Cropped view has observations but comparison must refuse rather than resize GT.
    try wait{croppedWorkspace.comparisonMessage.contains("exact native camera raster")}
    try require(croppedWorkspace.comparisonMetrics == nil,"Cropped binding manufactured a metric")
    let domains = WorkspaceController(),domainController = NativeDocumentController(workspace:domains)
    domainController.open(folder.appendingPathComponent("domains.gsv"));try wait{domains.scene != nil}
    try require(domains.scene!.activeClock == "a","Initial geometry domain mismatch")
    guard let other = domains.comparisonEntries.firstIndex(where:{$0.renderViewID == "view-b"}) else {throw SceneError.invalid("Missing second-domain binding")}
    domains.actions.onSelectComparison?(other);try wait{domains.comparisonMetrics != nil}
    try require(domains.scene!.activeClock == "b" && domains.currentTime == 10,"Bound comparison used the free viewport clock")
    domains.actions.onToggleComparison?(false)
    try require(domains.scene!.activeClock == "a","Return-to3D failed to restore scene domain")
    domains.actions.onActivateRenderView?("preview-b");try wait{domains.comparisonView.rendered.image != nil}
    try require(domains.scene!.activeClock == "b" && domains.currentTime == 10 && domains.comparisonView.previewOnly && domains.comparisonMetrics == nil,"GT-free preview used an unrelated domain or retained metrics")
    _ = try domains.comparisonView.exportImage()
    let report:[String:Any] = ["passed":true,"parser_cases":records,"metal":["far_inclusive":true,"far_exclusive":true,"near_inclusive":true,"near_exclusive":true,"empty_volume_intersection":true,"pixel_offset_delta":[delta.x,delta.y]],"controller":["static_camera_independent_observation_time":true,"cropped_GT_refused":true,"binding_domain_and_return":true,"GT_free_preview_and_export":true]]
    try JSONSerialization.data(withJSONObject:report,options:[.prettyPrinted,.sortedKeys]).write(to:output,options:.atomic)
    print("\(records.count) camera and render cases passed")
  }
}
