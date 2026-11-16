"""OpenGL 3.3 instanced EWA preview renderer.

Covariance projection runs in the vertex shader; NumPy performs one stable depth
sort on the CPU when geometry or the camera changes. No Python per-splat loop is
used. Quantitative camera rendering is deliberately separate from this preview.
"""
from __future__ import annotations

import ctypes
import time
import numpy as np
import os
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
from OpenGL import GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
from OpenGL.raw.GL.VERSION.GL_2_0 import glVertexAttribPointer as _vertex_pointer
from OpenGL.raw.GL.VERSION.GL_3_0 import glVertexAttribIPointer as _integer_pointer

from .gpu_sort import GpuSorter, depth_order_cpu

VERTEX = r"""#version 330 core
layout(location=0) in uint sourceIndex;
uniform samplerBuffer splatData;
uniform samplerBuffer secondData;
uniform samplerBuffer weightData;
uniform int preparedMode;
uniform int perRowWeights;
uniform int interpolationBoundary;
uniform float interpolation;
uniform usamplerBuffer selectionMask;
uniform mat4 worldToCamera;
uniform mat3 intrinsics;
uniform vec2 viewport;
uniform float pixelShift;
uniform float nearDepth;
uniform float exposure;
uniform float exposureMultiplier;
uniform float gaussianScale;
uniform float opacityScale;
out vec2 local;
out vec3 color;
out float opacity;
flat out float selected;
vec4 slerpQuaternion(vec4 a,vec4 b,float t) {
    a=normalize(a);b=normalize(b);
    float cosine=dot(a,b);
    if(cosine<0){b=-b;cosine=-cosine;}
    if(cosine>.9995)return normalize(mix(a,b,t));
    float angle=acos(clamp(cosine,-1.,1.));
    return (sin((1-t)*angle)*a+sin(t*angle)*b)/sin(angle);
}
mat3 quaternionRotation(vec4 q) {
    float w=q.x,x=q.y,y=q.z,z=q.w;
    return mat3(vec3(1-2*y*y-2*z*z,2*x*y+2*w*z,2*x*z-2*w*y),
                vec3(2*x*y-2*w*z,1-2*x*x-2*z*z,2*y*z+2*w*x),
                vec3(2*x*z+2*w*y,2*y*z-2*w*x,1-2*x*x-2*y*y));
}
void main() {
    int base=4*int(sourceIndex);
    vec4 meanOpacity=texelFetch(splatData,base);
    vec4 a=texelFetch(splatData,base+1);
    vec4 b=texelFetch(splatData,base+2);
    vec3 covA=a.xyz,covB=vec3(a.w,b.xy);
    vec4 colorSelected=vec4(texelFetch(splatData,base+3).rgb,float(texelFetch(selectionMask,int(sourceIndex)).r));
    const vec2 corners[6]=vec2[6](vec2(-3,-3),vec2(3,-3),vec2(-3,3),vec2(-3,3),vec2(3,-3),vec2(3,3));
    mat3 C=mat3(vec3(covA.x,covA.y,covA.z),vec3(covA.y,covB.x,covB.y),vec3(covA.z,covB.y,covB.z));
    bool valid=true;
    if(preparedMode!=0) {
        vec2 track=perRowWeights!=0?texelFetch(weightData,int(sourceIndex)).rg:vec2(interpolation,float(interpolationBoundary));
        float t=track.x;
        vec4 scaleA=a,scaleB=texelFetch(secondData,base+1);
        valid=track.y<.5?scaleA.w>.5:(track.y<1.5?scaleB.w>.5:(scaleA.w>.5&&scaleB.w>.5));
        meanOpacity=mix(meanOpacity,texelFetch(secondData,base),t);
        vec3 scale=mix(scaleA.xyz,scaleB.xyz,t);
        mat3 orientation=quaternionRotation(slerpQuaternion(b,texelFetch(secondData,base+2),t));
        mat3 basis=orientation*mat3(vec3(scale.x,0,0),vec3(0,scale.y,0),vec3(0,0,scale.z));
        C=basis*transpose(basis);
        colorSelected.rgb=mix(texelFetch(splatData,base+3).rgb,texelFetch(secondData,base+3).rgb,t);
    }
    vec3 c=(worldToCamera*vec4(meanOpacity.xyz,1)).xyz;
    mat3 R=mat3(worldToCamera);
    C=(R*C*transpose(R))*(gaussianScale*gaussianScale);
    float iz=1./max(c.z,nearDepth);
    float fx=intrinsics[0][0],fy=intrinsics[1][1],skew=intrinsics[1][0];
    vec2 center=vec2((fx*c.x+skew*c.y)*iz+intrinsics[2][0],fy*c.y*iz+intrinsics[2][1])+pixelShift;
    float yLo=-intrinsics[2][1]/fy,yHi=(viewport.y-intrinsics[2][1])/fy;
    float xLo=(-intrinsics[2][0]-max(skew*yLo,skew*yHi))/fx;
    float xHi=(viewport.x-intrinsics[2][0]-min(skew*yLo,skew*yHi))/fx;
    vec2 lo=vec2(xLo,yLo),hi=vec2(xHi,yHi);
    vec2 ray=clamp(c.xy*iz,lo-.15*(hi-lo),hi+.15*(hi-lo));
    vec3 jx=vec3(fx*iz,skew*iz,-(fx*ray.x+skew*ray.y)*iz);
    vec3 jy=vec3(0,fy*iz,-fy*ray.y*iz);
    float xx=dot(jx,C*jx)+.3,xy=dot(jx,C*jy),yy=dot(jy,C*jy)+.3;
    float halfTrace=.5*(xx+yy),delta=sqrt(max(.25*(xx-yy)*(xx-yy)+xy*xy,0));
    vec2 lambda=max(vec2(halfTrace+delta,halfTrace-delta),vec2(.01));
    float angle=(xy==0 && xx==yy)?0:.5*atan(2*xy,xx-yy);
    vec2 major=vec2(cos(angle),sin(angle))*sqrt(lambda.x);
    vec2 minor=vec2(-sin(angle),cos(angle))*sqrt(lambda.y);
    local=corners[gl_VertexID];
    vec2 p=center+major*local.x+minor*local.y;
    float depth=1.-nearDepth/max(c.z,nearDepth);
    gl_Position=vec4(2*p.x/viewport.x-1,1-2*p.y/viewport.y,2*depth-1,1);
    vec2 bound=3*sqrt(max(vec2(xx,yy),vec2(0)));
    float activatedOpacity=clamp(meanOpacity.w*opacityScale,0.,.999);
    if(!valid || c.z<=nearDepth || activatedOpacity<1./255. || any(isnan(p)) || any(isinf(p)) || center.x+bound.x<0 || center.y+bound.y<0 || center.x-bound.x>viewport.x || center.y-bound.y>viewport.y)
        gl_Position=vec4(4,4,4,1);
    color=colorSelected.rgb*exp2(exposure)*exposureMultiplier; opacity=activatedOpacity; selected=colorSelected.w;
}
"""
PICK_VERTEX = VERTEX.replace('flat out float selected;', 'flat out float selected;\nuniform vec2 pickPixel;\nflat out uvec2 pickCandidate;').replace('    color=colorSelected.rgb*exp2(exposure)*exposureMultiplier; opacity=activatedOpacity; selected=colorSelected.w;', '    vec2 click=pickPixel-center;\n    float determinant=xx*yy-xy*xy;\n    float r2=(yy*click.x*click.x-2*xy*click.x*click.y+xx*click.y*click.y)/max(determinant,1e-24);\n    bool hit=valid&&c.z>nearDepth&&r2<=9.&&!isnan(r2)&&!isinf(r2)&&activatedOpacity*exp(-.5*max(r2,0.))>=.1;\n    pickCandidate=uvec2(hit?floatBitsToUint(c.z):0x7f800000u,sourceIndex);\n    color=colorSelected.rgb*exp2(exposure)*exposureMultiplier; opacity=activatedOpacity; selected=colorSelected.w;')

