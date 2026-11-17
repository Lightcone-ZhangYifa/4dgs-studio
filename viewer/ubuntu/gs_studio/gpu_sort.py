"""Optional OpenGL 4.3 stable depth radix sort; OpenGL 3.3 keeps CPU sorting.

The eight four-bit LSD passes follow the macOS histogram/prefix/scatter design.
Only source indices move; covariance and RGB stay in the resident data buffer.
Within a workgroup, shared bit masks count earlier equal digits with popcount,
avoiding dependency on subgroup/warp size or optional ballot extensions.
"""
from __future__ import annotations

import os
import ctypes
import numpy as np
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
from OpenGL import GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader


GROUP_SIZE = 256


def _compute_functions():
    """Resolve compute commands from the API owning the current context.

    GTK may create a GLX context for NVIDIA PRIME while PyOpenGL was imported
    with its default EGL platform. Basic GLVND entry points still work, but the
    EGL loader can leave compute commands unresolved. Querying the active GLX
    context and its own procedure table avoids forcing an environment setting
    that would break ordinary native Wayland/EGL windows.
    """
    try:
        library = ctypes.CDLL("libGLX.so.0")
        current = library.glXGetCurrentContext
        current.restype = ctypes.c_void_p
        current.argtypes = []
        if current():
            resolve = library.glXGetProcAddress
            resolve.restype = ctypes.c_void_p
            resolve.argtypes = [ctypes.c_char_p]
            dispatch = resolve(b"glDispatchCompute")
            barrier = resolve(b"glMemoryBarrier")
            if dispatch and barrier:
                return (ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)(dispatch),
                        ctypes.CFUNCTYPE(None, ctypes.c_uint)(barrier), library)
    except (OSError, AttributeError):
        pass
    if not gl.glDispatchCompute or not gl.glMemoryBarrier:
        raise RuntimeError("The active GL context does not expose compute entry points")
    return gl.glDispatchCompute, gl.glMemoryBarrier, None


def _forward(transform):
    value = np.asarray(transform, np.float32)
    if value.shape == (4, 4):
        value = value[2, :3]
    if value.shape != (3,) or not np.isfinite(value).all():
        raise ValueError("Depth sorting requires a finite camera forward vector or world-to-camera matrix")
    return np.ascontiguousarray(value)


def depth_order_cpu(means, world_to_camera, *, second_means=None, weight=0.):
    """Stable descending Float32 center-depth order shared by both backends.

    Camera translation contributes the same constant to every center. Omitting
    it avoids rounding nearby distinct world depths to an artificial tie when a
    camera is far from the origin. Exact ties, including signed zero, retain
    source order; NaNs sort last. Behind-camera points are sorted deterministically
    and clipped later by the same preview vertex shader as visible points.
    """
    points = np.asarray(means, np.float32)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("Depth sorting requires means[N,3]")
    forward = _forward(world_to_camera)
    with np.errstate(over="ignore", invalid="ignore"):
        if second_means is not None:
            second = np.asarray(second_means, np.float32)
            weights = np.asarray(weight, np.float32)
            if second.shape != points.shape or (weights.ndim != 0 and weights.shape != (len(points),)):
                raise ValueError("Prepared sorting requires matching endpoint means and scalar or per-row weights")
            if not np.isfinite(weights).all() or np.any((weights < 0) | (weights > 1)):
                raise ValueError("Prepared interpolation weights must be in [0,1]")
            if weights.ndim:
                weights = weights[:, None]
            points = points * (np.float32(1) - weights) + second * weights
        depth = ((points[:, 0] * forward[0] + points[:, 1] * forward[1])
                 + points[:, 2] * forward[2])
    return np.asarray(np.argsort(-depth, kind="stable"), np.uint32)


KEYS = r"""#version 430 core
layout(local_size_x=256) in;
layout(std430,binding=0) readonly buffer Geometry { vec4 geometry[]; };
layout(std430,binding=1) writeonly buffer Keys { uvec2 pairs[]; };
uniform uint count;
uniform vec3 forward;
void main() {
 uint i=gl_GlobalInvocationID.x;
 if(i>=count) return;
 vec3 p=geometry[4*i].xyz;
 precise float x=p.x*forward.x;
 precise float y=p.y*forward.y;
 precise float z=p.z*forward.z;
 precise float xy=x+y;
 precise float depth=xy+z;
 // Float bit order becomes numerical order after flipping the sign convention.
 // Canonicalize +/-0 so both backends preserve source order for that tie.
 uint bits=floatBitsToUint(depth==0.?0.:depth);
 uint ascending=(bits&0x80000000u)!=0u?~bits:(bits^0x80000000u);
 uint key=isnan(depth)?0xffffffffu:~ascending;
 pairs[i]=uvec2(key,i);
}
"""

