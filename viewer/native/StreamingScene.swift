import Foundation
import Metal
import simd
import Darwin


enum SceneError: LocalizedError {
    case invalid(String)
    var errorDescription: String? { if case let .invalid(message) = self { return message }; return nil }
}

struct GaussianValue {
    let position: SIMD3<Float>
    let opacity: Float
    let scale: SIMD3<Float>
    let quaternion: SIMD4<Float> // wxyz
    let color: SIMD3<Float>
    let confidence: Float
    let valid: Bool
}

/// Evaluated identity-persistent Gaussian object, independent of its containing document.
/// A bounded frame working set is mapped into Metal; no T×N object graph exists.
final class StreamingScene {
    let document: GSVDocument
    let object: GSVObject
    let device: MTLDevice
    let count: Int
    let frameCount: Int
    let times: [Double]
    let frameArrays: [GSVArray]
    private let scaffoldPositions: GSVArray?
    private let scaffoldVisibility: GSVArray?
    let edges: [(Int,Int)]
    var isScaffold: Bool { scaffoldPositions != nil }
    let boundsMin: SIMD3<Float>
    let boundsMax: SIMD3<Float>
    private let ids: [Int64]
    private var staticFrame: MTLBuffer?
    private var frameCache = [Int: MTLBuffer]()
    private var lru = [Int]()
    private let lock = NSLock()
    private var checked = Set<Int>()
    var url: URL { document.url }
    var fileBytes: Int64 { document.fileBytes }
    var metadata: [String: Any] { object.metadata }
    var center: SIMD3<Float> { (boundsMin+boundsMax)/2 }
    var radius: Float { simd_length(boundsMax-boundsMin)/2 }
    var cachedFrameBytes: Int { lock.lock();defer { lock.unlock() };return frameCache.values.reduce(staticFrame?.length ?? 0) { $0+$1.length } }
    static func digest(_ data: Data) -> String { GSVDocument.digest(data) }
    static func supports(_ object: GSVObject) -> Bool { (object.type == "gaussian" && ["gsv.gaussian.persistent-identity","gsv.gaussian.static"].contains(object.encoding)) || (object.type == "scaffold" && object.encoding == "gsv.scaffold.graph") }

