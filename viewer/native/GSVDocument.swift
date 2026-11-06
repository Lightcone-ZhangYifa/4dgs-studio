import Foundation
import CoreFoundation
import CryptoKit
import Darwin

struct GSVObject {
    let id: String
    let type: String
    let encoding: String
    let label: String
    let space: String?
    let clock: String?
    let data: [String: Any]
    let metadata: [String: Any]
    let descriptor: [String: Any]
}

struct GSVArray {
    let id: String
    let buffer: String
    let dtype: String
    let shape: [Int]
    let offset: Int
    let byteCount: Int
}

struct GSVBuffer {
    let offset: Int
    let length: Int
    let sha256: String
}

/// The document envelope knows only objects, numeric arrays and byte buffers.
/// Semantics belong to independently identified object encodings.
final class GSVDocument {
    let url: URL
    let fileBytes: Int64
    let manifest: [String: Any]
    let manifestSHA256: String
    let objects: [GSVObject]
    let arrays: [String: GSVArray]
    let buffers: [String: GSVBuffer]
    private let fd: Int32
    private let originalStat: stat
    private var verified = Set<String>()
    private let verificationLock = NSLock()

    init(url: URL) throws {
        self.url = url
        let descriptor = Darwin.open(url.path, O_RDONLY | O_CLOEXEC | O_NONBLOCK)
        guard descriptor >= 0 else { throw SceneError.invalid("Cannot open GSV document: \(String(cString: strerror(errno)))") }
        var owned = false
        defer { if !owned { Darwin.close(descriptor) } }
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_size >= 64 else { throw SceneError.invalid("The GSV envelope is truncated.") }
        let prefix = try Self.read(descriptor, offset: 0, count: 64)
        guard prefix.prefix(8) == Data([0x47,0x53,0x56,0,0x0d,0x0a,0x1a,0x0a]) else { throw SceneError.invalid("This file is not a GSV scene container.") }
        func u64(_ offset: Int) -> UInt64 { prefix.withUnsafeBytes { UInt64(littleEndian: $0.loadUnaligned(fromByteOffset: offset, as: UInt64.self)) } }
        let fileLength = u64(8), manifestOffset = u64(16), manifestLength = u64(24)
        guard fileLength == UInt64(info.st_size), fileLength <= UInt64(Int.max-16383), manifestOffset >= 64,
              manifestLength > 0, manifestLength <= 64*1024*1024,
              manifestOffset <= fileLength, manifestLength == fileLength-manifestOffset else { throw SceneError.invalid("GSV manifest location or declared file size is invalid.") }
        let bytes = try Self.read(descriptor, offset: Int64(manifestOffset), count: Int(manifestLength))
        guard Data(SHA256.hash(data: bytes)) == prefix.subdata(in: 32..<64) else { throw SceneError.invalid("GSV manifest checksum failed.") }
        var strictJSON = StrictJSON(bytes); try strictJSON.validate()
        guard let json = try JSONSerialization.jsonObject(with: bytes) as? [String: Any], json["format"] as? String == "gsv.scene",
              let rawBuffers = json["buffers"] as? [String: [String: Any]],
              let rawArrays = json["arrays"] as? [String: [String: Any]],
              let rawObjects = json["objects"] as? [[String: Any]] else { throw SceneError.invalid("GSV requires a scene manifest with objects, arrays and buffers.") }
        var parsedBuffers = [String: GSVBuffer]()
        for (id, item) in rawBuffers {
            guard !id.isEmpty, let offset = Self.integer(item["offset"]), let length = Self.integer(item["length"]),
                  offset >= 64, offset % 16384 == 0, length >= 0, offset <= manifestOffset,
                  length <= Int(manifestOffset)-offset, ((offset+length+16383)/16384)*16384 <= manifestOffset, let hash = item["sha256"] as? String,
                  hash.count == 64, hash.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else { throw SceneError.invalid("Invalid buffer descriptor: \(id)") }
            parsedBuffers[id] = GSVBuffer(offset: offset, length: length, sha256: hash)
        }
        let regions = parsedBuffers.values.filter { $0.length > 0 }.sorted { $0.offset < $1.offset }
        for index in 1..<max(1, regions.count) {
            if regions[index].offset < ((regions[index-1].offset+regions[index-1].length+16383)/16384)*16384 { throw SceneError.invalid("GSV buffers overlap.") }
        }
        let sizes = ["f32":4,"f64":8,"i8":1,"u8":1,"i16":2,"u16":2,"i32":4,"u32":4,"i64":8,"u64":8,"bool":1]
        var parsedArrays = [String: GSVArray]()
        for (id, item) in rawArrays {
            guard !id.isEmpty, let bufferID = item["buffer"] as? String, let buffer = parsedBuffers[bufferID],
                  let dtype = item["dtype"] as? String, let width = sizes[dtype],
                  let dimensions = item["shape"] as? [Any], dimensions.count <= 16,
                  let offset = Self.integer(item["byte_offset"] ?? 0), offset >= 0, offset % width == 0 else { throw SceneError.invalid("Invalid numeric array descriptor: \(id)") }
            var count = 1, shape = [Int]()
            for dimension in dimensions {
                guard let value = Self.integer(dimension), value >= 0 else { throw SceneError.invalid("Invalid array shape: \(id)") }
                let (product, overflow) = count.multipliedReportingOverflow(by: value)
                guard !overflow else { throw SceneError.invalid("Array shape overflows addressable storage: \(id)") }
                count = product; shape.append(value)
            }
            let (bytes, overflow) = count.multipliedReportingOverflow(by: width)
            guard !overflow, offset <= buffer.length, bytes <= buffer.length-offset else { throw SceneError.invalid("Array exceeds its buffer: \(id)") }
            parsedArrays[id] = GSVArray(id:id, buffer:bufferID, dtype:dtype, shape:shape, offset:buffer.offset+offset, byteCount:bytes)
        }
        var parsedObjects = [GSVObject](), objectIDs = Set<String>()
        for item in rawObjects {
            guard let id = item["id"] as? String, !id.isEmpty, objectIDs.insert(id).inserted,
                  let type = item["type"] as? String, !type.isEmpty,
                  let encoding = item["encoding"] as? String, !encoding.isEmpty,
                  let data = item["data"] as? [String: Any] else { throw SceneError.invalid("Each GSV object requires a unique ID, type, encoding and data map.") }
            parsedObjects.append(GSVObject(id:id,type:type,encoding:encoding,label:item["label"] as? String ?? id,
                space:item["space"] as? String,clock:item["clock"] as? String,data:data,metadata:item["metadata"] as? [String:Any] ?? [:],descriptor:item))
        }
        let spaces = json["spaces"] as? [String: Any] ?? [:], clocks = json["clocks"] as? [String: Any] ?? [:]
        for field in ["spaces","clocks","metadata"] {if let value=json[field],!(value is [String:Any]){throw SceneError.invalid("GSV \(field) must be a map.")}}
        for (id,raw) in spaces {
            guard !id.isEmpty,let space=raw as? [String:Any],let unit=space["unit"] as? String,!unit.isEmpty,["right","left"].contains(space["handedness"] as? String ?? ""),let axes=space["axes"] as? [String],axes.count==3,Set(axes).count==3,axes.allSatisfy({!$0.isEmpty}) else{throw SceneError.invalid("Coordinate spaces require units, handedness and three distinct axis labels.")}
        }
        var clockTargets=[String:String]()
        for (id, raw) in clocks {
            guard !id.isEmpty,let clock=raw as? [String:Any],["second","frame","normalized"].contains(clock["unit"] as? String ?? "") else{throw SceneError.invalid("GSV clocks must declare a supported time unit.")}
            if let value=clock["mapping"] {
                guard let mapping=value as? [String:Any],let target=mapping["target"] as? String,clocks[target] != nil,target != id,
                      let scale=mapping["scale"] as? NSNumber,CFGetTypeID(scale) != CFBooleanGetTypeID(),scale.doubleValue.isFinite,scale.doubleValue>0,
                      let offset=mapping["offset"] as? NSNumber,CFGetTypeID(offset) != CFBooleanGetTypeID(),offset.doubleValue.isFinite else{throw SceneError.invalid("Clock mapping requires a known target, positive finite scale and finite offset.")}
                clockTargets[id]=target
            }
        }
        for id in clockTargets.keys {var seen=Set<String>(),cursor=id;while let target=clockTargets[cursor]{guard seen.insert(cursor).inserted else{throw SceneError.invalid("Clock mappings cannot contain cycles.")};cursor=target}}
        for object in parsedObjects {
            let types = [
                "gsv.gaussian.persistent-identity":"gaussian", "gsv.gaussian.static":"gaussian",
                "gsv.gaussian.spacetime-polynomial":"gaussian", "gsv.gaussian.covariance4d":"gaussian",
                "gsv.gaussian.se3-basis":"gaussian", "gsv.gaussian.canonical-deformation":"gaussian",
                "gsv.gaussian.scaffold-deformation":"gaussian", "gsv.scaffold.graph":"scaffold",
                "gsv.camera.pinhole":"camera", "gsv.motion.se3-basis":"motion", "gsv.motion.neural":"motion",
                "gsv.tracks.points":"tracks", "gsv.tracks.image":"tracks",
                "gsv.transform.sampled-affine":"transform", "gsv.binding.weights":"binding",
                "gsv.geometry.points":"pointcloud", "gsv.geometry.triangles":"mesh",
                "gsv.observation.image":"image", "gsv.observation.camera-rgb":"image",
                "gsv.tensor.bundle":"tensor", "gsv.provenance.record":"provenance",
                "gsv.appearance.camera-affine":"appearance", "gsv.render.profile":"render", "gsv.render.view":"render"
            ]
            if object.encoding.hasPrefix("gsv."), types[object.encoding] == nil {
                throw SceneError.invalid("Unregistered reserved GSV encoding: \(object.encoding)")
            }
            if let type=types[object.encoding],object.type != type{throw SceneError.invalid("Object type does not match its registered encoding.")}
            if ["gsv.gaussian.persistent-identity","gsv.gaussian.static","gsv.gaussian.spacetime-polynomial","gsv.gaussian.covariance4d","gsv.gaussian.se3-basis"].contains(object.encoding),
               !["srgb","linear-srgb"].contains(object.data["color_space"] as? String ?? "") {throw SceneError.invalid("RGB Gaussian fields require a typed color_space.")}
            if let space = object.space, spaces[space] == nil { throw SceneError.invalid("Unknown object coordinate space: \(space)") }
            if let clock = object.clock, clocks[clock] == nil { throw SceneError.invalid("Unknown object clock: \(clock)") }
            if let rawLinks = object.descriptor["links"] {
                guard let links = rawLinks as? [String: Any] else { throw SceneError.invalid("Object links must be a map.") }
                guard object.type != "gaussian" || links["radiometry"] == nil else {throw SceneError.invalid("Camera appearance belongs to a render-view binding, not a Gaussian field.")}
                for (_, value) in links { let refs = (value as? [String]) ?? (value as? String).map{[$0]}; guard let refs, refs.allSatisfy({objectIDs.contains($0)}) else { throw SceneError.invalid("Dangling GSV object link.") } }
            }
        }
        fd = descriptor; originalStat = info; fileBytes = info.st_size; manifest = json; manifestSHA256 = Self.digest(bytes)
        objects = parsedObjects; buffers = parsedBuffers; arrays = parsedArrays; owned = true
    }
    deinit { Darwin.close(fd) }
    static func fields(_ value:[String:Any],_ required:[String],optional:[String] = [],context:String) throws {
    let keys = Set(value.keys),must = Set(required),allowed = must.union(optional)
    guard must.isSubset(of:keys),keys.isSubset(of:allowed) else {throw SceneError.invalid("Unknown or missing fields in " + context)}
  }
  static func number(_ value: Any?) -> Double? {
        guard let n = value as? NSNumber,CFGetTypeID(n) != CFBooleanGetTypeID(),n.doubleValue.isFinite else {return nil}
        return n.doubleValue
    }
    static func boolean(_ value: Any?) -> Bool? {
        guard let n = value as? NSNumber,CFGetTypeID(n) == CFBooleanGetTypeID() else {return nil}
        return n.boolValue
    }
    static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let text = number.stringValue
        return Int(text)
    }
    static func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format:"%02x",$0) }.joined() }
    private static func read(_ fd: Int32, offset: Int64, into bytes: UnsafeMutableRawBufferPointer) throws {
        guard let start = bytes.baseAddress, bytes.count > 0 else { return }
        var done = 0
        while done < bytes.count {
            let count = pread(fd, start.advanced(by: done), bytes.count - done, offset + Int64(done))
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { throw SceneError.invalid("Cannot read complete GSV data.") }
            done += count
        }
    }
    private static func read(_ fd: Int32, offset: Int64, count: Int) throws -> Data {
        var data = Data(count: count)
        try data.withUnsafeMutableBytes { try read(fd, offset: offset, into: $0) }
        return data
    }
    /// Copy into storage owned by the caller. A later source truncation cannot
    /// invalidate the returned bytes while a GPU command still references them.
    func readSnapshot(offset: Int64, into bytes: UnsafeMutableRawBufferPointer) throws {
        guard offset >= 0, offset <= fileBytes, Int64(bytes.count) <= fileBytes - offset else {
            throw SceneError.invalid("Read exceeds the GSV file bounds.")
        }
        try checkUnchanged()
        try Self.read(fd, offset: offset, into: bytes)
        try checkUnchanged()
    }
    func readSnapshot(offset: Int64, count: Int) throws -> Data {
        guard count >= 0 else { throw SceneError.invalid("Invalid byte count.") }
        var data = Data(count: count)
        try data.withUnsafeMutableBytes { try readSnapshot(offset: offset, into: $0) }
        return data
    }
    func checkUnchanged() throws {
        var current = stat()
        guard fstat(fd,&current) == 0,current.st_size == originalStat.st_size,current.st_ino == originalStat.st_ino,
              current.st_mtimespec.tv_sec == originalStat.st_mtimespec.tv_sec,current.st_mtimespec.tv_nsec == originalStat.st_mtimespec.tv_nsec,
              current.st_ctimespec.tv_sec == originalStat.st_ctimespec.tv_sec,current.st_ctimespec.tv_nsec == originalStat.st_ctimespec.tv_nsec else {
            throw SceneError.invalid("This GSV document changed while open. Reopen it to verify its contents.")
        }
    }
    func array(_ id: String) throws -> GSVArray {
        guard let array = arrays[id] else { throw SceneError.invalid("Missing array: \(id)") }; return array
    }
    func verify(_ bufferID: String) throws {
        try checkUnchanged(); verificationLock.lock(); defer { verificationLock.unlock() }
        guard !verified.contains(bufferID) else { return }
        guard let buffer = buffers[bufferID] else { throw SceneError.invalid("Missing buffer: \(bufferID)") }
        var hash = SHA256(), cursor = 0
        while cursor < buffer.length {
            let count = min(4*1024*1024,buffer.length-cursor)
            hash.update(data:try Self.read(fd,offset:Int64(buffer.offset+cursor),count:count));cursor += count
        }
        guard hash.finalize().map({String(format:"%02x",$0)}).joined() == buffer.sha256 else { throw SceneError.invalid("Buffer checksum failed: \(bufferID)") }
        try checkUnchanged(); verified.insert(bufferID)
    }
    func readArray(_ id: String, limit: Int = 128*1024*1024) throws -> Data {
        let item = try array(id)
        guard item.byteCount <= limit else { throw SceneError.invalid("Array \(id) exceeds this inspector's memory limit.") }
        try verify(item.buffer)
        return try readSnapshot(offset: Int64(item.offset), count: item.byteCount)
    }
    func doubles(_ id: String) throws -> [Double] {
        let item = try array(id), data = try readArray(id)
        guard item.dtype == "f64" || item.dtype == "f32" else { throw SceneError.invalid("Expected a floating-point array: \(id)") }
        return data.withUnsafeBytes { raw in
            if item.dtype == "f64" { return (0..<data.count/8).map { Double(bitPattern:UInt64(littleEndian:raw.loadUnaligned(fromByteOffset:$0*8,as:UInt64.self))) } }
            return (0..<data.count/4).map { Double(Float(bitPattern:UInt32(littleEndian:raw.loadUnaligned(fromByteOffset:$0*4,as:UInt32.self)))) }
        }
    }
}