FRAGMENT = r"""#version 330 core
in vec2 local;
in vec3 color;
in float opacity;
flat in float selected;
uniform int depthOnly;
out vec4 outputColor;
void main() {
    float r2=dot(local,local);
    if(r2>9) discard;
    float alpha=min(.999,opacity*exp(-.5*r2));
    if(alpha<(depthOnly!=0?.1:1./255.)) discard;
    vec3 rgb=color;
    if(selected>.5 && r2>5.5 && depthOnly==0) rgb=mix(rgb,vec3(1,.8,.12),.85);
    outputColor=vec4(rgb*alpha,alpha);
}
"""
LINE_VERTEX = r"""#version 330 core
layout(location=0) in vec3 pixelDepth;
layout(location=1) in vec4 color;
layout(location=2) in float sourceDepth;
uniform vec2 viewport;
out vec4 lineColor;
out float cameraDepth;
void main() {
 gl_Position=vec4((2*pixelDepth.x/viewport.x-1)*sourceDepth,(1-2*pixelDepth.y/viewport.y)*sourceDepth,(2*pixelDepth.z-1)*sourceDepth,sourceDepth);
 lineColor=color; cameraDepth=sourceDepth;
}
"""
LINE_FRAGMENT = r"""#version 330 core
in vec4 lineColor;
uniform float lineExposureGain;
out vec4 outputColor;
void main(){outputColor=vec4(lineColor.rgb*lineExposureGain*lineColor.a,lineColor.a);}
"""

