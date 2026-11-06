import AppKit
import MetalKit
import CryptoKit

/// Host-side Metal resource allocation only. No shader compilation, command
/// queue, native window, GPU submission or actual memory exhaustion is used.
@main struct RendererAllocationAcceptance {
    static func main() throws {
        let device=MTLCreateSystemDefaultDevice()!
        let frame=FrameResources()
        func buffers()->[ObjectIdentifier]{[frame.projected,frame.indicesA,frame.indicesB,frame.histogram,frame.prefixes,frame.totals,frame.visible,frame.indirect,frame.pick].compactMap{$0}.map(ObjectIdentifier.init)}
        func textures()->[ObjectIdentifier]{[frame.overlayDepth,frame.overlayAccumulation,frame.overlayRevealage].compactMap{$0}.map(ObjectIdentifier.init)}
        var checks=[[String:Any]]()
        func check(_ name:String,_ pass:Bool){checks.append(["name":name,"passed":pass])}
        try frame.allocate(device:device,count:16)
        let original=buffers()
        var attempts=0,failed=false
        do {try frame.allocate(device:device,count:32,bufferFactory:{length,options in attempts += 1;return attempts==6 ? nil:device.makeBuffer(length:length,options:options)})}
        catch{failed=true}
        check("injected mid-buffer failure is reported",failed && attempts==6)
        check("buffer failure leaves complete previous capacity and identities intact",frame.capacity==16 && buffers()==original)
        try frame.allocate(device:device,count:32)
        check("retry grows every required buffer and commits capacity",frame.capacity==32 && frame.projected.length>=32*64 && buffers().count==9 && buffers() != original)
        var oversizeAttempts=0;failed=false
        do {try frame.allocate(device:device,count:device.maxBufferLength/64+1,bufferFactory:{length,options in oversizeAttempts += 1;return device.makeBuffer(length:length,options:options)})}
        catch{failed=true}
        check("oversized count rejects before any allocation",failed && oversizeAttempts==0 && frame.capacity==32)
        try frame.allocateOverlays(device:device,width:16,height:16,needsTransparency:true)
        let originalTextures=textures();attempts=0;failed=false
        do {try frame.allocateOverlays(device:device,width:32,height:32,needsTransparency:true,textureFactory:{descriptor in attempts += 1;return attempts==3 ? nil:device.makeTexture(descriptor:descriptor)})}
        catch{failed=true}
        check("injected revealage allocation failure is reported",failed && attempts==3)
        check("resize failure publishes no partial OIT or depth attachment",textures()==originalTextures && frame.overlayDepth?.width==16)
        try frame.allocateOverlays(device:device,width:32,height:32,needsTransparency:true)
        check("retry commits complete matching-size OIT pair and depth",textures().count==3 && frame.overlayDepth?.width==32 && frame.overlayAccumulation?.width==32 && frame.overlayRevealage?.width==32)
        let depth=ObjectIdentifier(frame.overlayDepth!)
        try frame.allocateOverlays(device:device,width:32,height:32,needsTransparency:false)
        check("opaque mode releases idle OIT while reusing depth",frame.overlayAccumulation==nil && frame.overlayRevealage==nil && ObjectIdentifier(frame.overlayDepth!)==depth)
        attempts=0;failed=false
        do {try frame.allocateOverlays(device:device,width:32,height:32,needsTransparency:true,textureFactory:{descriptor in attempts += 1;return attempts==2 ? nil:device.makeTexture(descriptor:descriptor)})}
        catch{failed=true}
        check("same-size OIT enable failure publishes neither attachment",failed && frame.overlayAccumulation==nil && frame.overlayRevealage==nil && ObjectIdentifier(frame.overlayDepth!)==depth)
        try frame.allocateOverlays(device:device,width:32,height:32,needsTransparency:true)
        check("same-size OIT retry recreates both attachments",frame.overlayAccumulation != nil && frame.overlayRevealage != nil)
        let leasedBuffers=buffers(),leasedTextures=textures()
        frame.leased=true;frame.retire()
        check("retirement preserves resources while GPU slot remains leased",frame.retireRequested && frame.capacity==32 && buffers()==leasedBuffers && textures()==leasedTextures)
        frame.finishLease()
        check("completion retires all caches and frees lease",!frame.leased &&  !frame.retireRequested && frame.capacity==0 && frame.bytes==0 && buffers().isEmpty && textures().isEmpty)
        try frame.allocate(device:device,count:4);frame.retire()
        check("idle cache retirement releases immediately",frame.capacity==0 && frame.bytes==0)
        let passed=checks.allSatisfy{$0["passed"] as? Bool==true}
        var hashes=[String:String]()
        for path in ["viewer/native/GaussianRenderer.swift","viewer/native/Shaders.metal","viewer/native/tests/RendererAllocationAcceptance.swift"] {hashes[path]=SHA256.hash(data:try Data(contentsOf:URL(fileURLWithPath:path))).map{String(format:"%02x",$0)}.joined()}
        let report:[String:Any]=["passed":passed,"checks":checks,"device":device.name,"tested_source_sha256":hashes,"scope":"Small host-side Metal resource allocations with deterministic injected nil factory outcomes; no GPU commands, shader compilation, window or real OOM. In-flight retirement exercises the lease state machine directly."]
        try JSONSerialization.data(withJSONObject:report,options:[.prettyPrinted,.sortedKeys]).write(to:URL(fileURLWithPath:CommandLine.arguments[1]),options:.atomic)
        print("Renderer allocation acceptance: \(passed), \(checks.count) checks");if !passed{exit(1)}
    }
}