# Native prepared endpoints use the same sixteen-float stride as viewport RGB
# records. Only their first three components are needed for sorting; covariance
# and quaternion interpolation remain in the preview shader.
PREPARED_KEYS = KEYS.replace(
    "uniform vec3 forward;",
    """uniform vec3 forward;
layout(std430,binding=6) readonly buffer Second { vec4 secondGeometry[]; };
layout(std430,binding=7) readonly buffer Weights { float weights[]; };
uniform uint useWeights;
uniform uint weightStride;
uniform float scalarWeight;""").replace(
    "vec3 p=geometry[4*i].xyz;",
    """float w=useWeights!=0u?weights[weightStride*i]:scalarWeight;
 precise float inverseWeight=1.-w;
 precise vec3 firstMean=geometry[4*i].xyz*inverseWeight;
 precise vec3 secondMean=secondGeometry[4*i].xyz*w;
 precise vec3 p=firstMean+secondMean;""")

HISTOGRAM = r"""#version 430 core
layout(local_size_x=256) in;
layout(std430,binding=1) readonly buffer Source { uvec2 source[]; };
layout(std430,binding=3) writeonly buffer Histogram { uint histogram[]; };
uniform uint count;
uniform uint shift;
shared uint bins[16];
void main() {
 uint lane=gl_LocalInvocationID.x, i=gl_GlobalInvocationID.x;
 if(lane<16u) bins[lane]=0u;
 barrier();
 if(i<count) atomicAdd(bins[(source[i].x>>shift)&15u],1u);
 barrier();
 if(lane<16u) histogram[gl_WorkGroupID.x*16u+lane]=bins[lane];
}
"""

PREFIX = r"""#version 430 core
layout(local_size_x=16) in;
layout(std430,binding=3) readonly buffer Histogram { uint histogram[]; };
layout(std430,binding=4) writeonly buffer Prefix { uint prefix[]; };
uniform uint groups;
shared uint totals[16];
void main() {
 uint bin=gl_LocalInvocationID.x, sum=0u;
 for(uint group=0u;group<groups;++group) {
  uint i=group*16u+bin;
  prefix[i]=sum;
  sum+=histogram[i];
 }
 totals[bin]=sum;
 barrier();
 uint base=0u;
 for(uint earlier=0u;earlier<bin;++earlier) base+=totals[earlier];
 prefix[groups*16u+bin]=base;
}
"""

SCATTER = r"""#version 430 core
layout(local_size_x=256) in;
layout(std430,binding=1) readonly buffer Source { uvec2 source[]; };
layout(std430,binding=2) writeonly buffer Destination { uvec2 destination[]; };
layout(std430,binding=4) readonly buffer Prefix { uint prefix[]; };
layout(std430,binding=5) writeonly buffer Order { uint order[]; };
uniform uint count;
uniform uint groups;
uniform uint shift;
// Eight words cover a 256-invocation group for each of the sixteen bins.
// These are ordinary shared atomic words, not hardware subgroup ballots.
shared uint masks[128];
void main() {
 uint lane=gl_LocalInvocationID.x, i=gl_GlobalInvocationID.x;
 uvec2 item=i<count?source[i]:uvec2(0xffffffffu,0u);
 uint digit=i<count?((item.x>>shift)&15u):16u;
 if(lane<128u) masks[lane]=0u;
 barrier();
 if(i<count) atomicOr(masks[digit*8u+(lane>>5u)],1u<<(lane&31u));
 barrier();
 if(i>=count) return;
 uint rank=0u;
 uint word=lane>>5u;
 for(uint earlier=0u;earlier<word;++earlier) rank+=uint(bitCount(masks[digit*8u+earlier]));
 rank+=uint(bitCount(masks[digit*8u+word]&((1u<<(lane&31u))-1u)));
 uint target=prefix[groups*16u+digit]+prefix[gl_WorkGroupID.x*16u+digit]+rank;
 destination[target]=item;
 if(shift==28u) order[target]=item.y;
}
"""


