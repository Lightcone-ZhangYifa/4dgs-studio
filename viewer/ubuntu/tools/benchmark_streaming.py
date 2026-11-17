#!/usr/bin/env python3
"""Measure SDK endpoint preparation and GPU interval switches separately.

Uses an EGL device pbuffer rather than a desktop window. The synthetic GSV and
all renderer objects are temporary. Results identify the actual graphics driver;
completed timings include glFinish and exclude source preparation unless stated.
"""
from __future__ import annotations
import argparse
import ctypes as C
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'packages/gsv/src'))
import numpy as np
from gsv import Writer, add_persistent_gaussians
from gs_studio.model import SceneDocument
from gs_studio.gl_renderer import Renderer
from OpenGL import GL as gl


class DeviceContext:
    """A temporary EGL_EXT_platform_device OpenGL context, without a compositor."""
    def __init__(self,width,height,device_index=0):
        self.display=self.context=self.surface=None
        self.egl=C.CDLL('libEGL.so.1')
        def bind(name,result,args):
            fn=getattr(self.egl,name);fn.restype=result;fn.argtypes=args;return fn
        self.getproc=bind('eglGetProcAddress',C.c_void_p,[C.c_char_p])
        def proc(name,result,*args):
            address=self.getproc(name.encode())
            if not address:raise RuntimeError(f'{name} is unavailable')
            return C.CFUNCTYPE(result,*args)(address)
        query=proc('eglQueryDevicesEXT',C.c_uint,C.c_int,C.POINTER(C.c_void_p),C.POINTER(C.c_int))
        platform=proc('eglGetPlatformDisplayEXT',C.c_void_p,C.c_uint,C.c_void_p,C.POINTER(C.c_int))
        self.initialize=bind('eglInitialize',C.c_uint,[C.c_void_p,C.POINTER(C.c_int),C.POINTER(C.c_int)])
        choose=bind('eglChooseConfig',C.c_uint,[C.c_void_p,C.POINTER(C.c_int),C.POINTER(C.c_void_p),C.c_int,C.POINTER(C.c_int)])
        api=bind('eglBindAPI',C.c_uint,[C.c_uint])
        create_context=bind('eglCreateContext',C.c_void_p,[C.c_void_p,C.c_void_p,C.c_void_p,C.POINTER(C.c_int)])
        create_surface=bind('eglCreatePbufferSurface',C.c_void_p,[C.c_void_p,C.c_void_p,C.POINTER(C.c_int)])
        self.current=bind('eglMakeCurrent',C.c_uint,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p])
        self.destroy_context=bind('eglDestroyContext',C.c_uint,[C.c_void_p,C.c_void_p])
        self.destroy_surface=bind('eglDestroySurface',C.c_uint,[C.c_void_p,C.c_void_p])
        self.terminate=bind('eglTerminate',C.c_uint,[C.c_void_p])
        get_error=bind('eglGetError',C.c_uint,[])
        def check(ok,operation):
            if not ok:raise RuntimeError(f'{operation} failed (EGL 0x{get_error():04x})')
        devices=(C.c_void_p*32)();count=C.c_int()
        check(query(32,devices,C.byref(count)),'Device enumeration')
        if not 0<=device_index<count.value:raise ValueError(f'Device index must be in [0,{count.value})')
        try:
            self.display=platform(0x313F,devices[device_index],None)
            major,minor=C.c_int(),C.c_int()
            check(self.initialize(self.display,C.byref(major),C.byref(minor)),'Device initialization')
            attrs=(C.c_int*15)(0x3033,1,0x3040,8,0x3024,8,0x3023,8,0x3022,8,0x3021,8,0x3025,24,0x3038)
            config=C.c_void_p();found=C.c_int()
            check(choose(self.display,attrs,C.byref(config),1,C.byref(found)) and found.value,'OpenGL pbuffer configuration')
            check(api(0x30A2),'OpenGL API binding')
            ctx_attrs=(C.c_int*7)(0x3098,4,0x30FB,3,0x30FD,1,0x3038)
            self.context=create_context(self.display,config,None,ctx_attrs)
            check(self.context,'OpenGL 4.3 context creation')
            pbuffer_attrs=(C.c_int*5)(0x3057,width,0x3056,height,0x3038)
            self.surface=create_surface(self.display,config,pbuffer_attrs)
            check(self.surface,'Pbuffer creation')
            check(self.current(self.display,self.surface,self.surface,self.context),'Making context current')
        except BaseException:
            self.close();raise

    def close(self):
        if self.display:
            self.current(self.display,None,None,None)
            if self.surface:self.destroy_surface(self.display,self.surface)
            if self.context:self.destroy_context(self.display,self.context)
            self.terminate(self.display)
        self.display=self.context=self.surface=None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=1_000_000)
    parser.add_argument('--intervals',type=int,default=4)
    parser.add_argument('--width',type=int,default=1280)
    parser.add_argument('--height',type=int,default=720)
    parser.add_argument('--device',type=int,default=0)
    args=parser.parse_args()
    if min(args.count,args.intervals,args.width,args.height)<1:parser.error('Counts and dimensions must be positive')
    context=DeviceContext(args.width,args.height,args.device)
    output={'vendor':gl.glGetString(gl.GL_VENDOR).decode(),'renderer':gl.glGetString(gl.GL_RENDERER).decode(),
            'version':gl.glGetString(gl.GL_VERSION).decode(),'context':'EGL device pbuffer',
            'resolution':[args.width,args.height],'count':args.count}
    try:
        with tempfile.TemporaryDirectory(prefix='gsv-stream-benchmark-') as temp:
            path=Path(temp)/'stream.gsv'
            rng=np.random.default_rng(743)
            base=np.zeros((args.count,16),np.float32)
            base[:,:3]=rng.uniform([-1,-1,2],[1,1,4],(args.count,3));base[:,3]=.7
            base[:,4:7]=[.01,.014,.01];base[:,7:9]=1
            base[:,12:15]=rng.uniform(.1,1,(args.count,3));base[:,15]=-1
            def records():
                for index in range(args.intervals+1):
                    record=base.copy();record[:,0]+=index*.04
                    record[:,8]=np.cos(index*.05);record[:,11]=np.sin(index*.05)
                    yield record
            with Writer(path) as out:
                add_persistent_gaussians(out,'field',np.arange(args.count,dtype=np.int64),np.arange(args.intervals+1,dtype=np.float64),records())
            started=time.perf_counter();doc=SceneDocument(path)
            output['source_open_validation_ms']=(time.perf_counter()-started)*1000
            with doc:
                states=[];preparation=[]
                for index in range(args.intervals):
                    started=time.perf_counter();states.append(doc.prepare_frame(index+.25))
                    preparation.append((time.perf_counter()-started)*1000)
                output['cold_prepare_ms']=preparation
                output['shared_endpoint_identity']=[states[i].first is states[i-1].second for i in range(1,len(states))]
                k=np.array([[args.height,0,args.width/2],[0,args.height,args.height/2],[0,0,1.]],np.float64)
                pose=np.eye(4)
                modes={}
                for reuse in (False,True):
                    renderer=Renderer(reuse_endpoints=reuse);renderer.initialize()
                    try:
                        results=[]
                        for state in states:
                            gl.glFinish();started=time.perf_counter()
                            renderer.upload(state,pose,set())
                            submitted=time.perf_counter()
                            renderer.target(args.width,args.height);renderer.begin(k,pose,args.width,args.height)
                            renderer.splats();renderer.splats(depth_only=True);renderer.finish(0,args.width,args.height)
                            gl.glFinish();finished=time.perf_counter()
                            results.append({'upload_and_sort_submission_ms':(submitted-started)*1000,
                                            'completed_ms':(finished-started)*1000,
                                            'endpoint_uploads':renderer.last_endpoint_uploads,
                                            'endpoint_upload_bytes':renderer.last_endpoint_upload_bytes,
                                            'swapped':renderer.last_endpoint_swapped,'sort_backend':renderer.sort_backend})
                        modes['reuse' if reuse else 'upload_both']=results
                    finally:renderer.close()
                output['measurements']=modes
                print(json.dumps(output,indent=2))
    finally:context.close()


if __name__=='__main__':main()