PATH_FRAGMENT = r"""#version 330 core
in vec4 lineColor;
in float cameraDepth;
uniform int accumulationPass;
uniform float exposureGain;
out vec4 outputColor;
void main() {
 float alpha=clamp(lineColor.a,0.,1.);
 float weight=clamp(1./(1.+.03*cameraDepth*cameraDepth),.01,1.);
 if(accumulationPass==0)
  outputColor=vec4(clamp(lineColor.rgb*exposureGain,0.,1.)*alpha*weight,alpha*weight);
 else outputColor=vec4(alpha);
}
"""
COMPOSITE_VERTEX = r"""#version 330 core
void main() {
 const vec2 vertices[3]=vec2[3](vec2(-1,-1),vec2(3,-1),vec2(-1,3));
 gl_Position=vec4(vertices[gl_VertexID],0,1);
}
"""
COMPOSITE_FRAGMENT = r"""#version 330 core
uniform sampler2D accumulation;
uniform sampler2D revealage;
out vec4 outputColor;
void main() {
 ivec2 pixel=ivec2(gl_FragCoord.xy);
 vec4 value=texelFetch(accumulation,pixel,0);
 float alpha=1.-clamp(texelFetch(revealage,pixel,0).r,0.,1.);
 if(value.a<1e-7 || alpha<1e-6) outputColor=vec4(0);
 else outputColor=vec4(value.rgb/value.a*alpha,alpha);
}
"""


def projected_ellipses(frame, k, c2w, pixel_shift=.0, near=.001, width=None, height=None, gaussian_scale=1.):
    """Vectorized CPU mirror for click picking and geometry contract tests."""
    rotation = c2w[:3, :3].T
    camera = (np.asarray(frame.means) - c2w[:3, 3]) @ rotation.T
    z = camera[:, 2]
    invz = 1 / np.maximum(z, near)
    projected = camera @ k.T
    centers = projected[:, :2] * invz[:, None] + pixel_shift
    cov = np.einsum('ij,njk,lk->nil', rotation, frame.covariance, rotation, optimize=True) * (gaussian_scale * gaussian_scale)
    # Match the preview shader's derivative-only expanded image bounds.
    width = 2 * k[0, 2] if width is None else width
    height = 2 * k[1, 2] if height is None else height
    fx, fy, skew = k[0, 0], k[1, 1], k[0, 1]
    yl, yh = -k[1, 2] / fy, (height - k[1, 2]) / fy
    xl = (-k[0, 2] - max(skew * yl, skew * yh)) / fx
    xh = (width - k[0, 2] - min(skew * yl, skew * yh)) / fx
    lo, hi = np.array([xl, yl]), np.array([xh, yh])
    ray = np.clip(camera[:, :2] * invz[:, None], lo - .15 * (hi - lo), hi + .15 * (hi - lo))
    j = np.zeros((len(z), 2, 3), dtype=np.float64)
    j[:, 0, 0] = fx * invz
    j[:, 0, 1] = skew * invz
    j[:, 0, 2] = -(fx * ray[:, 0] + skew * ray[:, 1]) * invz
    j[:, 1, 1] = fy * invz
    j[:, 1, 2] = -fy * ray[:, 1] * invz
    c2 = np.einsum('nij,njk,nlk->nil', j, cov, j, optimize=True)
    c2[:, 0, 0] += .3
    c2[:, 1, 1] += .3
    return centers, c2, z