/// Reject duplicate keys before Foundation turns objects into dictionaries.
private struct StrictJSON {
    var bytes: [UInt8]
    var cursor = 0
    init(_ data: Data) { bytes = Array(data) }
    mutating func validate() throws {
        try value(0);space();guard cursor == bytes.count else{throw SceneError.invalid("Trailing bytes in GSV JSON.")}
    }
    mutating func space(){while cursor<bytes.count,[9,10,13,32].contains(bytes[cursor]){cursor += 1}}
    mutating func string() throws -> String {
        guard cursor<bytes.count,bytes[cursor]==34 else{throw SceneError.invalid("Expected a JSON string.")}
        let start=cursor;cursor += 1
        while cursor<bytes.count {
            let byte=bytes[cursor];cursor += 1
            if byte==34 {
                let result=try JSONSerialization.jsonObject(with:Data(bytes[start..<cursor]),options:[.fragmentsAllowed])
                guard let text=result as? String else{throw SceneError.invalid("Invalid JSON string.")};return text
            }
            if byte==92 {cursor += 1}
        }
        throw SceneError.invalid("Unterminated JSON string.")
    }
    mutating func value(_ depth:Int) throws {
        guard depth<96 else{throw SceneError.invalid("GSV JSON nesting is too deep.")};space()
        guard cursor<bytes.count else{throw SceneError.invalid("Incomplete JSON value.")}
        switch bytes[cursor] {
        case 123:
            cursor += 1;space();var keys=Set<String>()
            if cursor<bytes.count,bytes[cursor]==125{cursor += 1;return}
            while true {
                space();let key=try string();guard keys.insert(key).inserted else{throw SceneError.invalid("Duplicate JSON key: \(key)")}
                space();guard cursor<bytes.count,bytes[cursor]==58 else{throw SceneError.invalid("Missing JSON colon.")};cursor += 1;try value(depth+1);space()
                guard cursor<bytes.count else{throw SceneError.invalid("Incomplete JSON object.")}
                let separator=bytes[cursor];cursor += 1;if separator==125{return};guard separator==44 else{throw SceneError.invalid("Invalid JSON object separator.")}
            }
        case 91:
            cursor += 1;space();if cursor<bytes.count,bytes[cursor]==93{cursor += 1;return}
            while true {try value(depth+1);space();guard cursor<bytes.count else{throw SceneError.invalid("Incomplete JSON array.")};let separator=bytes[cursor];cursor += 1;if separator==93{return};guard separator==44 else{throw SceneError.invalid("Invalid JSON array separator.")}}
        case 34:_=try string()
        default:
            let start=cursor
            while cursor<bytes.count,![9,10,13,32,44,93,125].contains(bytes[cursor]){cursor += 1}
            let token=String(bytes:bytes[start..<cursor],encoding:.utf8) ?? ""
            if ["true","false","null"].contains(token){return}
            guard let number=Double(token),number.isFinite else{throw SceneError.invalid("Invalid or nonfinite GSV JSON number.")}
        }
    }
}