    convenience init(url: URL, device: MTLDevice) throws {
        let document = try GSVDocument(url:url)
        guard let object = document.objects.first(where:Self.supports) else { throw SceneError.invalid("This document has no identity-persistent Gaussian object. Its other objects can be inspected in Studio.") }
        try self.init(document:document,object:object,device:device)
    }
    init(document: GSVDocument, object: GSVObject, device: MTLDevice) throws {
        if object.encoding == "gsv.gaussian.static" {
            guard object.space != nil,let idRef = object.data["ids"] as? String else {throw SceneError.invalid("Static Gaussian fields require explicit space and identities.")}
            let idArray = try document.array(idRef)
            guard idArray.dtype == "i64",idArray.shape.count == 1,idArray.shape[0] <= 10_000_000,idArray.shape[0] <= device.maxBufferLength/64 else {throw SceneError.invalid("Invalid static Gaussian identity array.")}
            let n = idArray.shape[0],idData = try document.readArray(idRef)
            let identities = idData.withUnsafeBytes {raw in (0..<n).map{raw.loadUnaligned(fromByteOffset:$0*8,as:Int64.self)}}
            guard zip(identities.dropFirst(),identities).allSatisfy({$0 > $1}) else {throw SceneError.invalid("Static identities must be unique and sorted.")}
            func values(_ key:String,_ tail:[Int]) throws->[Float] {
                guard let ref = object.data[key] as? String else {throw SceneError.invalid("Static Gaussian attribute is missing: " + key)}
                let a = try document.array(ref)
                guard ["f32","f64"].contains(a.dtype),a.shape == [n]+tail else {throw SceneError.invalid("Static Gaussian attribute shape is invalid: " + key)}
                let data = try document.readArray(ref)
                let values:[Float] = a.dtype == "f32" ? data.withUnsafeBytes{raw in (0..<data.count/4).map{raw.loadUnaligned(fromByteOffset:$0*4,as:Float.self)}} : try document.doubles(ref).map{Float($0)}
                guard values.allSatisfy({$0.isFinite}) else {throw SceneError.invalid("Static Gaussian values exceed native Float32 range.")}
                return values
            }
            let means = try values("means",[3]),scales = try values("scales",[3]),quaternions = try values("quaternions",[4]),colors = try values("colors",[3]),opacity = try values("opacity",[])
            var packed = [Float](repeating:0,count:n*16),low = SIMD3<Float>(repeating:.infinity),high = SIMD3<Float>(repeating:-.infinity)
            for i in 0..<n {
                guard scales[(i*3)..<(i*3+3)].allSatisfy({$0 > 0}),(0...1).contains(opacity[i]),abs((0..<4).reduce(Float(0)){$0+quaternions[i*4+$1]*quaternions[i*4+$1]}-1)<0.003 else {throw SceneError.invalid("Invalid static Gaussian scale, opacity or rotation.")}
                let extent = 4*max(scales[i*3],max(scales[i*3+1],scales[i*3+2]))
                for c in 0..<3 {packed[i*16+c]=means[i*3+c];packed[i*16+4+c]=scales[i*3+c];packed[i*16+12+c]=colors[i*3+c];low[c]=min(low[c],means[i*3+c]-extent);high[c]=max(high[c],means[i*3+c]+extent)}
                packed[i*16+3]=opacity[i];packed[i*16+7]=1;packed[i*16+15] = -1
                for c in 0..<4 {packed[i*16+8+c]=quaternions[i*4+c]}
            }
            if n == 0 {low = SIMD3(repeating:-1);high = SIMD3(repeating:1)}
            guard (0..<3).allSatisfy({low[$0].isFinite && high[$0].isFinite}) else {throw SceneError.invalid("Static Gaussian bounds exceed native numeric range.")}
            self.document=document;self.object=object;self.device=device;count=n;frameCount=1;times=[0];frameArrays=[];ids=identities;boundsMin=low;boundsMax=high;scaffoldPositions=nil;scaffoldVisibility=nil;edges=[]
            if n > 0 {staticFrame = packed.withUnsafeBytes{device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)};guard staticFrame != nil else {throw SceneError.invalid("Cannot allocate static Gaussian records.")}}
            return
        }
        guard object.space != nil,object.clock != nil else{throw SceneError.invalid("Spatial and temporal objects require explicit space and clock references.")}
        guard Self.supports(object), let idsID = object.data["ids"] as? String, let timesID = object.data["times"] as? String else { throw SceneError.invalid("Invalid geometry object: \(object.label)") }
        var frameIDs = object.data["frames"] as? [String] ?? []
        var bounds = object.data["bounds"] as? [[NSNumber]] ?? []
        var positions: GSVArray?, visibility: GSVArray?, links = [(Int,Int)]()
        if object.encoding == "gsv.scaffold.graph" {
            guard let positionsID=object.data["positions"] as? String else {throw SceneError.invalid("Scaffold positions are missing.")}
            positions=try document.array(positionsID)
            let p=positions!
            guard p.dtype=="f32",p.shape.count==3,p.shape[2]==3 else {throw SceneError.invalid("Scaffold positions require f32[T,N,3].")}
            frameIDs=Array(repeating:positionsID,count:p.shape[0])
            let bytes=try document.readArray(positionsID)
            var low=SIMD3<Float>(repeating:.infinity),high=SIMD3<Float>(repeating:-.infinity)
            try bytes.withUnsafeBytes{raw in
                for i in 0..<bytes.count/12 {
                    let v=SIMD3(raw.loadUnaligned(fromByteOffset:i*12,as:Float.self),raw.loadUnaligned(fromByteOffset:i*12+4,as:Float.self),raw.loadUnaligned(fromByteOffset:i*12+8,as:Float.self))
                    guard (0..<3).allSatisfy({v[$0].isFinite}) else{throw SceneError.invalid("Nonfinite scaffold node.")};low=simd_min(low,v);high=simd_max(high,v)
                }
            }
            if bytes.isEmpty{low=SIMD3(repeating:-1);high=SIMD3(repeating:1)}
            bounds=[(0..<3).map{NSNumber(value:low[$0])},(0..<3).map{NSNumber(value:high[$0])}]
            for name in ["certainty","visibility"] {
                if let ref=object.data[name] as? String {
                    let a=try document.array(ref)
                    guard a.dtype=="bool",a.shape==Array(p.shape.prefix(2)),try document.readArray(ref).allSatisfy({$0==0 || $0==1}) else{throw SceneError.invalid("Scaffold \(name) requires canonical bool[T,N].")}
                    if name=="visibility"{visibility=a}
                }
            }
            if let ref=object.data["quaternions"] as? String {
                let a=try document.array(ref);guard a.dtype=="f32",a.shape==[p.shape[0],p.shape[1],4] else{throw SceneError.invalid("Scaffold rotations require f32[T,N,4] wxyz.")}
                let data=try document.readArray(ref)
                try data.withUnsafeBytes{raw in for i in 0..<data.count/16 {
                    var norm:Float=0
                    for c in 0..<4 {let value=raw.loadUnaligned(fromByteOffset:i*16+c*4,as:Float.self);guard value.isFinite else{throw SceneError.invalid("Nonfinite scaffold rotation.")};norm+=value*value}
                    guard abs(sqrt(norm)-1)<=0.001 else{throw SceneError.invalid("Scaffold quaternions must be normalized wxyz rotations.")}
                }}
            }
            if let ref=object.data["edges"] as? String {
                let a=try document.array(ref);guard a.dtype=="i64",a.shape.count==2,a.shape[1]==2 else{throw SceneError.invalid("Scaffold edges require i64[E,2].")}
                let data=try document.readArray(ref)
                try data.withUnsafeBytes{raw in for i in 0..<a.shape[0]{let x=raw.loadUnaligned(fromByteOffset:i*16,as:Int64.self),y=raw.loadUnaligned(fromByteOffset:i*16+8,as:Int64.self);guard x>=0,y>=0,x<Int64(p.shape[1]),y<Int64(p.shape[1]) else{throw SceneError.invalid("Scaffold edge row is outside node table.")};links.append((Int(x),Int(y)))}}
            }
        }
        guard bounds.count==2,bounds.allSatisfy({$0.count==3}) else{throw SceneError.invalid("Geometry bounds are missing.")}
        let idArray = try document.array(idsID), timeArray = try document.array(timesID)
        guard idArray.dtype == "i64",idArray.shape.count == 1,idArray.shape[0] <= 10_000_000,
              timeArray.dtype == "f64",timeArray.shape == [frameIDs.count],
              (idArray.shape[0] == 0) == frameIDs.isEmpty else { throw SceneError.invalid("Gaussian IDs and time dimensions do not match.") }
        let n = idArray.shape[0], values = try document.doubles(timesID)
        guard values.allSatisfy({$0.isFinite}),zip(values.dropFirst(),values).allSatisfy({$0 > $1}) else { throw SceneError.invalid("Gaussian timestamps must be finite and strictly increasing.") }
        let low = SIMD3(bounds[0][0].floatValue,bounds[0][1].floatValue,bounds[0][2].floatValue), high = SIMD3(bounds[1][0].floatValue,bounds[1][1].floatValue,bounds[1][2].floatValue)
        guard (0..<3).allSatisfy({low[$0].isFinite && high[$0].isFinite && low[$0] <= high[$0]}) else { throw SceneError.invalid("Gaussian bounds must be finite and ordered.") }
        let idBytes = try document.readArray(idsID)
        let identityValues = idBytes.withUnsafeBytes { raw in (0..<n).map { Int64(littleEndian:raw.loadUnaligned(fromByteOffset:$0*8,as:Int64.self)) } }
        guard zip(identityValues.dropFirst(),identityValues).allSatisfy({$0 > $1}) else { throw SceneError.invalid("Gaussian IDs must be unique sorted signed 64-bit integers.") }
        var frames = [GSVArray]()
        if let positions { guard positions.shape == [frameIDs.count,n,3] else { throw SceneError.invalid("Scaffold trajectory dimensions disagree.") } }
        for id in (positions == nil ? frameIDs : []) {
            let array = try document.array(id)
            guard array.dtype == "f32",array.shape == [n,16],array.byteCount <= device.maxBufferLength else { throw SceneError.invalid("Gaussian frame \(id) has incompatible dimensions or exceeds the device buffer limit.") }
            frames.append(array)
        }
        self.document=document;self.object=object;self.device=device;count=n;frameCount=frameIDs.count;times=values
        frameArrays=frames;ids=identityValues;boundsMin=low;boundsMax=high;scaffoldPositions=positions;scaffoldVisibility=visibility;edges=links
    }
    func id(at index: Int) -> Int64? { index >= 0 && index < count ? ids[index] : nil }
    func index(of id: Int64) -> Int? {
        var low=0,high=count
        while low<high { let mid=low+(high-low)/2;if ids[mid]<id {low=mid+1} else {high=mid} }
        return low<count && ids[low]==id ? low : nil
    }
    func checkUnchanged() throws { try document.checkUnchanged() }
    func frame(_ index: Int) throws -> MTLBuffer {
        try checkUnchanged()
        guard index >= 0,index < frameCount,count > 0 else { throw SceneError.invalid("Frame index is outside this Gaussian object.") }
        lock.lock();defer {lock.unlock()}
        if let staticFrame {return staticFrame}
        if let buffer=frameCache[index] {lru.removeAll{$0==index};lru.append(index);return buffer}
        if let positions=scaffoldPositions {
            try document.verify(positions.buffer)
            let bytes=try document.readSnapshot(offset:Int64(positions.offset+index*count*12),count:count*12)
            var visibility=Data(repeating:1,count:count)
            if let source=scaffoldVisibility{try document.verify(source.buffer);visibility=try document.readSnapshot(offset:Int64(source.offset+index*count),count:count)}
            var records=[Float](repeating:0,count:count*16)
            let glyphSize=max(radius*0.003,0.0001)
            bytes.withUnsafeBytes{raw in for i in 0..<count {
                for c in 0..<3{records[i*16+c]=raw.loadUnaligned(fromByteOffset:i*12+c*4,as:Float.self);records[i*16+4+c]=glyphSize}
                records[i*16+3]=0.95;records[i*16+7]=visibility[i]==0 ? 0:1;records[i*16+8]=1
                records[i*16+12]=0.3;records[i*16+13]=0.95;records[i*16+14]=0.6;records[i*16+15] = -1
            }}
            guard let buffer=records.withUnsafeBytes({device.makeBuffer(bytes:$0.baseAddress!,length:$0.count,options:.storageModeShared)}) else{throw SceneError.invalid("Cannot allocate scaffold glyphs.")}
            frameCache[index]=buffer;lru.append(index);while lru.count>4{frameCache.removeValue(forKey:lru.removeFirst())};return buffer
        }
        let array=frameArrays[index];try document.verify(array.buffer)
        guard let buffer = device.makeBuffer(length: array.byteCount, options: .storageModeShared) else {
            throw SceneError.invalid("Metal cannot allocate the Gaussian frame.")
        }
        try document.readSnapshot(offset: Int64(array.offset),
            into: UnsafeMutableRawBufferPointer(start: buffer.contents(), count: array.byteCount))
        if !checked.contains(index) {
            let raw=buffer.contents().assumingMemoryBound(to:Float.self)
            var boundMagnitude=max(1.0,(0..<3).map{max(abs(Double(boundsMin[$0])),abs(Double(boundsMax[$0])))}.max() ?? 1)
            for row in 0..<count {
                let p=raw.advanced(by:row*16)
                guard (0..<16).allSatisfy({p[$0].isFinite}),p[3]>=0,p[3]<=1,p[4]>0,p[5]>0,p[6]>0,p[7]==0 || p[7]==1,
                      p[15] == -1 || (p[15]>=0 && p[15]<=1),
                      abs(p[8]*p[8]+p[9]*p[9]+p[10]*p[10]+p[11]*p[11]-1)<0.003 else {throw SceneError.invalid("Invalid Gaussian record at frame \(index), row \(row).")}
                if p[7]==1{boundMagnitude=max(boundMagnitude,4*Double(max(p[4],max(p[5],p[6]))));for c in 0..<3{boundMagnitude=max(boundMagnitude,abs(Double(p[c])))}}
            }
            let tolerance=8*Double(Float.ulpOfOne)*boundMagnitude
            for row in 0..<count {
                let p=raw.advanced(by:row*16)
                if p[7]==1{
                    let extent=4*Double(max(p[4],max(p[5],p[6])))
                    guard (0..<3).allSatisfy({Double(p[$0])-extent>=Double(boundsMin[$0])-tolerance && Double(p[$0])+extent<=Double(boundsMax[$0])+tolerance}) else{throw SceneError.invalid("Declared bounds do not enclose valid Gaussian support at frame \(index), row \(row).")}
                }
            }
            checked.insert(index)
        }
        buffer.label="\(object.label) · \(index)";frameCache[index]=buffer;lru.append(index)
        while lru.count>4 {frameCache.removeValue(forKey:lru.removeFirst())}
        return buffer
    }
    static func value(buffer: MTLBuffer,index: Int) -> GaussianValue {
        let row=buffer.contents().assumingMemoryBound(to:SIMD4<Float>.self).advanced(by:index*4)
        return GaussianValue(position:SIMD3(row[0].x,row[0].y,row[0].z),opacity:row[0].w,scale:SIMD3(row[1].x,row[1].y,row[1].z),quaternion:row[2],color:SIMD3(row[3].x,row[3].y,row[3].z),confidence:row[3].w,valid:row[1].w>0.5)
    }
    func trajectory(at index: Int,maxSamples: Int=4096) throws -> [SIMD4<Float>] {
        try checkUnchanged()
        guard index>=0,index<count,maxSamples>0 else {throw SceneError.invalid("Invalid trajectory identity or sample limit.")}
        if let staticFrame {let value = Self.value(buffer:staticFrame,index:index);return [SIMD4(value.position,value.valid ? 1:0)]}
        let samples=min(frameCount,maxSamples);var points=[SIMD4<Float>](),sample=0,nextFrame=0,gap:SIMD4<Float>?
        for frame in 0..<frameCount {
            let point: SIMD4<Float>
            if let positions=scaffoldPositions {
                try document.verify(positions.buffer)
                let data=try document.readSnapshot(offset:Int64(positions.offset+(frame*count+index)*12),count:12)
                var valid:Float=1
                if let source=scaffoldVisibility{try document.verify(source.buffer);valid=(try document.readSnapshot(offset:Int64(source.offset+frame*count+index),count:1))[0] == 0 ? 0:1}
                point=data.withUnsafeBytes{raw in SIMD4(raw.loadUnaligned(fromByteOffset:0,as:Float.self),raw.loadUnaligned(fromByteOffset:4,as:Float.self),raw.loadUnaligned(fromByteOffset:8,as:Float.self),valid)}
            }else{
                let item=frameArrays[frame];try document.verify(item.buffer)
                let data=try document.readSnapshot(offset:Int64(item.offset+index*64),count:64)
                point=data.withUnsafeBytes {raw in SIMD4(raw.loadUnaligned(fromByteOffset:0,as:Float.self),raw.loadUnaligned(fromByteOffset:4,as:Float.self),raw.loadUnaligned(fromByteOffset:8,as:Float.self),raw.loadUnaligned(fromByteOffset:28,as:Float.self))}
            }
            guard point.x.isFinite,point.y.isFinite,point.z.isFinite,point.w==0 || point.w==1 else {throw SceneError.invalid("Invalid trajectory record.")}
            if point.w==0,gap==nil {gap=point}
            if sample<samples,frame==nextFrame {
                if let gap,points.last?.w==1,point.w==1 {points.append(gap)}
                points.append(point);gap=nil;sample += 1
                if sample<samples {nextFrame=sample*(frameCount-1)/(samples-1)}
            }
        }
        try checkUnchanged();return points
    }
}