class Renderer:
    def __init__(self, sort_mode='auto'):
        if sort_mode not in ('auto', 'cpu', 'gpu'):
            raise ValueError('Sort mode must be auto, cpu or gpu')
        self.sort_mode = sort_mode
        self.gpu_sorter = GpuSorter()
        self.sort_backend = 'cpu'
        self.sort_reason = 'Stable CPU depth sort'
        self.last_sort_submission_ms = 0.
        self.pick_program = self.pick_buffer = self.pick_capacity = 0
        self.program = self.line_program = self.path_program = self.composite_program = 0
        self.vao = self.buffer = self.line_vao = self.line_buffer = 0
        self.order_buffer = self.mask_buffer = self.data_texture = self.mask_texture = 0
        self.order_capacity = self.mask_capacity = 0
        self._frame = None
        self._geometry_key = None
        self._selected = None
        self.second_buffer = self.second_texture = self.weight_buffer = self.weight_texture = 0
        self.second_capacity = self.weight_capacity = 0
        self.prepared_mode = self.per_row_weights = False
        self.interpolation = 0.
        self.interpolation_boundary = 0
        self._sort_frame = self._sort_forward = None
        self.sort_count = 0
        self.last_sort_reused = False
        self.last_sort_cpu_ms = 0.
        self.fbo = self.texture = self.depth = 0
        self.oit_fbo = self.accumulation_texture = self.revealage_texture = 0
        self.exposure_gain = 1.
        self.near_depth = .001
        self.size = (0, 0)
        self.count = 0
        self._uniforms = {}
        self.capacity = 0
        self.line_capacity = 0

    def initialize(self):
        try:
            self.program = compileProgram(compileShader(VERTEX, gl.GL_VERTEX_SHADER), compileShader(FRAGMENT, gl.GL_FRAGMENT_SHADER))
            self.line_program = compileProgram(compileShader(LINE_VERTEX, gl.GL_VERTEX_SHADER), compileShader(LINE_FRAGMENT, gl.GL_FRAGMENT_SHADER))
            self.path_program = compileProgram(compileShader(LINE_VERTEX, gl.GL_VERTEX_SHADER), compileShader(PATH_FRAGMENT, gl.GL_FRAGMENT_SHADER))
            self.composite_program = compileProgram(compileShader(COMPOSITE_VERTEX, gl.GL_VERTEX_SHADER), compileShader(COMPOSITE_FRAGMENT, gl.GL_FRAGMENT_SHADER))
            # GTK may use EGL on Wayland and GLX on Xwayland. VBO offsets
            # need no client-array retention in PyOpenGL context-specific caches.
            self.vao = int(gl.glGenVertexArrays(1))
            self.buffer = int(gl.glGenBuffers(1))
            self.order_buffer = int(gl.glGenBuffers(1))
            self.mask_buffer = int(gl.glGenBuffers(1))
            self.second_buffer = int(gl.glGenBuffers(1))
            self.weight_buffer = int(gl.glGenBuffers(1))
            self.second_texture = int(gl.glGenTextures(1))
            self.weight_texture = int(gl.glGenTextures(1))
            self.data_texture = int(gl.glGenTextures(1))
            self.mask_texture = int(gl.glGenTextures(1))
            shader = compileShader(PICK_VERTEX, gl.GL_VERTEX_SHADER)
            self.pick_program = int(gl.glCreateProgram())
            try:
                gl.glAttachShader(self.pick_program, shader)
                names = (ctypes.c_char_p * 1)(b'pickCandidate')
                gl.glTransformFeedbackVaryings(self.pick_program, 1, ctypes.cast(names, ctypes.POINTER(ctypes.POINTER(ctypes.c_char))), gl.GL_INTERLEAVED_ATTRIBS)
                gl.glLinkProgram(self.pick_program)
                if not gl.glGetProgramiv(self.pick_program, gl.GL_LINK_STATUS):
                    raise RuntimeError(gl.glGetProgramInfoLog(self.pick_program).decode(errors='replace'))
            finally:
                gl.glDeleteShader(shader)
            self.pick_buffer = int(gl.glGenBuffers(1))
            self.max_texels = int(gl.glGetIntegerv(gl.GL_MAX_TEXTURE_BUFFER_SIZE))
            gl.glBindVertexArray(self.vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.order_buffer)
            gl.glEnableVertexAttribArray(0)
            _integer_pointer(0, 1, gl.GL_UNSIGNED_INT, 4, ctypes.c_void_p(0))
            gl.glVertexAttribDivisor(0, 1)
            self.line_vao = int(gl.glGenVertexArrays(1))
            self.line_buffer = int(gl.glGenBuffers(1))
            gl.glBindVertexArray(self.line_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.line_buffer)
            for index, count, offset in [(0, 3, 0), (1, 4, 3), (2, 1, 7)]:
                gl.glEnableVertexAttribArray(index)
                _vertex_pointer(index, count, gl.GL_FLOAT, False, 32, ctypes.c_void_p(offset * 4))
            gl.glBindVertexArray(0)
            self.gpu_sorter.initialize()
            self.sort_reason = self.gpu_sorter.reason
        except Exception:
            self.close()
            raise

    def uniform(self, program, name):
        key = (program, name)
        if key not in self._uniforms:
            self._uniforms[key] = gl.glGetUniformLocation(program, name)
        return self._uniforms[key]

    def _upload_texture_buffer(self, buffer, texture, data, capacity_name, fmt):
        data = np.ascontiguousarray(data, np.float32)
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, buffer)
        capacity = getattr(self, capacity_name)
        if data.nbytes > capacity:
            capacity = max(data.nbytes, capacity * 2)
            gl.glBufferData(gl.GL_TEXTURE_BUFFER, capacity, None, gl.GL_DYNAMIC_DRAW)
            setattr(self, capacity_name, capacity)
        gl.glBufferSubData(gl.GL_TEXTURE_BUFFER, 0, data.nbytes, data)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, texture)
        gl.glTexBuffer(gl.GL_TEXTURE_BUFFER, fmt, buffer)

    def upload(self, frame, c2w, selected):
        n = len(frame.ids) if frame is not None else 0
        if not n:
            self.count = 0
            self._frame = self._geometry_key = self._selected = None
            self._sort_frame = self._sort_forward = None
            self.last_sort_reused = False
            return
        if n * 4 > self.max_texels:
            raise RuntimeError(f"Scene exceeds this GPU's texture-buffer limit ({self.max_texels // 4:,} splats)")
        self.prepared_mode = bool(getattr(frame, 'is_prepared', False))
        geometry_key = ('endpoints', frame.endpoint_key) if self.prepared_mode else ('frame', id(frame))
        geometry_changed = geometry_key != self._geometry_key
        if geometry_changed:
            if self.prepared_mode:
                data = frame.first
                self._upload_texture_buffer(self.second_buffer, self.second_texture, frame.second, 'second_capacity', gl.GL_RGBA32F)
            else:
                data = np.empty((n, 16), np.float32)
                data[:, :3] = frame.means
                data[:, 3] = frame.opacity
                data[:, 4:7] = frame.covariance[:, 0, :]
                data[:, 7] = frame.covariance[:, 1, 1]
                data[:, 8] = frame.covariance[:, 1, 2]
                data[:, 9] = frame.covariance[:, 2, 2]
                data[:, 10:12] = 0
                data[:, 12:15] = frame.colors
                data[:, 15] = 0
            self._upload_texture_buffer(self.buffer, self.data_texture, data, 'capacity', gl.GL_RGBA32F)
            self._geometry_key = geometry_key
        self._frame = frame
        self.per_row_weights = self.prepared_mode and frame.weights is not None
        if self.prepared_mode:
            if self.per_row_weights:
                weights = np.empty((n, 2), np.float32)
                weights[:, 0] = frame.weights
                # Preserve exact Float64 endpoint/interior validity policy even
                # when an interior weight rounds to 0 or 1 in a GPU uniform.
                for _, start, stop, weight in frame.segments:
                    weights[start:stop, 1] = 0 if weight == 0 else 1 if weight == 1 else 2
                self._upload_texture_buffer(self.weight_buffer, self.weight_texture, weights, 'weight_capacity', gl.GL_RG32F)
                self.interpolation = 0.
                input_key = (geometry_key, id(frame.weights))
            else:
                self.interpolation = float(frame.weight)
                self.interpolation_boundary = 0 if frame.weight == 0 else 1 if frame.weight == 1 else 2
                input_key = (geometry_key, self.interpolation)
        else:
            input_key = geometry_key
        if geometry_changed or self._selected != selected:
            flags = np.zeros(n, np.uint8)
            for object_id, identity in selected:
                flags[(frame.object_ids == object_id) if identity is None else ((frame.object_ids == object_id) & (frame.ids == identity))] = 1
            gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, self.mask_buffer)
            if n > self.mask_capacity:
                self.mask_capacity = min(self.max_texels, max(n, self.mask_capacity * 2))
                gl.glBufferData(gl.GL_TEXTURE_BUFFER, self.mask_capacity, None, gl.GL_DYNAMIC_DRAW)
            gl.glBufferSubData(gl.GL_TEXTURE_BUFFER, 0, n, flags)
            gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.mask_texture)
            gl.glTexBuffer(gl.GL_TEXTURE_BUFFER, gl.GL_R8UI, self.mask_buffer)
            self._selected = selected.copy()
        self.count = n
        forward = np.asarray(c2w[:3, 2])
        self.last_sort_reused = input_key == self._sort_frame and np.array_equal(forward, self._sort_forward)
        if self.last_sort_reused:
            self.last_sort_cpu_ms = self.last_sort_submission_ms = 0.
            return
        started = time.perf_counter()
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.order_buffer)
        if n * 4 > self.order_capacity:
            self.order_capacity = max(n * 4, self.order_capacity * 2)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, self.order_capacity, None, gl.GL_DYNAMIC_DRAW)
        gpu = self.gpu_sorter.supported and (self.sort_mode == 'gpu' or (self.sort_mode == 'auto' and n >= 16384))
        self.sort_backend = 'cpu'
        if gpu:
            try:
                kwargs = {}
                if self.prepared_mode:
                    kwargs = dict(second_buffer=self.second_buffer, weight_buffer=self.weight_buffer if self.per_row_weights else None, weight=self.interpolation, weight_stride=2)
                self.gpu_sorter.sort(self.buffer, self.order_buffer, n, forward, **kwargs)
                self.sort_backend = 'gpu-radix'
                self.sort_reason = self.gpu_sorter.reason
            except Exception as error:
                self.sort_reason = 'GPU depth sort unavailable; using stable CPU sort: ' + str(error)
                self.gpu_sorter.close()
                self.gpu_sorter.reason = self.sort_reason
        if self.sort_backend == 'cpu':
            if self.prepared_mode:
                weight = np.float32(frame.weight) if frame.weights is None else frame.weights[:, None]
                # GL 3.3 evaluates only positions for sorting, never materializes
                # rotations/covariances or removes identity/validity rows on GTK.
                means = frame.first[:, :3] * (1 - weight) + frame.second[:, :3] * weight
            else:
                means = frame.means
            order = depth_order_cpu(means, forward)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.order_buffer)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, order.nbytes, order)
            if not gpu:
                self.sort_reason = self.gpu_sorter.reason if not self.gpu_sorter.supported else 'Stable CPU depth sort for small scenes or explicit benchmark override'
        self._sort_frame, self._sort_forward = input_key, forward.copy()
        self.sort_count += 1
        self.last_sort_submission_ms = (time.perf_counter() - started) * 1000
        self.last_sort_cpu_ms = self.last_sort_submission_ms if self.sort_backend == 'cpu' else 0.

    def target(self, width, height):
        if self.size == (width, height):
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
            return
        self._close_target()
        self.fbo = int(gl.glGenFramebuffers(1))
        self.texture = int(gl.glGenTextures(1))
        self.depth = int(gl.glGenRenderbuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, self.texture, 0)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.depth)
        gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH_COMPONENT24, width, height)
        gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT, gl.GL_RENDERBUFFER, self.depth)
        if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("OpenGL preview framebuffer allocation failed")
        self.size = (width, height)

    def begin(self, k, c2w, width, height, pixel_shift=0., exposure=0., *, exposure_multiplier=1., gaussian_scale=1., opacity_scale=1., near_depth=.001, background=(.018, .025, .04)):
        self.exposure_gain = float(2. ** exposure) * exposure_multiplier
        self.near_depth = near_depth
        gl.glViewport(0, 0, width, height)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_FRAMEBUFFER_SRGB)
        gl.glColorMask(True, True, True, True)
        gl.glDepthMask(True)
        gl.glClearColor(*background, 1.)
        gl.glClearDepth(1.)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendEquation(gl.GL_FUNC_ADD)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_DEPTH_TEST)
        self._splat_uniforms(self.program, k, c2w, width, height, pixel_shift=pixel_shift, exposure=exposure, exposure_multiplier=exposure_multiplier, gaussian_scale=gaussian_scale, opacity_scale=opacity_scale, near_depth=near_depth)

    def _splat_uniforms(self, program, k, c2w, width, height, *, pixel_shift=0., exposure=0., exposure_multiplier=1., gaussian_scale=1., opacity_scale=1., near_depth=.001):
        gl.glUseProgram(program)
        gl.glUniform1i(self.uniform(program, 'splatData'), 0)
        gl.glUniform1i(self.uniform(program, 'selectionMask'), 1)
        gl.glUniform1i(self.uniform(program, 'secondData'), 2)
        gl.glUniform1i(self.uniform(program, 'weightData'), 3)
        gl.glUniform1i(self.uniform(program, 'preparedMode'), int(self.prepared_mode))
        gl.glUniform1i(self.uniform(program, 'perRowWeights'), int(self.per_row_weights))
        gl.glUniform1i(self.uniform(program, 'interpolationBoundary'), self.interpolation_boundary)
        gl.glUniform1f(self.uniform(program, 'interpolation'), self.interpolation)
        view = np.eye(4, dtype=np.float32)
        view[:3, :3] = c2w[:3, :3].T
        view[:3, 3] = -view[:3, :3] @ c2w[:3, 3]
        gl.glUniformMatrix4fv(self.uniform(program, 'worldToCamera'), 1, False, np.ascontiguousarray(view.T))
        gl.glUniformMatrix3fv(self.uniform(program, 'intrinsics'), 1, False, np.ascontiguousarray(k.T, np.float32))
        gl.glUniform2f(self.uniform(program, 'viewport'), width, height)
        gl.glUniform1f(self.uniform(program, 'pixelShift'), pixel_shift)
        gl.glUniform1f(self.uniform(program, 'nearDepth'), near_depth)
        gl.glUniform1f(self.uniform(program, 'exposure'), exposure)
        gl.glUniform1f(self.uniform(program, 'exposureMultiplier'), exposure_multiplier)
        gl.glUniform1f(self.uniform(program, 'gaussianScale'), gaussian_scale)
        gl.glUniform1f(self.uniform(program, 'opacityScale'), opacity_scale)

    def splats(self, depth_only=False):
        if not self.count:
            return
        gl.glUseProgram(self.program)
        gl.glBindVertexArray(self.vao)
        self._bind_splat_textures()
        gl.glUniform1i(self.uniform(self.program, 'depthOnly'), int(depth_only))
        if depth_only:
            gl.glColorMask(False, False, False, False)
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthFunc(gl.GL_LESS)
            gl.glDepthMask(True)
        else:
            gl.glDepthMask(False)
        gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 6, self.count)
        gl.glColorMask(True, True, True, True)
        gl.glDepthMask(False)

    def _bind_splat_textures(self):
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.data_texture)
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.mask_texture)
        gl.glActiveTexture(gl.GL_TEXTURE2)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.second_texture if self.prepared_mode else self.data_texture)
        gl.glActiveTexture(gl.GL_TEXTURE3)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.weight_texture if self.per_row_weights else self.data_texture)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def pick(self, k, c2w, width, height, pixel, *, gaussian_scale=1., opacity_scale=1., near_depth=.001):
        """OpenGL 3.3 exact center-depth/identity pick without CPU covariance.

        Transform feedback projects one vertex per primitive using the visible
        footprint shader. Only (Float32 depth bits, source index) return to the
        CPU; nearest depth and source-order ties match the Metal picker. No
        normalized depth attachment can collapse nearby distinct center depths.
        """
        if not self.count:
            return None
        previous_program = int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        previous_vao = int(gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING))
        previous_generic = int(gl.glGetIntegerv(gl.GL_TRANSFORM_FEEDBACK_BUFFER_BINDING))
        previous_indexed = int(np.asarray(gl.glGetIntegeri_v(gl.GL_TRANSFORM_FEEDBACK_BUFFER_BINDING, 0)).reshape(-1)[0])
        previous_active = int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE))
        discarded = bool(gl.glIsEnabled(gl.GL_RASTERIZER_DISCARD))
        try:
            gl.glBindBuffer(gl.GL_TRANSFORM_FEEDBACK_BUFFER, self.pick_buffer)
            if self.count * 8 > self.pick_capacity:
                self.pick_capacity = max(self.count * 8, self.pick_capacity * 2)
                gl.glBufferData(gl.GL_TRANSFORM_FEEDBACK_BUFFER, self.pick_capacity, None, gl.GL_STREAM_READ)
            gl.glBindBufferBase(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, self.pick_buffer)
            self._splat_uniforms(self.pick_program, k, c2w, width, height, gaussian_scale=gaussian_scale, opacity_scale=opacity_scale, near_depth=near_depth)
            gl.glUniform2f(self.uniform(self.pick_program, 'pickPixel'), *pixel)
            self._bind_splat_textures()
            gl.glBindVertexArray(self.vao)
            gl.glEnable(gl.GL_RASTERIZER_DISCARD)
            gl.glBeginTransformFeedback(gl.GL_POINTS)
            gl.glDrawArraysInstanced(gl.GL_POINTS, 0, 1, self.count)
            gl.glEndTransformFeedback()
            data = gl.glGetBufferSubData(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, self.count * 8)
            values = np.frombuffer(data, np.uint32).reshape(-1, 2)
            depth = values[:, 0].min()
            if depth == 0x7f800000:
                return None
            return int(values[values[:, 0] == depth, 1].min())
        finally:
            if not discarded:
                gl.glDisable(gl.GL_RASTERIZER_DISCARD)
            gl.glBindBufferBase(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, previous_indexed)
            gl.glBindBuffer(gl.GL_TRANSFORM_FEEDBACK_BUFFER, previous_generic)
            gl.glBindVertexArray(previous_vao)
            gl.glUseProgram(previous_program)
            gl.glActiveTexture(previous_active)

    def lines(self, vertices, width, height, occlude=True, *, depth_write=False, program=None, exposure_gain=1.):
        if vertices is None or len(vertices) == 0:
            return
        data = np.ascontiguousarray(vertices, np.float32)
        if data.shape[1] == 7:
            # Fixed-size screen annotations have no meaningful perspective depth.
            data = np.column_stack([data, np.ones(len(data), np.float32)])
        program = self.line_program if program is None else program
        gl.glUseProgram(program)
        gl.glUniform2f(self.uniform(program, 'viewport'), width, height)
        if program == self.line_program:
            gl.glUniform1f(self.uniform(program, 'lineExposureGain'), exposure_gain)
        gl.glBindVertexArray(self.line_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.line_buffer)
        if data.nbytes > self.line_capacity:
            self.line_capacity = max(data.nbytes, self.line_capacity * 2)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, self.line_capacity, None, gl.GL_DYNAMIC_DRAW)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data.nbytes, data)
        if occlude:
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthFunc(gl.GL_LEQUAL)
        else:
            gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(depth_write)
        gl.glDrawArrays(gl.GL_LINES, 0, len(data))
        gl.glDepthMask(False)

    def _oit_target(self, width, height):
        if not self.oit_fbo:
            self.oit_fbo = int(gl.glGenFramebuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.oit_fbo)
            for name, attachment, internal, fmt in [
                ('accumulation_texture', gl.GL_COLOR_ATTACHMENT0, gl.GL_RGBA32F, gl.GL_RGBA),
                ('revealage_texture', gl.GL_COLOR_ATTACHMENT1, gl.GL_R16F, gl.GL_RED),
            ]:
                texture = int(gl.glGenTextures(1))
                setattr(self, name, texture)
                gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, width, height, 0, fmt, gl.GL_FLOAT, None)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, attachment, gl.GL_TEXTURE_2D, texture, 0)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT, gl.GL_RENDERBUFFER, self.depth)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('OpenGL path transparency allocation failed')
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.oit_fbo)

    def paths(self, vertices, width, height):
        """macOS weighted blended OIT, implemented in two OpenGL 3.3 passes.

        Sharing the main depth attachment preserves Gaussian-center occlusion and
        opaque trail depth. Two passes avoid requiring per-target blend functions
        introduced after the baseline OpenGL 3.3 API.
        """
        if vertices is None or len(vertices) == 0:
            return
        pairs = np.asarray(vertices).reshape(-1, 2, vertices.shape[1])
        opaque = np.min(pairs[:, :, 6], axis=1) >= .999
        if np.any(opaque):
            self.lines(pairs[opaque].reshape(-1, pairs.shape[2]), width, height, depth_write=True, exposure_gain=self.exposure_gain)
        transparent = pairs[~opaque].reshape(-1, pairs.shape[2])
        if not len(transparent):
            return
        self._oit_target(width, height)
        gl.glDepthMask(False)
        gl.glEnable(gl.GL_BLEND)
        gl.glUseProgram(self.path_program)
        gl.glUniform1f(self.uniform(self.path_program, 'exposureGain'), self.exposure_gain)
        # Accumulate color*alpha*depth-weight and alpha*depth-weight.
        gl.glDrawBuffer(gl.GL_COLOR_ATTACHMENT0)
        gl.glClearBufferfv(gl.GL_COLOR, 0, np.zeros(4, np.float32))
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        gl.glUniform1i(self.uniform(self.path_program, 'accumulationPass'), 0)
        self.lines(transparent, width, height, program=self.path_program)
        # Revealage is the product of (1-alpha), independent of segment order.
        gl.glDrawBuffer(gl.GL_COLOR_ATTACHMENT1)
        gl.glClearBufferfv(gl.GL_COLOR, 0, np.ones(4, np.float32))
        gl.glBlendFunc(gl.GL_ZERO, gl.GL_ONE_MINUS_SRC_COLOR)
        gl.glUniform1i(self.uniform(self.path_program, 'accumulationPass'), 1)
        self.lines(transparent, width, height, program=self.path_program)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glBlendFunc(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUseProgram(self.composite_program)
        gl.glUniform1i(self.uniform(self.composite_program, 'accumulation'), 0)
        gl.glUniform1i(self.uniform(self.composite_program, 'revealage'), 1)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.accumulation_texture)
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.revealage_texture)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindVertexArray(self.line_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)

    def finish(self, destination, width, height):
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, destination)
        gl.glBlitFramebuffer(0, 0, *self.size, 0, 0, width, height, gl.GL_COLOR_BUFFER_BIT, gl.GL_LINEAR)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, destination)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(True)
        gl.glViewport(0, 0, width, height)

    def _close_target(self):
        if self.oit_fbo:
            gl.glDeleteFramebuffers(1, [self.oit_fbo])
        for texture in (self.accumulation_texture, self.revealage_texture):
            if texture:
                gl.glDeleteTextures([texture])
        self.oit_fbo = self.accumulation_texture = self.revealage_texture = 0
        if self.fbo:
            gl.glDeleteFramebuffers(1, [self.fbo])
        if self.texture:
            gl.glDeleteTextures([self.texture])
        if self.depth:
            gl.glDeleteRenderbuffers(1, [self.depth])
        self.fbo = self.texture = self.depth = 0
        self.size = (0, 0)

    def close(self):
        self.gpu_sorter.close()
        self._close_target()
        for value in (self.buffer, self.line_buffer, self.order_buffer, self.mask_buffer, self.second_buffer, self.weight_buffer, self.pick_buffer):
            if value:
                gl.glDeleteBuffers(1, [value])
        for value in (self.data_texture, self.mask_texture, self.second_texture, self.weight_texture):
            if value:
                gl.glDeleteTextures([value])
        for value in (self.vao, self.line_vao):
            if value:
                gl.glDeleteVertexArrays(1, [value])
        for value in (self.program, self.line_program, self.path_program, self.composite_program, self.pick_program):
            if value:
                gl.glDeleteProgram(value)
        self.pick_program = self.pick_buffer = self.pick_capacity = 0
        self.path_program = self.composite_program = 0
        self.program = self.line_program = self.vao = self.buffer = self.line_vao = self.line_buffer = 0
        self.capacity = self.line_capacity = self.count = 0
        self.order_buffer = self.mask_buffer = self.data_texture = self.mask_texture = 0
        self.order_capacity = self.mask_capacity = 0
        self._frame = self._geometry_key = None
        self._selected = None
        self.second_buffer = self.second_texture = self.weight_buffer = self.weight_texture = 0
        self.second_capacity = self.weight_capacity = 0
        self._sort_frame = self._sort_forward = None
        self._uniforms.clear()
