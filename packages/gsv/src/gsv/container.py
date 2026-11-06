"""Fixed envelope, typed buffers and extensible peer objects."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import struct
import tempfile
from pathlib import Path

import numpy as np

MAGIC = b"GSV\x00\r\n\x1a\n"
HEADER = struct.Struct("<8sQQQ32s")
ALIGNMENT = 16384
MAX_MANIFEST = 64 * 1024**2
DTYPES = {
    "f32": "<f4",
    "f64": "<f8",
    "i8": "i1",
    "u8": "u1",
    "i16": "<i2",
    "u16": "<u2",
    "i32": "<i4",
    "u32": "<u4",
    "i64": "<i8",
    "u64": "<u8",
    "bool": "?",
}
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


class GSVError(ValueError):
    pass


def aligned(n):
    return ((n + ALIGNMENT - 1) // ALIGNMENT) * ALIGNMENT


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise GSVError(f"{name} must be an integer in [{minimum}, 2^63-1]")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GSVError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(data):
    try:
        result = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda s: (_ for _ in ()).throw(
                GSVError(f"Nonfinite JSON: {s}")
            ),
        )
        _json_values(result)
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
        raise GSVError(f"Invalid manifest: {error}") from error


def _json_values(value):
    # JSON's exponent syntax can overflow a decoder even without a NaN token.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise GSVError("Nonfinite JSON number")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _name(value, what):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise GSVError(f"Invalid {what}")


def _validate_manifest(m, manifest_offset):
    if not isinstance(m, dict) or m.get("format") != "gsv.scene":
        raise GSVError("Invalid scene manifest")
    if (
        not isinstance(m.get("objects"), list)
        or not isinstance(m.get("buffers"), dict)
        or not isinstance(m.get("arrays"), dict)
    ):
        raise GSVError("objects, buffers and arrays are required")
    for field in ("spaces", "clocks", "metadata"):
        if field in m and not isinstance(m[field], dict):
            raise GSVError(f"{field} must be a dictionary")
    for key, space in m.get("spaces", {}).items():
        _name(key, "space ID")
        if not isinstance(space, dict):
            raise GSVError("Invalid space descriptor")
        _name(space.get("unit"), "space unit")
        if space.get("handedness") not in ("right", "left"):
            raise GSVError("Space handedness must be right or left")
        axes = space.get("axes")
        if (
            not isinstance(axes, list)
            or len(axes) != 3
            or any(not isinstance(x, str) or not x for x in axes)
            or len(set(axes)) != 3
        ):
            raise GSVError("Space requires three distinct axis labels")
    ranges = []
    for key, b in m["buffers"].items():
        _name(key, "buffer ID")
        if not isinstance(b, dict):
            raise GSVError("Invalid buffer descriptor")
        off = _integer(b.get("offset"), "buffer offset", ALIGNMENT)
        n = _integer(b.get("length"), "buffer length")
        if off % ALIGNMENT or off + aligned(n) > manifest_offset:
            raise GSVError(f"Buffer outside payload: {key}")
        if not isinstance(b.get("sha256"), str) or not SHA_RE.fullmatch(b["sha256"]):
            raise GSVError("Invalid buffer checksum")
        if n:
            ranges.append((off, off + aligned(n)))
    ranges.sort()
    if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
        raise GSVError("Overlapping buffer reservations")
    for key, a in m["arrays"].items():
        _name(key, "array ID")
        if (
            not isinstance(a, dict)
            or not isinstance(a.get("buffer"), str)
            or a["buffer"] not in m["buffers"]
            or not isinstance(a.get("dtype"), str)
            or a["dtype"] not in DTYPES
        ):
            raise GSVError(f"Invalid array: {key}")
        shape = a.get("shape")
        if not isinstance(shape, list) or len(shape) > 16:
            raise GSVError("Array rank must be at most 16")
        for d in shape:
            _integer(d, "array dimension")
        size = math.prod(shape) * np.dtype(DTYPES[a["dtype"]]).itemsize
        off = _integer(a.get("byte_offset", 0), "array offset")
        if size > 2**63 - 1 or off + size > m["buffers"][a["buffer"]]["length"]:
            raise GSVError(f"Array exceeds buffer: {key}")
        if off % np.dtype(DTYPES[a["dtype"]]).itemsize:
            raise GSVError("Unaligned typed array")
    ids = set()
    for obj in m["objects"]:
        if not isinstance(obj, dict):
            raise GSVError("Object must be a dictionary")
        for f in ("id", "type", "encoding"):
            _name(obj.get(f), f)
        if obj["encoding"].startswith("gsv."):
            from .codecs import CODECS

            if obj["encoding"] not in CODECS:
                raise GSVError("Unregistered core encoding: " + obj["encoding"])
        if obj["id"] in ids:
            raise GSVError("Duplicate object ID")
        ids.add(obj["id"])
        if not isinstance(obj.get("data"), dict):
            raise GSVError("Object data must be a dictionary")
        if "metadata" in obj and not isinstance(obj["metadata"], dict):
            raise GSVError("Object metadata must be a dictionary")
        for field in ("space", "clock"):
            if field in obj and (
                not isinstance(obj[field], str)
                or obj[field] not in m.get(field + "s", {})
            ):
                raise GSVError(f"Unknown {field} reference")
    for obj in m["objects"]:
        links = obj.get("links", {})
        if not isinstance(links, dict):
            raise GSVError("links must be a dictionary")
        for value in links.values():
            refs = value if isinstance(value, list) else [value]
            if any(not isinstance(x, str) or x not in ids for x in refs):
                raise GSVError("Dangling object link")
    for k, v in m.get("clocks", {}).items():
        _name(k, "clock ID")
        if not isinstance(v, dict) or v.get("unit") not in (
            "second",
            "frame",
            "normalized",
        ):
            raise GSVError("Unknown clock unit")
        if "mapping" in v:
            x = v["mapping"]
            if (
                not isinstance(x, dict)
                or not isinstance(x.get("target"), str)
                or x["target"] not in m["clocks"]
                or type(x.get("scale")) not in (int, float)
                or not math.isfinite(x["scale"])
                or x["scale"] <= 0
                or type(x.get("offset")) not in (int, float)
                or not math.isfinite(x["offset"])
            ):
                raise GSVError("Invalid clock mapping")
    clocks = m.get("clocks", {})
    resolved = set()
    for start in clocks:
        clock = start
        chain = set()
        while clock not in resolved:
            if clock in chain:
                raise GSVError("Cyclic clock mapping")
            chain.add(clock)
            mapping = clocks[clock].get("mapping")
            if mapping is None:
                break
            clock = mapping["target"]
        resolved.update(chain)


class Writer:
    """Stream array payloads to a temporary file; publish only on successful close.

    Objects are optional. Use ``with Writer(path) as out: pass`` for an empty scene.
    """

    def __init__(self, path, *, metadata=None, overwrite=False, validate=True):
        self.path = Path(path)
        self.overwrite = overwrite
        self._validate = validate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not overwrite:
            raise FileExistsError(self.path)
        fd, name = tempfile.mkstemp(
            prefix="." + self.path.name + ".", suffix=".partial", dir=self.path.parent
        )
        self._path = Path(name)
        self._file = os.fdopen(fd, "w+b")
        self._file.write(bytes(HEADER.size))
        self.manifest = {
            "format": "gsv.scene",
            "objects": [],
            "arrays": {},
            "buffers": {},
            "spaces": {},
            "clocks": {},
            "metadata": metadata or {},
        }
        self.closed = False

    def _require_open(self):
        if self.closed or self._file.closed:
            raise GSVError("Writer is closed; create a new writer for another document")

    def _buffer(self, key, chunks, *, extra=None):
        self._require_open()
        if key in self.manifest["buffers"]:
            raise GSVError("Duplicate buffer ID")
        offset = aligned(self._file.tell())
        self._file.write(bytes(offset - self._file.tell()))
        h = hashlib.sha256()
        length = 0
        for chunk in chunks:
            self._file.write(chunk)
            h.update(chunk)
            length += len(chunk)
        self._file.write(bytes(aligned(self._file.tell()) - self._file.tell()))
        self.manifest["buffers"][key] = {
            **(extra or {}),
            "offset": offset,
            "length": length,
            "sha256": h.hexdigest(),
        }

    def add_array(self, key, values):
        self._require_open()
        _name(key, "array ID")
        if key in self.manifest["arrays"]:
            raise GSVError("Duplicate array ID")
        a = np.asarray(values)
        if a.dtype.hasobject or a.dtype.fields is not None:
            raise GSVError("Only numeric or boolean arrays are supported")
        match = next(
            (
                k
                for k, v in DTYPES.items()
                if np.dtype(v).kind == a.dtype.kind
                and np.dtype(v).itemsize == a.dtype.itemsize
            ),
            None,
        )
        if match is None:
            raise GSVError(f"Unsupported dtype: {a.dtype}")
        shape = a.shape
        a = np.ascontiguousarray(a, dtype=DTYPES[match]).reshape(shape)
        raw = memoryview(a).cast("B") if a.size else memoryview(b"")
        if match == "bool":
            # NumPy can expose bool views backed by arbitrary nonzero bytes.
            # The on-disk contract uses canonical 0/1, independent of producer.
            a = np.minimum(a.view(np.uint8), np.uint8(1))
            raw = memoryview(a).cast("B") if a.size else memoryview(b"")
        self._buffer(
            key, (raw[i : i + 8 * 1024**2] for i in range(0, len(raw), 8 * 1024**2))
        )
        self.manifest["arrays"][key] = {
            "buffer": key,
            "dtype": match,
            "shape": list(a.shape),
            "byte_offset": 0,
        }
        return key

    def add_object(self, id, type, encoding, data, **fields):
        self._require_open()
        obj = {**fields, "id": id, "type": type, "encoding": encoding, "data": data}
        self.manifest["objects"].append(obj)
        return obj

    def close(self):
        if self.closed:
            return
        try:
            offset = self._file.tell()
            try:
                payload = json.dumps(
                    self.manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (ValueError, TypeError, RecursionError) as error:
                raise GSVError(f"Invalid manifest: {error}") from error
            if len(payload) > MAX_MANIFEST:
                raise GSVError("Manifest exceeds 64 MiB")
            # Validate the bytes that will actually be published. JSON encoding can
            # turn distinct Python dictionary keys into the same string.
            _validate_manifest(_json(payload), offset)
            self._file.write(payload)
            length = self._file.tell()
            self._file.seek(0)
            self._file.write(
                HEADER.pack(
                    MAGIC,
                    length,
                    offset,
                    len(payload),
                    hashlib.sha256(payload).digest(),
                )
            )
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            if self._validate:
                from .codecs import validate_object

                with Scene(self._path, verify=False) as scene:
                    # Public manifest editing may add typed views over an
                    # existing buffer. Validate even unreferenced Boolean
                    # aliases; add_array's normalization alone is insufficient.
                    for key, a in scene.manifest["arrays"].items():
                        if a["dtype"] == "bool":
                            scene.array(key)
                    for obj in scene.objects:
                        validate_object(scene, obj)
            if self.overwrite:
                os.replace(self._path, self.path)
            else:
                os.link(self._path, self.path)
                self._path.unlink()
            # Persist the directory entry as well as the completed file payload.
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.closed = True
        except BaseException:
            self.abort()
            raise

    def abort(self):
        if not self._file.closed:
            self._file.close()
        self._path.unlink(missing_ok=True)
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind:
            self.abort()
        else:
            self.close()


class Scene:
    """Immutable scene view with bounded manifest and lazy hash-verified arrays."""

    def __init__(self, path, *, verify=True, max_array_bytes=16 * 1024**3):
        self.path = Path(path)
        self.verify = verify
        self.max_array_bytes = max_array_bytes
        self._verified = set()
        self._file = self.path.open("rb")
        self._stat = os.fstat(self._file.fileno())
        try:
            prefix = self._file.read(HEADER.size)
            if len(prefix) != HEADER.size:
                raise GSVError("Truncated scene header")
            magic, size, offset, length, digest = HEADER.unpack(prefix)
            if magic != MAGIC:
                raise GSVError("Not a GSV scene container")
            if (
                size != self._stat.st_size
                or offset < HEADER.size
                or not 0 < length <= MAX_MANIFEST
                or offset + length != size
            ):
                raise GSVError("Invalid manifest extent or file size")
            self._file.seek(offset)
            raw = self._file.read(length)
            if hashlib.sha256(raw).digest() != digest:
                raise GSVError("Manifest checksum mismatch")
            self.manifest = _json(raw)
            _validate_manifest(self.manifest, offset)
            # Associate Boolean views once. Long sequences commonly have one
            # buffer per frame/image; scanning every array for every buffer
            # makes an otherwise sequential integrity audit quadratic.
            self._boolean_arrays = {}
            for a in self.manifest["arrays"].values():
                if a["dtype"] == "bool":
                    self._boolean_arrays.setdefault(a["buffer"], []).append(a)
        except BaseException:
            self._file.close()
            raise

    @property
    def objects(self):
        return self.manifest["objects"]

    def object(self, id):
        return next(o for o in self.objects if o["id"] == id)

    def _unchanged(self):
        s = os.fstat(self._file.fileno())
        if (s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (
            self._stat.st_size,
            self._stat.st_mtime_ns,
            self._stat.st_ctime_ns,
        ):
            raise GSVError("Scene changed while open")

    def verify_buffer(self, key):
        self._unchanged()
        if key in self._verified:
            return
        b = self.manifest["buffers"][key]
        remaining = b["length"]
        position = b["offset"]
        h = hashlib.sha256()
        while remaining:
            block = os.pread(self._file.fileno(), min(remaining, 8 * 1024**2), position)
            if not block:
                raise GSVError("Truncated buffer")
            h.update(block)
            remaining -= len(block)
            position += len(block)
        if h.hexdigest() != b["sha256"]:
            raise GSVError(f"Buffer checksum mismatch: {key}")
        padding = aligned(b["length"]) - b["length"]
        if padding and os.pread(self._file.fileno(), padding, position) != bytes(
            padding
        ):
            raise GSVError(f"Invalid buffer padding: {key}")
        for a in self._boolean_arrays.get(key, ()):
            position = b["offset"] + a.get("byte_offset", 0)
            remaining = math.prod(a["shape"])
            while remaining:
                length = min(remaining, 8 * 1024**2)
                block = os.pread(self._file.fileno(), length, position)
                if len(block) != length or np.any(
                    np.frombuffer(block, dtype=np.uint8) > 1
                ):
                    raise GSVError("Boolean arrays require canonical 0/1 bytes")
                position += length
                remaining -= length
        self._unchanged()
        self._verified.add(key)

    def array(self, key):
        self._unchanged()
        a = self.manifest["arrays"][key]
        b = self.manifest["buffers"][a["buffer"]]
        n = math.prod(a["shape"]) * np.dtype(DTYPES[a["dtype"]]).itemsize
        if n > self.max_array_bytes:
            raise GSVError("Array exceeds configured access limit")
        if self.verify:
            self.verify_buffer(a["buffer"])
        if not n:
            empty = np.empty(a["shape"], dtype=DTYPES[a["dtype"]])
            empty.flags.writeable = False
            return empty
        mapped = np.memmap(
            self._file,
            dtype=DTYPES[a["dtype"]],
            mode="r",
            offset=b["offset"] + a.get("byte_offset", 0),
            shape=tuple(a["shape"]),
            order="C",
        )
        if a["dtype"] == "bool" and not self.verify:
            raw = mapped.view(np.uint8).reshape(-1)
            for start in range(0, len(raw), 8 * 1024**2):
                if np.any(raw[start : start + 8 * 1024**2] > 1):
                    raise GSVError("Boolean arrays require canonical 0/1 bytes")
        return mapped

    def convert_time(self, time, source, target):
        """Convert explicitly related clocks; unrelated clocks never align implicitly."""
        value = np.asarray(time, dtype=np.float64)
        if not np.isfinite(value).all():
            raise GSVError("Time must be finite")
        clocks = self.manifest.get("clocks", {})

        def ancestors(clock):
            if clock not in clocks:
                raise GSVError(f"Unknown clock: {clock}")
            path = [clock]
            while "mapping" in clocks[clock]:
                clock = clocks[clock]["mapping"]["target"]
                path.append(clock)
            return path

        sp, tp = ancestors(source), ancestors(target)
        common = next((clock for clock in sp if clock in tp), None)
        if common is None:
            raise GSVError("Clocks have no declared common time reference")

        def affine(clock):
            scale, offset = 1.0, 0.0
            while clock != common:
                m = clocks[clock]["mapping"]
                scale *= m["scale"]
                offset = offset * m["scale"] + m["offset"]
                clock = m["target"]
            if not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset):
                raise GSVError("Clock mapping overflows")
            return scale, offset

        ss, so = affine(source)
        ts, to = affine(target)
        # Cancel shared origins before adding the sample value. Traversing only
        # to the closest common ancestor also avoids irrelevant large offsets.
        result = value * (ss / ts) + (so - to) / ts
        if not np.isfinite(result).all():
            raise GSVError("Converted time overflows")
        return float(result) if result.ndim == 0 else result

    def validate(self, *, semantic=True):
        from .codecs import validate_object

        for key in self.manifest["buffers"]:
            self.verify_buffer(key)
        return [
            validate_object(self, obj)
            if semantic
            else {"id": obj["id"], "status": "envelope_verified"}
            for obj in self.objects
        ]

    def copy(self, path, *, overwrite=False):
        """Preserve unknown objects and manifest extension fields without execution."""
        with Writer(path, overwrite=overwrite) as out:
            out.manifest = copy.deepcopy(self.manifest)
            out.manifest["buffers"] = {}
            for key, b in self.manifest["buffers"].items():
                self.verify_buffer(key)

                def chunks():
                    position = b["offset"]
                    left = b["length"]
                    while left:
                        chunk = os.pread(
                            self._file.fileno(), min(left, 8 * 1024**2), position
                        )
                        if not chunk:
                            raise GSVError("Source truncated during copy")
                        left -= len(chunk)
                        position += len(chunk)
                        yield chunk

                out._buffer(
                    key,
                    chunks(),
                    extra={
                        k: v
                        for k, v in b.items()
                        if k not in ("offset", "length", "sha256")
                    },
                )
                if out.manifest["buffers"][key]["sha256"] != b["sha256"]:
                    raise GSVError(f"Source changed during copy: {key}")
                self._unchanged()
            self._unchanged()

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


open_scene = Scene
