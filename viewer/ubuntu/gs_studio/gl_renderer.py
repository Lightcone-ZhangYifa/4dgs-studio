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
from .gpu_visibility import GpuVisibility

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
flat out int frustumVisible;
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
    frustumVisible=int(!(!valid || c.z<=nearDepth || activatedOpacity<1./255. || any(isnan(p)) || any(isinf(p)) || any(isnan(bound)) || any(isinf(bound)) || center.x+bound.x<0 || center.y+bound.y<0 || center.x-bound.x>viewport.x || center.y-bound.y>viewport.y));
    if(frustumVisible==0)
        gl_Position=vec4(4,4,4,1);
    color=colorSelected.rgb*exp2(exposure)*exposureMultiplier; opacity=activatedOpacity; selected=colorSelected.w;
}
"""
PICK_VERTEX = VERTEX.replace('flat out float selected;', 'flat out float selected;\nuniform vec2 pickPixel;\nflat out uvec2 pickCandidate;').replace('    color=colorSelected.rgb*exp2(exposure)*exposureMultiplier; opacity=activatedOpacity; selected=colorSelected.w;', '    vec2 click=pickPixel-center;\n    float determinant=xx*yy-xy*xy;\n    float r2=(yy*click.x*click.x-2*xy*click.x*click.y+xx*click.y*click.y)/max(determinant,1e-24);\n    bool hit=valid&&c.z>nearDepth&&r2<=9.&&!isnan(r2)&&!isinf(r2)&&activatedOpacity*exp(-.5*max(r2,0.))>=.1;\n    pickCandidate=uvec2(hit?floatBitsToUint(c.z):0x7f800000u,sourceIndex);\n    color=colorSelected.rgb*exp2(exposure)*exposureMultiplier; opacity=activatedOpacity; selected=colorSelected.w;')

MARKER_VERTEX = VERTEX.replace('    local=corners[gl_VertexID];', '    local=corners[gl_VertexID]/3.;').replace(
    '    vec2 p=center+major*local.x+minor*local.y;',
    '    float radius=max(6.,min(80.,3.*max(length(major),length(minor))+2.));\n    vec2 p=center+local*radius;').replace(
    '2*depth-1,1);','2*max(0.,depth-1e-7)-1,1);')
MARKER_FRAGMENT = r"""#version 330 core
in vec2 local;
out vec4 outputColor;
void main(){
 float radius=length(local);
 if(radius<.84 || radius>1.)discard;
 outputColor=radius>.96?vec4(.09,.05,0,1):vec4(1,.78,.08,1);
}
"""

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


class GpuMetrics:
    """Bounded asynchronous timer queries; never read an unavailable result."""
    def __init__(self):
        self.supported=False
        self.reason='GPU timer queries are not initialized'
        self.pending=[]
        self.active=None
        self.free={'elapsed':[],'timestamp':[]}
        self.objects=set()
        self.maximum_queries=48
        self.completed_frame_count=0
        self.timing_samples_skipped=0
        self.last_render_ms=None
        self.last_sort_ms=None
        self.last_gpu_ms=None
        self.timestamps=[]
        self.last_result_at=None
        self._sort_by_frame={}
        self._library=None

    def initialize(self):
        try:
            if int(gl.glGetQueryiv(gl.GL_TIME_ELAPSED,gl.GL_QUERY_COUNTER_BITS))<=0:
                raise RuntimeError('Elapsed GPU timer queries are unavailable')
            self._read_u64=gl.glGetQueryObjectui64v
            self._timestamp=gl.glQueryCounter
            try:
                library=ctypes.CDLL('libGLX.so.0')
                library.glXGetCurrentContext.restype=ctypes.c_void_p
                library.glXGetCurrentContext.argtypes=[]
                if library.glXGetCurrentContext():
                    resolve=library.glXGetProcAddress
                    resolve.restype=ctypes.c_void_p;resolve.argtypes=[ctypes.c_char_p]
                    read=resolve(b'glGetQueryObjectui64v');stamp=resolve(b'glQueryCounter')
                    if not read or not stamp:raise RuntimeError('Active GLX context has no timer entry points')
                    self._read_u64=ctypes.CFUNCTYPE(None,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64))(read)
                    self._timestamp=ctypes.CFUNCTYPE(None,ctypes.c_uint,ctypes.c_uint)(stamp)
                    self._library=library
            except OSError:
                pass
            if not self._read_u64 or not self._timestamp:raise RuntimeError('GPU timestamp entry points are unavailable')
            self.supported=True;self.reason='Nonblocking GL_TIME_ELAPSED and GL_TIMESTAMP queries'
        except Exception as error:
            self.supported=False;self.reason=str(error)

    def _take(self,kind='elapsed'):
        if self.free[kind]:return self.free[kind].pop()
        if len(self.objects)>=self.maximum_queries:return None
        query=int(np.asarray(gl.glGenQueries(1)).reshape(-1)[0]);self.objects.add(query)
        return query

    def begin(self,stage,sequence):
        if not self.supported or self.active is not None:return None
        try:
            # Query targets are context state. Never nest inside a profiler or
            # another renderer's active elapsed query.
            if int(gl.glGetQueryiv(gl.GL_TIME_ELAPSED,gl.GL_CURRENT_QUERY)):
                self.timing_samples_skipped+=1;return None
            self.poll()
            query=self._take('elapsed')
            stamp=self._take('timestamp') if stage=='render' and query is not None else None
            if query is None or (stage=='render' and stamp is None):
                if query is not None:self.free['elapsed'].append(query)
                self.timing_samples_skipped+=1;return None
            token={'query':query,'timestamp':stamp,'stage':stage,'sequence':sequence,'aborted':False}
            gl.glBeginQuery(gl.GL_TIME_ELAPSED,query)
            self.active=token
            return token
        except Exception as error:
            self.supported=False;self.reason='GPU timing unavailable; rendering continues: '+str(error)
            return None

    def end(self,token,*,aborted=False):
        if token is None or self.active is not token:return
        try:
            gl.glEndQuery(gl.GL_TIME_ELAPSED)
            token['aborted']=aborted
            if token['timestamp'] is not None:self._timestamp(token['timestamp'],gl.GL_TIMESTAMP)
            self.pending.append(token)
        except Exception as error:
            self.supported=False;self.reason='GPU timing unavailable; rendering continues: '+str(error)
        finally:self.active=None

    def abort(self):
        if self.active is not None:self.end(self.active,aborted=True)

    def _value(self,query):
        value=ctypes.c_uint64()
        self._read_u64(query,gl.GL_QUERY_RESULT,ctypes.byref(value))
        return int(value.value)

    def poll(self):
        try:return self._poll_results()
        except Exception as error:
            self.supported=False;self.reason='GPU timing unavailable; rendering continues: '+str(error)
            return False

    def _poll_results(self):
        if not self.supported:return False
        changed=False
        remaining=[]
        for index,token in enumerate(self.pending):
            ids=[token['query']]+([token['timestamp']] if token['timestamp'] is not None else [])
            if not all(bool(gl.glGetQueryObjectiv(query,gl.GL_QUERY_RESULT_AVAILABLE)) for query in ids):
                remaining.extend(self.pending[index:]);break
            elapsed=self._value(token['query'])/1_000_000
            stamp=self._value(token['timestamp']) if token['timestamp'] is not None else None
            self.free['elapsed'].append(token['query'])
            if token['timestamp'] is not None:self.free['timestamp'].append(token['timestamp'])
            if token['aborted']:continue
            if token['stage']=='sort':
                self._sort_by_frame[token['sequence']]=elapsed
                continue
            self.completed_frame_count+=1
            self.last_render_ms=elapsed
            self.last_sort_ms=self._sort_by_frame.pop(token['sequence'],0.)
            self.last_gpu_ms=elapsed+self.last_sort_ms
            self.timestamps.append(stamp)
            self.timestamps=[t for t in self.timestamps if stamp-t<=1_000_000_000]
            self.last_result_at=time.monotonic()
            changed=True
        self.pending=remaining
        if len(self._sort_by_frame)>64:
            newest=max(self._sort_by_frame)
            self._sort_by_frame={k:v for k,v in self._sort_by_frame.items() if newest-k<64}
        return changed

    def summary(self):
        active=self.last_result_at is not None and time.monotonic()-self.last_result_at<.5
        fps=0.
        if active and len(self.timestamps)>1:
            span=self.timestamps[-1]-self.timestamps[0]
            if span>0:fps=(len(self.timestamps)-1)*1_000_000_000/span
        return {'gpu_ms':self.last_gpu_ms if self.supported else None,'gpu_render_ms':self.last_render_ms if self.supported else None,'gpu_sort_ms':self.last_sort_ms if self.supported else None,
                'completed_frame_count':self.completed_frame_count,'gpu_completed_fps':fps,
                'gpu_timing_available':self.supported,'gpu_timing_reason':self.reason,
                'timing_samples_skipped':self.timing_samples_skipped}

    def close(self):
        self.abort()
        if self.objects:gl.glDeleteQueries(len(self.objects),list(self.objects))
        self.pending=[];self.free={'elapsed':[],'timestamp':[]};self.objects=set();self.active=None
        self.supported=False


class RenderAllocationError(RuntimeError):
    """A recoverable target allocation failure; completed textures stay valid."""


class Renderer:
    def __init__(self, sort_mode='auto', *, reuse_endpoints=True):
        if sort_mode not in ('auto', 'cpu', 'gpu'):
            raise ValueError('Sort mode must be auto, cpu or gpu')
        self.gpu_metrics=GpuMetrics()
        self.gpu_visibility=GpuVisibility()
        self._visibility_arguments=None
        self._visibility_layout=None
        self.source_count=0
        self._frame_sequence=0
        self._render_query=None
        self._frame_open=False
        self.submitted_frame_count=0
        self.sort_mode = sort_mode
        self.reuse_endpoints = bool(reuse_endpoints)
        self._first_source = self._second_source = None
        self.last_endpoint_uploads = self.last_endpoint_upload_bytes = 0
        self.last_endpoint_swapped = False
        self.gpu_sorter = GpuSorter()
        self.sort_backend = 'cpu'
        self.sort_reason = 'Stable CPU depth sort'
        self.last_sort_submission_ms = 0.
        self.pick_program = self.pick_buffer = self.pick_capacity = 0
        self.pick_surface_fbo = self.pick_surface_texture = 0
        self.pick_surface_size=(0,0)
        self.marker_program=self.marker_buffer=self.marker_vao=self.marker_capacity=self.marker_count=0
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
            self.marker_program=compileProgram(compileShader(MARKER_VERTEX,gl.GL_VERTEX_SHADER),compileShader(MARKER_FRAGMENT,gl.GL_FRAGMENT_SHADER))
            self.marker_buffer=int(gl.glGenBuffers(1));self.marker_vao=int(gl.glGenVertexArrays(1))
            gl.glBindVertexArray(self.marker_vao);gl.glBindBuffer(gl.GL_ARRAY_BUFFER,self.marker_buffer)
            gl.glEnableVertexAttribArray(0);_integer_pointer(0,1,gl.GL_UNSIGNED_INT,4,ctypes.c_void_p(0));gl.glVertexAttribDivisor(0,1)
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
            self.gpu_metrics.initialize()
            self.gpu_visibility.initialize(VERTEX)
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
        self.last_endpoint_uploads = self.last_endpoint_upload_bytes = 0
        self.last_endpoint_swapped = False
        n = len(frame.ids) if frame is not None else 0
        declared=getattr(frame,'source_count',None)
        self.source_count=n if declared is None else max(n,int(declared))
        layout=getattr(frame,'source_layout',None)
        if layout is None:
            segments=getattr(frame,'segments',None)
            if getattr(frame,'is_prepared',False) and segments is not None:
                layout=(frame.endpoint_key[:2],tuple((owner,start,stop) for owner,start,stop,_ in segments))
            else:
                # Foreign callers without source metadata use conservative
                # array identity; SDK Frames carry a stable manifest layout.
                layout=(id(frame.ids),id(frame.object_ids)) if frame is not None else None
        if layout != self._visibility_layout:
            self.gpu_visibility.reset()
            self._visibility_layout=layout
        if not n:
            self.gpu_visibility.mark_empty()
            self.count = 0
            self._frame = self._geometry_key = self._selected = None
            self._first_source = self._second_source = None
            self._sort_frame = self._sort_forward = None
            self.last_sort_reused = False
            self.marker_count=0
            return
        if self.gpu_visibility.empty:
            self.gpu_visibility.reset()
        if n * 4 > self.max_texels:
            raise RuntimeError(f"Scene exceeds this GPU's texture-buffer limit ({self.max_texels // 4:,} splats)")
        self.prepared_mode = bool(getattr(frame, 'is_prepared', False))
        geometry_key = ('endpoints', frame.endpoint_key) if self.prepared_mode else ('frame', id(frame))
        geometry_changed = geometry_key != self._geometry_key
        if geometry_changed:
            if self.prepared_mode:
                # Source arrays are immutable. Forward/backward interval changes
                # often share exactly one endpoint; rotate its GPU ownership
                # instead of copying that 64-byte record per primitive again.
                reuse = self.reuse_endpoints and not frame.first.flags.writeable and not frame.second.flags.writeable
                if reuse and (frame.first is self._second_source or frame.second is self._first_source):
                    self.buffer, self.second_buffer = self.second_buffer, self.buffer
                    self.data_texture, self.second_texture = self.second_texture, self.data_texture
                    self.capacity, self.second_capacity = self.second_capacity, self.capacity
                    self._first_source, self._second_source = self._second_source, self._first_source
                    self.last_endpoint_swapped = True
                if not reuse or frame.first is not self._first_source:
                    self._upload_texture_buffer(self.buffer, self.data_texture, frame.first, 'capacity', gl.GL_RGBA32F)
                    self.last_endpoint_uploads += 1
                    self.last_endpoint_upload_bytes += frame.first.nbytes
                if not reuse or frame.second is not self._second_source:
                    self._upload_texture_buffer(self.second_buffer, self.second_texture, frame.second, 'second_capacity', gl.GL_RGBA32F)
                    self.last_endpoint_uploads += 1
                    self.last_endpoint_upload_bytes += frame.second.nbytes
                self._first_source, self._second_source = frame.first, frame.second
            else:
                self._first_source = self._second_source = None
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
            if not self.prepared_mode:
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
                if identity is None:continue
                flags[(frame.object_ids == object_id) if identity is None else ((frame.object_ids == object_id) & (frame.ids == identity))] = 1
            gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, self.mask_buffer)
            if n > self.mask_capacity:
                self.mask_capacity = min(self.max_texels, max(n, self.mask_capacity * 2))
                gl.glBufferData(gl.GL_TEXTURE_BUFFER, self.mask_capacity, None, gl.GL_DYNAMIC_DRAW)
            gl.glBufferSubData(gl.GL_TEXTURE_BUFFER, 0, n, flags)
            gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.mask_texture)
            gl.glTexBuffer(gl.GL_TEXTURE_BUFFER, gl.GL_R8UI, self.mask_buffer)
            markers=np.asarray(np.flatnonzero(flags),np.uint32)
            self.marker_count=len(markers)
            if self.marker_count:
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER,self.marker_buffer)
                if markers.nbytes>self.marker_capacity:
                    self.marker_capacity=max(markers.nbytes,self.marker_capacity*2)
                    gl.glBufferData(gl.GL_ARRAY_BUFFER,self.marker_capacity,None,gl.GL_DYNAMIC_DRAW)
                gl.glBufferSubData(gl.GL_ARRAY_BUFFER,0,markers.nbytes,markers)
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
            timer=self.gpu_metrics.begin('sort',self._frame_sequence+1)
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
            finally:
                self.gpu_metrics.end(timer)
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

    @staticmethod
    def _delete_targets(fbos=(),textures=(),depths=()):
        for value in fbos:
            if value:gl.glDeleteFramebuffers(1,[value])
        for value in textures:
            if value:gl.glDeleteTextures([value])
        for value in depths:
            if value:gl.glDeleteRenderbuffers(1,[value])

    def _new_transparency_target(self,width,height,depth):
        fbo=accumulation=revealage=0
        try:
            fbo=int(gl.glGenFramebuffers(1));gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,fbo)
            for attachment,internal,fmt in [(gl.GL_COLOR_ATTACHMENT0,gl.GL_RGBA32F,gl.GL_RGBA),(gl.GL_COLOR_ATTACHMENT1,gl.GL_R16F,gl.GL_RED)]:
                texture=int(gl.glGenTextures(1))
                if attachment==gl.GL_COLOR_ATTACHMENT0:accumulation=texture
                else:revealage=texture
                gl.glBindTexture(gl.GL_TEXTURE_2D,texture)
                gl.glTexImage2D(gl.GL_TEXTURE_2D,0,internal,width,height,0,fmt,gl.GL_FLOAT,None)
                gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MIN_FILTER,gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MAG_FILTER,gl.GL_NEAREST)
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER,attachment,gl.GL_TEXTURE_2D,texture,0)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER,gl.GL_DEPTH_ATTACHMENT,gl.GL_RENDERBUFFER,depth)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)!=gl.GL_FRAMEBUFFER_COMPLETE:raise RuntimeError('Incomplete transparency framebuffer')
            return fbo,accumulation,revealage
        except Exception as error:
            self._delete_targets((fbo,),(accumulation,revealage))
            raise RenderAllocationError('Cannot allocate preview transparency: '+str(error)) from error

    def target(self,width,height,*,transparency=False):
        """Allocate a complete candidate before retiring the completed preview."""
        previous_draw=int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        previous_read=int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        if self.size==(width,height):
            try:
                if transparency and not self.oit_fbo:
                    self.oit_fbo,self.accumulation_texture,self.revealage_texture=self._new_transparency_target(width,height,self.depth)
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.fbo)
                return
            except Exception:
                gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER,previous_draw);gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,previous_read)
                raise
        fbo=texture=depth=oit=accumulation=revealage=0
        try:
            if width<1 or height<1:raise ValueError('Preview dimensions must be positive')
            fbo=int(gl.glGenFramebuffers(1));texture=int(gl.glGenTextures(1));depth=int(gl.glGenRenderbuffers(1))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,fbo);gl.glBindTexture(gl.GL_TEXTURE_2D,texture)
            gl.glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_RGBA8,width,height,0,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE,None)
            gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MIN_FILTER,gl.GL_LINEAR);gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MAG_FILTER,gl.GL_LINEAR)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER,gl.GL_COLOR_ATTACHMENT0,gl.GL_TEXTURE_2D,texture,0)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER,depth);gl.glRenderbufferStorage(gl.GL_RENDERBUFFER,gl.GL_DEPTH_COMPONENT24,width,height)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER,gl.GL_DEPTH_ATTACHMENT,gl.GL_RENDERBUFFER,depth)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)!=gl.GL_FRAMEBUFFER_COMPLETE:raise RuntimeError('Incomplete preview framebuffer')
            if transparency:oit,accumulation,revealage=self._new_transparency_target(width,height,depth)
        except Exception as error:
            self._delete_targets((fbo,oit),(texture,accumulation,revealage),(depth,))
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER,previous_draw);gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,previous_read)
            if isinstance(error,RenderAllocationError):raise
            raise RenderAllocationError('Cannot allocate preview framebuffer: '+str(error)) from error
        old_fbos=(self.fbo,self.oit_fbo);old_textures=(self.texture,self.accumulation_texture,self.revealage_texture);old_depths=(self.depth,)
        self.fbo,self.texture,self.depth=fbo,texture,depth
        self.oit_fbo,self.accumulation_texture,self.revealage_texture=oit,accumulation,revealage
        self.size=(width,height)
        self._delete_targets(old_fbos,old_textures,old_depths)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.fbo)

    def begin(self, k, c2w, width, height, pixel_shift=0., exposure=0., *, exposure_multiplier=1., gaussian_scale=1., opacity_scale=1., near_depth=.001, background=(.018, .025, .04)):
        self.gpu_metrics.abort()
        self._frame_sequence+=1
        self._render_query=self.gpu_metrics.begin('render',self._frame_sequence)
        self._frame_open=True
        self._visibility_arguments=(k.copy(),c2w.copy(),width,height,
            dict(pixel_shift=pixel_shift,gaussian_scale=gaussian_scale,opacity_scale=opacity_scale,near_depth=near_depth))
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
        if not depth_only and self._visibility_arguments is not None:
            k,pose,width,height,controls=self._visibility_arguments
            def count_visible(program):
                previous_program=int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
                previous_vao=int(gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING))
                discarded=bool(gl.glIsEnabled(gl.GL_RASTERIZER_DISCARD))
                try:
                    self._splat_uniforms(program,k,pose,width,height,**controls)
                    self._bind_splat_textures()
                    gl.glBindVertexArray(self.vao)
                    gl.glEnable(gl.GL_RASTERIZER_DISCARD)
                    gl.glDrawArraysInstanced(gl.GL_POINTS,0,1,self.count)
                finally:
                    if not discarded:gl.glDisable(gl.GL_RASTERIZER_DISCARD)
                    gl.glUseProgram(previous_program)
                    gl.glBindVertexArray(previous_vao)
            self.gpu_visibility.submit(count_visible,self._frame_sequence,
                {'source_time':getattr(self._frame,'time',None),'pose':pose.tolist()})
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

    def pick(self, k, c2w, width, height, pixel, *, gaussian_scale=1., opacity_scale=1., near_depth=.001, return_depth=False):
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
            index=int(values[values[:,0]==depth,1].min())
            return (index,float(np.asarray(depth,np.uint32).view(np.float32))) if return_depth else index
        finally:
            if not discarded:
                gl.glDisable(gl.GL_RASTERIZER_DISCARD)
            gl.glBindBufferBase(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, previous_indexed)
            gl.glBindBuffer(gl.GL_TRANSFORM_FEEDBACK_BUFFER, previous_generic)
            gl.glBindVertexArray(previous_vao)
            gl.glUseProgram(previous_program)
            gl.glActiveTexture(previous_active)

    def camera_occlusion_depths(self,k,c2w,width,height,pixels,*,gaussian_scale=1.,opacity_scale=1.,near_depth=.001):
        """Gaussian-only display surface, sampled at visible frustum edge pixels.

        Camera picking precedes trail/marker overlays in the Metal renderer.
        Rebuild that depth surface here instead of reading annotation depth from
        the completed preview. No projected primitive data returns to the CPU.
        """
        if not len(pixels):return np.empty(0,np.float32)
        if not self.count:return np.ones(len(pixels),np.float32)
        draw=int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING));read=int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        viewport=np.array(gl.glGetIntegerv(gl.GL_VIEWPORT))
        program=int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM));vao=int(gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING))
        color=np.array(gl.glGetBooleanv(gl.GL_COLOR_WRITEMASK));depth_mask=bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
        enabled=bool(gl.glIsEnabled(gl.GL_DEPTH_TEST));depth_func=int(gl.glGetIntegerv(gl.GL_DEPTH_FUNC))
        try:
            if self.pick_surface_size!=(width,height):
                self._close_pick_surface()
                self.pick_surface_fbo=int(gl.glGenFramebuffers(1));self.pick_surface_texture=int(gl.glGenTextures(1))
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.pick_surface_fbo)
                gl.glBindTexture(gl.GL_TEXTURE_2D,self.pick_surface_texture)
                gl.glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_DEPTH_COMPONENT32F,width,height,0,gl.GL_DEPTH_COMPONENT,gl.GL_FLOAT,None)
                gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MIN_FILTER,gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MAG_FILTER,gl.GL_NEAREST)
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER,gl.GL_DEPTH_ATTACHMENT,gl.GL_TEXTURE_2D,self.pick_surface_texture,0)
                gl.glDrawBuffer(gl.GL_NONE);gl.glReadBuffer(gl.GL_NONE)
                if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)!=gl.GL_FRAMEBUFFER_COMPLETE:raise RuntimeError('Camera pick depth allocation failed')
                self.pick_surface_size=(width,height)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.pick_surface_fbo)
            gl.glViewport(0,0,width,height);gl.glDepthMask(True);gl.glClearDepth(1.)
            gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
            self._splat_uniforms(self.program,k,c2w,width,height,gaussian_scale=gaussian_scale,opacity_scale=opacity_scale,near_depth=near_depth)
            self.splats(depth_only=True)
            result=[]
            for x,y in pixels:
                sample=gl.glReadPixels(int(x),height-1-int(y),1,1,gl.GL_DEPTH_COMPONENT,gl.GL_FLOAT)
                result.append(float((np.frombuffer(sample,np.float32) if isinstance(sample,(bytes,bytearray)) else np.asarray(sample)).reshape(-1)[0]))
            return np.asarray(result,np.float32)
        finally:
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER,draw);gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,read)
            gl.glViewport(*viewport);gl.glColorMask(*color);gl.glDepthMask(depth_mask);gl.glDepthFunc(depth_func)
            (gl.glEnable if enabled else gl.glDisable)(gl.GL_DEPTH_TEST)
            gl.glUseProgram(program);gl.glBindVertexArray(vao)

    def _close_pick_surface(self):
        if self.pick_surface_fbo:gl.glDeleteFramebuffers(1,[self.pick_surface_fbo])
        if self.pick_surface_texture:gl.glDeleteTextures([self.pick_surface_texture])
        self.pick_surface_fbo=self.pick_surface_texture=0;self.pick_surface_size=(0,0)

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

    def _oit_target(self,width,height):
        if not self.oit_fbo:
            previous_draw=int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING));previous_read=int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
            try:self.oit_fbo,self.accumulation_texture,self.revealage_texture=self._new_transparency_target(width,height,self.depth)
            except Exception:
                gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER,previous_draw);gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,previous_read)
                raise
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.oit_fbo)

    def paths(self, vertices, width, height):
        """macOS weighted blended OIT, implemented in two OpenGL 3.3 passes.

        Sharing the main depth attachment preserves Gaussian-center occlusion and
        opaque trail depth. Two passes avoid requiring per-target blend functions
        introduced after the baseline OpenGL 3.3 API.
        """
        if vertices is None or len(vertices) == 0:
            return
        pairs = np.asarray(vertices).reshape(-1, 2, vertices.shape[1])
        pairs=pairs[np.max(pairs[:,:,6],axis=1)>0]
        if not len(pairs):return
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

    def selection_markers(self,k,c2w,width,height,*,gaussian_scale=1.,opacity_scale=1.,near_depth=.001):
        if not self.marker_count:return
        self._splat_uniforms(self.marker_program,k,c2w,width,height,gaussian_scale=gaussian_scale,opacity_scale=opacity_scale,near_depth=near_depth)
        self._bind_splat_textures();gl.glBindVertexArray(self.marker_vao)
        gl.glEnable(gl.GL_DEPTH_TEST);gl.glDepthFunc(gl.GL_LEQUAL);gl.glDepthMask(False)
        gl.glDrawArraysInstanced(gl.GL_TRIANGLES,0,6,self.marker_count)

    def finish(self, destination, width, height):
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, destination)
        gl.glBlitFramebuffer(0, 0, *self.size, 0, 0, width, height, gl.GL_COLOR_BUFFER_BIT, gl.GL_LINEAR)
        if self._render_query is not None:
            self.gpu_metrics.end(self._render_query);self._render_query=None
        if self._frame_open:
            self.submitted_frame_count+=1;self._frame_open=False
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, destination)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(True)
        gl.glViewport(0, 0, width, height)

    def performance_metrics(self):
        metrics=self.gpu_metrics.summary()
        metrics.update(self.gpu_visibility.summary())
        buffers=self.capacity+self.second_capacity+self.weight_capacity+self.mask_capacity+self.order_capacity+self.line_capacity+self.pick_capacity+self.marker_capacity
        scratch=self.gpu_sorter.capacity*16+(self.gpu_sorter.group_capacity*2+1)*16*4 if self.gpu_sorter.buffers else 0
        pixels=self.size[0]*self.size[1]
        textures=pixels*7 if self.fbo else 0
        if self.oit_fbo:textures+=pixels*18
        if self.pick_surface_fbo:textures+=self.pick_surface_size[0]*self.pick_surface_size[1]*4
        metrics.update({'submitted_frame_count':self.submitted_frame_count,'resident_bytes':buffers+scratch+textures,
                        'resident_bytes_estimated':True,'source_count':self.source_count,
                        'active_count':getattr(self._frame,'valid_count',self.count) if self._frame is not None else 0})
        return metrics

    @property
    def metrics_pending(self):
        return bool(self.gpu_metrics.pending or (self.gpu_visibility.supported and self.gpu_visibility.pending))

    def poll_metrics(self):
        timing=self.gpu_metrics.poll()
        visibility=self.gpu_visibility.poll()
        return timing or visibility

    def reset_visibility(self):
        self.gpu_visibility.reset()
        self._visibility_layout=None

    def abort_frame(self):
        self.gpu_metrics.abort();self._render_query=None;self._frame_open=False

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
        self.gpu_metrics.close()
        self.gpu_visibility.close()
        self._close_pick_surface()
        self.gpu_sorter.close()
        self._close_target()
        for value in (self.buffer, self.line_buffer, self.order_buffer, self.mask_buffer, self.second_buffer, self.weight_buffer, self.pick_buffer, self.marker_buffer):
            if value:
                gl.glDeleteBuffers(1, [value])
        for value in (self.data_texture, self.mask_texture, self.second_texture, self.weight_texture):
            if value:
                gl.glDeleteTextures([value])
        for value in (self.vao, self.line_vao, self.marker_vao):
            if value:
                gl.glDeleteVertexArrays(1, [value])
        for value in (self.program, self.line_program, self.path_program, self.composite_program, self.pick_program, self.marker_program):
            if value:
                gl.glDeleteProgram(value)
        self.marker_program=self.marker_buffer=self.marker_vao=self.marker_capacity=self.marker_count=0
        self.pick_program = self.pick_buffer = self.pick_capacity = 0
        self.path_program = self.composite_program = 0
        self.program = self.line_program = self.vao = self.buffer = self.line_vao = self.line_buffer = 0
        self.capacity = self.line_capacity = self.count = 0
        self.order_buffer = self.mask_buffer = self.data_texture = self.mask_texture = 0
        self.order_capacity = self.mask_capacity = 0
        self._frame = self._geometry_key = None
        self._first_source = self._second_source = None
        self._selected = None
        self.second_buffer = self.second_texture = self.weight_buffer = self.weight_texture = 0
        self.second_capacity = self.weight_capacity = 0
        self._sort_frame = self._sort_forward = None
        self._uniforms.clear()