class GpuSorter:
    """Own compute scratch buffers; input geometry and output order stay caller-owned.

    Construct/initialize/sort/close under the same current GL context. force=True
    enables software drivers for correctness tests; ordinary software previews
    retain the faster NumPy implementation. Unsupported contexts allocate nothing.
    """
    def __init__(self, force=False):
        self.force = force
        self.supported = False
        self.reason = "GPU depth sort is not initialized"
        self.programs = []
        self.buffers = []
        self.capacity = self.group_capacity = 0
        self.max_groups = self.max_block_bytes = 0
        self.max_bindings = 0
        self.prepared_program = 0
        self._uniforms = {}
        self._dispatch_compute = self._memory_barrier = self._proc_library = None

    def initialize(self):
        self.close()
        major, minor = int(gl.glGetIntegerv(gl.GL_MAJOR_VERSION)), int(gl.glGetIntegerv(gl.GL_MINOR_VERSION))
        if (major, minor) < (4, 3):
            self.reason = "OpenGL 4.3 compute shaders are unavailable; using stable CPU depth sort"
            return False
        renderer = (gl.glGetString(gl.GL_RENDERER) or b"").decode(errors="replace")
        if not self.force and any(x in renderer.lower() for x in ("llvmpipe", "softpipe", "swrast", "swiftshader", "software")):
            self.reason = "Software OpenGL uses stable CPU depth sort"
            return False
        self.max_bindings = int(gl.glGetIntegerv(gl.GL_MAX_SHADER_STORAGE_BUFFER_BINDINGS))
        if self.max_bindings < 6:
            self.reason = "Insufficient shader-storage bindings; using stable CPU depth sort"
            return False
        if int(gl.glGetIntegerv(gl.GL_MAX_COMPUTE_WORK_GROUP_INVOCATIONS)) < GROUP_SIZE:
            self.reason = "Insufficient compute workgroup capacity; using stable CPU depth sort"
            return False
        try:
            self._dispatch_compute, self._memory_barrier, self._proc_library = _compute_functions()
            for source in (KEYS, HISTOGRAM, PREFIX, SCATTER):
                shader = compileShader(source, gl.GL_COMPUTE_SHADER)
                try:
                    self.programs.append(int(compileProgram(shader)))
                finally:
                    gl.glDeleteShader(shader)
            self.buffers = [int(x) for x in gl.glGenBuffers(4)]
            self.max_groups = int(np.asarray(gl.glGetIntegeri_v(gl.GL_MAX_COMPUTE_WORK_GROUP_COUNT, 0)).reshape(-1)[0])
            # PyOpenGL 3.1.7's convenience return type for this query is a
            # double on some distros. Use the specified GLint64 output ABI.
            maximum = ctypes.c_int64()
            gl.glGetInteger64v(gl.GL_MAX_SHADER_STORAGE_BLOCK_SIZE, ctypes.byref(maximum))
            self.max_block_bytes = maximum.value
            self.supported = True
            self.reason = "GPU stable Float32 depth radix sort"
            return True
        except Exception as exc:
            self.close()
            self.reason = "GPU depth-sort initialization failed; stable CPU fallback: " + str(exc)
            return False

    def _uniform(self, program, name, value):
        key = (program, name)
        if key not in self._uniforms:
            self._uniforms[key] = gl.glGetUniformLocation(program, name)
        gl.glUniform1ui(self._uniforms[key], int(value))

    def _reserve(self, count, groups):
        if count > self.capacity:
            capacity = min(self.max_block_bytes // 8, max(count, self.capacity * 2))
            for buffer in self.buffers[:2]:
                gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, buffer)
                gl.glBufferData(gl.GL_SHADER_STORAGE_BUFFER, capacity * 8, None, gl.GL_DYNAMIC_COPY)
            self.capacity = capacity
        if groups > self.group_capacity:
            capacity = max(groups, self.group_capacity * 2)
            for buffer, size in ((self.buffers[2], capacity*16*4), (self.buffers[3], (capacity+1)*16*4)):
                gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, buffer)
                gl.glBufferData(gl.GL_SHADER_STORAGE_BUFFER, size, None, gl.GL_DYNAMIC_COPY)
            self.group_capacity = capacity

    def sort(self, data_buffer, order_buffer, count, world_to_camera, *, second_buffer=None,
             weight_buffer=None, weight=0., weight_stride=1):
        if not self.supported:
            raise RuntimeError(self.reason)
        if not isinstance(count, (int, np.integer)) or count < 0:
            raise ValueError("Depth-sort count must be a nonnegative integer")
        if not count:
            return
        groups = (int(count) + GROUP_SIZE - 1) // GROUP_SIZE
        if groups > self.max_groups or count * 64 > self.max_block_bytes:
            raise RuntimeError("Scene exceeds this GPU's compute depth-sort capacity; use stable CPU sorting")
        prepared = second_buffer is not None
        if weight_buffer is not None and not prepared:
            raise ValueError("A weight buffer requires a second geometry endpoint")
        if prepared:
            if self.max_bindings < 8:
                raise RuntimeError("Prepared GPU sorting requires eight storage-buffer bindings")
            if not np.isfinite(weight) or not 0 <= weight <= 1:
                raise ValueError("Prepared interpolation weight must be in [0,1]")
            if not isinstance(weight_stride, (int, np.integer)) or weight_stride < 1:
                raise ValueError("Prepared weight stride must be a positive integer")
            if weight_buffer is not None and ((count-1)*weight_stride+1)*4 > self.max_block_bytes:
                raise RuntimeError("Prepared weight buffer exceeds the GPU storage-block limit")
        forward = _forward(world_to_camera)
        previous_program = int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        previous_buffer = int(gl.glGetIntegerv(gl.GL_SHADER_STORAGE_BUFFER_BINDING))
        bindings = [int(np.asarray(gl.glGetIntegeri_v(gl.GL_SHADER_STORAGE_BUFFER_BINDING, i)).reshape(-1)[0]) for i in range(8 if prepared else 6)]
        try:
            self._reserve(count, groups)
            a, b, histogram, prefix = self.buffers
            gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 0, data_buffer)
            gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 1, a)
            if prepared and not self.prepared_program:
                shader = compileShader(PREPARED_KEYS, gl.GL_COMPUTE_SHADER)
                try:
                    self.prepared_program = int(compileProgram(shader))
                finally:
                    gl.glDeleteShader(shader)
            program = self.prepared_program if prepared else self.programs[0]
            gl.glUseProgram(program)
            self._uniform(program, "count", count)
            gl.glUniform3fv(gl.glGetUniformLocation(program, "forward"), 1, forward)
            if prepared:
                gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 6, second_buffer)
                gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 7, weight_buffer or 0)
                self._uniform(program, "useWeights", weight_buffer is not None)
                self._uniform(program, "weightStride", weight_stride)
                gl.glUniform1f(gl.glGetUniformLocation(program, "scalarWeight"), float(np.float32(weight)))
            self._dispatch_compute(groups, 1, 1)
            self._memory_barrier(gl.GL_SHADER_STORAGE_BARRIER_BIT)
            gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 3, histogram)
            gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 4, prefix)
            gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 5, order_buffer)
            for shift in range(0, 32, 4):
                gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 1, a)
                gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 2, b)
                program = self.programs[1]
                gl.glUseProgram(program)
                self._uniform(program, "count", count)
                self._uniform(program, "shift", shift)
                self._dispatch_compute(groups, 1, 1)
                self._memory_barrier(gl.GL_SHADER_STORAGE_BARRIER_BIT)
                program = self.programs[2]
                gl.glUseProgram(program)
                self._uniform(program, "groups", groups)
                self._dispatch_compute(1, 1, 1)
                self._memory_barrier(gl.GL_SHADER_STORAGE_BARRIER_BIT)
                program = self.programs[3]
                gl.glUseProgram(program)
                for name, value in (("count", count), ("groups", groups), ("shift", shift)):
                    self._uniform(program, name, value)
                self._dispatch_compute(groups, 1, 1)
                self._memory_barrier(gl.GL_SHADER_STORAGE_BARRIER_BIT)
                a, b = b, a
            self._memory_barrier(gl.GL_VERTEX_ATTRIB_ARRAY_BARRIER_BIT | gl.GL_BUFFER_UPDATE_BARRIER_BIT | gl.GL_SHADER_STORAGE_BARRIER_BIT)
        finally:
            for i, buffer in enumerate(bindings):
                gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, i, buffer)
            gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, previous_buffer)
            gl.glUseProgram(previous_program)

    def close(self):
        if self.buffers:
            gl.glDeleteBuffers(len(self.buffers), self.buffers)
        for program in self.programs:
            gl.glDeleteProgram(program)
        if self.prepared_program:
            gl.glDeleteProgram(self.prepared_program)
            self.prepared_program = 0
        self.programs, self.buffers = [], []
        self.capacity = self.group_capacity = 0
        self._uniforms = {}
        self._dispatch_compute = self._memory_barrier = self._proc_library = None
        self.supported = False
