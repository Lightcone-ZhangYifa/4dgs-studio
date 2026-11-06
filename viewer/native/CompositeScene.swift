import Foundation
import Metal
import simd

protocol GeometryScene: AnyObject {
    var document:GSVDocument {get}
    var object:GSVObject {get}
    var count:Int {get}
    var frameCount:Int {get}
    var times:[Double] {get}
    var center:SIMD3<Float> {get}
    var radius:Float {get}
    var isScaffold:Bool {get}
    var edges:[(Int,Int)] {get}
    var cachedFrameBytes:Int {get}
    func frame(_ index:Int) throws -> MTLBuffer
    func id(at index:Int)->Int64?
    func sourceObject(at index:Int)->GSVObject?
    func index(of id:Int64,objectID:String?)->Int?
    func checkUnchanged() throws
    func trajectory(at index:Int,maxSamples:Int) throws->[SIMD4<Float>]
}

extension StreamingScene: GeometryScene {
    func sourceObject(at index:Int)->GSVObject? {index>=0 && index<count ? object:nil}
    func index(of id:Int64,objectID:String?)->Int? {objectID==nil || objectID==object.id ? index(of:id):nil}
}

/// Compatible objects are combined BEFORE projection and the global GPU sort.
/// A four-frame cache bounds the concatenation cost and memory footprint.
final class CompositeScene: GeometryScene {
    let children:[StreamingScene]
    let document:GSVDocument
    let object:GSVObject
    let count:Int
    let frameCount:Int
    let times:[Double]
    let center:SIMD3<Float>
    let radius:Float
    let isScaffold:Bool
    let edges:[(Int,Int)]
    private let starts:[Int]
    private let device:MTLDevice
    private var cache=[Int:MTLBuffer](),lru=[Int]()
    private let lock=NSLock()
    var cachedFrameBytes:Int{lock.lock();defer{lock.unlock()};return cache.values.reduce(0){$0+$1.length}+children.reduce(0){$0+$1.cachedFrameBytes}}
    init(children:[StreamingScene],device:MTLDevice) throws {
        guard let first=children.first else{throw SceneError.invalid("No visible geometry objects.")}
        let reference=children.first(where:{$0.frameCount>1}) ?? first
        guard children.allSatisfy({$0.object.space==reference.object.space && ($0.frameCount<=1 || $0.object.clock==reference.object.clock)}) else{throw SceneError.invalid("Visible geometry has different coordinate spaces or clocks. Explicit alignment is required before compositing.")}
        self.children=children;self.device=device;document=first.document;object=reference.object
        var starts=[Int](),total=0,lo=SIMD3<Float>(repeating:.infinity),hi=SIMD3<Float>(repeating:-.infinity),edges=[(Int,Int)]()
        for child in children{starts.append(total);edges += child.edges.map{($0.0+total,$0.1+total)};total += child.count;lo=simd_min(lo,child.boundsMin);hi=simd_max(hi,child.boundsMax)}
        guard total<=10_000_000,total*64<=device.maxBufferLength else{throw SceneError.invalid("Visible object combination exceeds the device buffer limit.")}
        self.starts=starts;count=total;self.edges=edges;center=(lo+hi)/2;radius=simd_length(hi-lo)/2
        let dynamic=children.filter{$0.frameCount>1}
        times=Array(Set((dynamic.isEmpty ? [reference]:dynamic).flatMap{$0.times})).sorted();frameCount=times.count;isScaffold=children.allSatisfy{$0.isScaffold}
    }
    func checkUnchanged() throws{try document.checkUnchanged()}
    private func owner(_ index:Int)->(StreamingScene,Int)?{
        guard index>=0,index<count else{return nil}
        for i in starts.indices.reversed() where index>=starts[i]{return(children[i],index-starts[i])};return nil
    }
    func id(at index:Int)->Int64?{guard let (scene,row)=owner(index) else{return nil};return scene.id(at:row)}
    func sourceObject(at index:Int)->GSVObject?{owner(index)?.0.object}
    func index(of id:Int64,objectID:String?)->Int?{
        var matches=[Int]()
        for (i,child) in children.enumerated() where objectID==nil || child.object.id==objectID{if let row=child.index(of:id){matches.append(starts[i]+row)}}
        return matches.count==1 ? matches[0]:nil
    }
    func trajectory(at index:Int,maxSamples:Int) throws->[SIMD4<Float>]{guard let (scene,row)=owner(index) else{throw SceneError.invalid("Invalid element index.")};return try scene.trajectory(at:row,maxSamples:maxSamples)}
    func frame(_ index:Int) throws->MTLBuffer{
        try checkUnchanged();guard index>=0,index<times.count,count>0 else{throw SceneError.invalid("Invalid composition frame.")}
        lock.lock();defer{lock.unlock()}
        if let hit=cache[index]{lru.removeAll{$0==index};lru.append(index);return hit}
        guard let output=device.makeBuffer(length:count*64,options:.storageModeShared) else{throw SceneError.invalid("Cannot allocate combined scene buffer.")}
        let time=times[index]
        for (childIndex,child) in children.enumerated() where child.count>0 {
            var low=0,high=child.times.count
            while low<high{let m=(low+high)/2;if child.times[m]<=time{low=m+1}else{high=m}}
            let a=max(0,min(low-1,child.times.count-1)),b=min(a+1,child.times.count-1)
            let alpha: Float
            if a == b || time <= child.times[a] {
                alpha = 0
            } else if time >= child.times[b] {
                alpha = 1
            } else {
                let span = child.times[b] - child.times[a]
                // Two valid finite timestamps can have an overflowing span.
                // Scale the three operands together, as camera/viewport tracks
                // do, so a midpoint does not silently become the first record.
                let fraction = span.isFinite
                    ? (time - child.times[a]) / span
                    : (time * 0.5 - child.times[a] * 0.5)
                        / (child.times[b] * 0.5 - child.times[a] * 0.5)
                alpha = Float(min(1, max(0, fraction)))
            }
            let sourceA=try child.frame(a),destination=output.contents().advanced(by:starts[childIndex]*64)
            if alpha==0{memcpy(destination,sourceA.contents(),child.count*64);continue}
            let sourceB=try child.frame(b)
            if alpha==1{memcpy(destination,sourceB.contents(),child.count*64);continue}
            let out=destination.assumingMemoryBound(to:SIMD4<Float>.self)
            let x=sourceA.contents().assumingMemoryBound(to:SIMD4<Float>.self),y=sourceB.contents().assumingMemoryBound(to:SIMD4<Float>.self)
            for i in 0..<child.count{
                let q=i*4;out[q]=simd_mix(x[q],y[q],SIMD4(repeating:alpha));out[q+1]=simd_mix(x[q+1],y[q+1],SIMD4(repeating:alpha));out[q+1].w=x[q+1].w>0.5 && y[q+1].w>0.5 ? 1:0
                let qa=simd_quatf(ix:x[q+2].y,iy:x[q+2].z,iz:x[q+2].w,r:x[q+2].x),qb=simd_quatf(ix:y[q+2].y,iy:y[q+2].z,iz:y[q+2].w,r:y[q+2].x),rotation=simd_slerp(qa,qb,alpha)
                out[q+2]=SIMD4(rotation.real,rotation.imag.x,rotation.imag.y,rotation.imag.z);out[q+3]=simd_mix(x[q+3],y[q+3],SIMD4(repeating:alpha));if x[q+3].w<0 || y[q+3].w<0{out[q+3].w = -1}
            }
        }
        output.label="Combined scene · global sort";cache[index]=output;lru.append(index);while lru.count>4{cache.removeValue(forKey:lru.removeFirst())};return output
    }
}
