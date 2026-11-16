"""Bounded, asynchronous GL 3.3 frustum-visible Gaussian statistics.

Projection is compiled from the renderer's vertex shader. One point invocation
per Gaussian and a one-point geometry filter produce a scalar query result;
no per-primitive values or blocking result reads return to the CPU.
"""
from __future__ import annotations

import time
import numpy as np
from OpenGL import GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader


GEOMETRY = r'''#version 330 core
layout(points) in;
layout(points,max_vertices=1) out;
flat in int frustumVisible[];
void main() {
    if(frustumVisible[0]==0)return;
    gl_Position=vec4(0,0,0,1);
    EmitVertex();EndPrimitive();
}
'''


class GpuVisibility:
    def __init__(self, *, interval=.1, maximum_queries=3):
        self.interval = float(interval)
        self.maximum_queries = int(maximum_queries)
        self.enabled = True
        self.supported = False
        self.reason = 'Frustum visibility queries are not initialized'
        self.program = 0
        self.pending, self.free = [], []
        self.objects = set()
        self.epoch = 0
        self.next_sample_at = 0.
        self.samples_submitted = self.samples_completed = self.samples_skipped = 0
        self.last_result = None
        self.empty = False

    def initialize(self, vertex_source):
        try:
            if int(gl.glGetQueryiv(gl.GL_PRIMITIVES_GENERATED,gl.GL_QUERY_COUNTER_BITS))<=0:
                raise RuntimeError('GL_PRIMITIVES_GENERATED is unavailable')
            self.program = int(compileProgram(compileShader(vertex_source,gl.GL_VERTEX_SHADER),
                                               compileShader(GEOMETRY,gl.GL_GEOMETRY_SHADER)))
            self.supported = True
            self.reason = 'Asynchronous GL 3.3 projection/geometry query; at most 10 samples per second'
        except Exception as error:
            self._fail(error)

    def reset(self):
        """Forget an old source immediately; drain old queries when ready."""
        self.epoch += 1
        self.next_sample_at = 0.
        self.last_result = None
        self.empty = False

    def mark_empty(self):
        """An empty draw has exactly zero visible primitives, without a query."""
        if not self.empty:
            self.reset()
            self.empty = True

    def _fail(self, error):
        self.supported = False
        self.last_result = None
        self.reason = 'Frustum visibility unavailable; rendering continues: '+str(error)

    def submit(self, draw, sequence, metadata=None):
        if not self.enabled or not self.supported:
            return False
        now = time.monotonic()
        if now < self.next_sample_at:
            return False
        active = False
        query = None
        try:
            self.poll()
            if not self.supported:
                return False
            # Do not disturb another renderer/profiler's query target.
            if int(gl.glGetQueryiv(gl.GL_PRIMITIVES_GENERATED,gl.GL_CURRENT_QUERY)):
                self.samples_skipped += 1
                return False
            if self.free:
                query = self.free.pop()
            elif len(self.objects) < self.maximum_queries:
                query = int(np.asarray(gl.glGenQueries(1)).reshape(-1)[0])
                self.objects.add(query)
            else:
                self.samples_skipped += 1
                return False
            gl.glBeginQuery(gl.GL_PRIMITIVES_GENERATED,query)
            active = True
            draw(self.program)
            gl.glEndQuery(gl.GL_PRIMITIVES_GENERATED)
            active = False
            self.pending.append({'query':query,'epoch':self.epoch,'sequence':int(sequence),
                                 'submitted_at':now,'metadata':dict(metadata or {})})
            self.next_sample_at = now+self.interval
            self.samples_submitted += 1
            return True
        except Exception as error:
            if active:
                try:
                    gl.glEndQuery(gl.GL_PRIMITIVES_GENERATED)
                except Exception:
                    pass
            self._fail(error)
            return False

    def poll(self):
        if not self.supported:
            return False
        changed = False
        try:
            while self.pending:
                token = self.pending[0]
                query = token['query']
                if not bool(gl.glGetQueryObjectiv(query,gl.GL_QUERY_RESULT_AVAILABLE)):
                    break
                self.pending.pop(0)
                self.free.append(query)
                if token['epoch'] != self.epoch:
                    continue
                value = int(gl.glGetQueryObjectuiv(query,gl.GL_QUERY_RESULT))
                self.last_result = {**token,'count':value,'completed_at':time.monotonic()}
                self.samples_completed += 1
                changed = True
        except Exception as error:
            self._fail(error)
        return changed

    def summary(self):
        result = self.last_result if self.supported and self.enabled else None
        metadata = result['metadata'] if result else {}
        return {'visible_count':0 if self.empty else result['count'] if result else None,
                'visibility_available':self.empty or (self.supported and self.enabled),
                'visibility_reason':'Empty geometry; exactly zero visible Gaussians (no GPU sample)' if self.empty else self.reason,
                'visibility_method':'empty' if self.empty else 'gpu-query' if result else 'unavailable',
                'visibility_pending':bool(self.pending) and self.supported,
                'visibility_age_ms':(time.monotonic()-result['submitted_at'])*1000 if result else None,
                'visibility_sequence':result['sequence'] if result else None,
                'visibility_source_time':metadata.get('source_time'),
                'visibility_pose':metadata.get('pose'),
                'visibility_samples_submitted':self.samples_submitted,
                'visibility_samples_completed':self.samples_completed,
                'visibility_samples_skipped':self.samples_skipped}

    def close(self):
        if self.program:
            gl.glDeleteProgram(self.program)
        if self.objects:
            gl.glDeleteQueries(len(self.objects),list(self.objects))
        self.program = 0
        self.objects.clear()
        self.pending.clear()
        self.free.clear()
        self.supported = False
        self.last_result = None
        self.empty = False
