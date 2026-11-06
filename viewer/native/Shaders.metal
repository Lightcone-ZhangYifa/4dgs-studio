#include <metal_stdlib>
using namespace metal;

// Binary datasource ABI: exactly 64 bytes, std140-like four float4 values.
struct GaussianRecord { float4 mean; float4 scale; float4 quat; float4 color; };
struct Projected { float4 centerDepth; float4 axes; float4 color; uint4 info; };
struct Uniforms {
    float4x4 worldToCamera;
    float4 intrinsics; // fx,fy,cx,cy; half-integer framebuffer centers
    float4 viewport;   // width,height,skew,interpolation
    float4 controls;   // opacity multiplier,scale multiplier,exposure stops,near
    uint4 counts;      // N, selected source index, showGrid, grid normal axis (0=x,1=y,2=z)
    float4 depthPolicy; // far, near-inclusive, far-inclusive, project divide epsilon
    float4 projectionPolicy; // post-K pixel offset xy, Jacobian mode, factor
    float4 filterPolicy; // covariance dilation, raster shift, bounded preview exposure gain, reserved
};

float4 slerpQuaternion(float4 a, float4 b, float t) {
    a=normalize(a); b=normalize(b); float cosine=dot(a,b);
    if(cosine<0){b=-b;cosine=-cosine;}
    if(cosine>.9995f)return normalize(mix(a,b,t));
    float theta=acos(clamp(cosine,-1.f,1.f));
    return (sin((1-t)*theta)*a+sin(t*theta)*b)/sin(theta);
}
float3x3 quaternionRotation(float4 q) {
    float w=q.x,x=q.y,y=q.z,z=q.w;
    return float3x3(float3(1-2*y*y-2*z*z,2*x*y+2*w*z,2*x*z-2*w*y),
                   float3(2*x*y-2*w*z,1-2*x*x-2*z*z,2*y*z+2*w*x),
                   float3(2*x*z+2*w*y,2*y*z-2*w*x,1-2*x*x-2*y*y));
}

kernel void projectGaussians(device const GaussianRecord *a [[buffer(0)]],
                             device const GaussianRecord *b [[buffer(1)]],
                             device Projected *projected [[buffer(2)]],
                             device uint2 *indices [[buffer(3)]],
                             device atomic_uint *visible [[buffer(4)]],
                             constant Uniforms &u [[buffer(5)]], device const uchar *selected [[buffer(6)]],
                             constant uint &hasSelection [[buffer(7)]], uint i [[thread_position_in_grid]]) {
    if(i>=u.counts.x)return;
    indices[i]=uint2(0xffffffffu,i);
    float t=u.viewport.w;
    GaussianRecord ga=a[i],gb=b[i];
    float4 mean=mix(ga.mean,gb.mean,t);
    bool valid=t<=0?ga.scale.w>.5f:(t>=1?gb.scale.w>.5f:(ga.scale.w>.5f&&gb.scale.w>.5f));
    Projected p; p.centerDepth=float4(0,0,0,0);p.axes=0;p.color=0;p.info=uint4(i,0,0,0);
    float3 cam=(u.worldToCamera*float4(mean.xyz,1)).xyz;
    float opacity=clamp(mean.w*u.controls.x,0.f,.999f);
    if(!valid||!all(isfinite(cam))||cam.z<=0 || (u.depthPolicy.y>.5f ? cam.z<u.controls.w : cam.z<=u.controls.w) || (u.depthPolicy.z>.5f ? cam.z>u.depthPolicy.x : cam.z>=u.depthPolicy.x)||opacity<1.f/255.f){projected[i]=p;return;}
    float3 scale=mix(ga.scale.xyz,gb.scale.xyz,t)*u.controls.y;
    float4 q=slerpQuaternion(ga.quat,gb.quat,t);
    float3x3 rotation=quaternionRotation(q);
    float3x3 viewRotation=float3x3(u.worldToCamera[0].xyz,u.worldToCamera[1].xyz,u.worldToCamera[2].xyz);
    float3x3 basis=viewRotation*rotation;
    float invZ=1.f/cam.z;
    float fx=u.intrinsics.x,fy=u.intrinsics.y,skew=u.viewport.z;
    float projectInvZ=1.f/(cam.z+u.depthPolicy.w);
    float2 center=float2((fx*cam.x+skew*cam.y)*projectInvZ+u.intrinsics.z,fy*cam.y*projectInvZ+u.intrinsics.w)+u.projectionPolicy.xy;
    // Stabilize the covariance Jacobian outside an expanded camera frustum.
    // The projected center remains unchanged. As in gsplat's perspective
    // projection, clamp only the derivatives to avoid near-camera, offscreen
    // Gaussians growing huge ellipses across the image. Derive bounds from all
    // four inverse-K corners so skew and off-center/letterboxed K are supported.
    float yLo=-u.intrinsics.w/fy, yHi=(u.viewport.y-u.intrinsics.w)/fy;
    float skewLo=min(skew*yLo,skew*yHi),skewHi=max(skew*yLo,skew*yHi);
    float xLo=(-u.intrinsics.z-skewHi)/fx;
    float xHi=(u.viewport.x-u.intrinsics.z-skewLo)/fx;
    float2 lo=float2(xLo,yLo),hi=float2(xHi,yHi),margin=.5f*(u.projectionPolicy.w-1.f)*(hi-lo);
    float2 derivativeRay=clamp(cam.xy*invZ,lo-margin,hi+margin);
    if(u.projectionPolicy.z>.5f && u.projectionPolicy.z<1.5f){
        float2 bound=u.projectionPolicy.w*.5f*float2(u.viewport.x/fx,u.viewport.y/fy);
        derivativeRay=clamp(cam.xy*invZ,-bound,bound);
    }else if(u.projectionPolicy.z>2.5f){
        float2 principal=u.intrinsics.zw-u.filterPolicy.y;
        float2 halfMargin=(u.projectionPolicy.w-1.f)*.5f*float2(u.viewport.x/fx,u.viewport.y/fy);
        float2 lower=-principal/float2(fx,fy)-halfMargin;
        float2 upper=(u.viewport.xy-principal)/float2(fx,fy)+halfMargin;
        derivativeRay=clamp(cam.xy*invZ,lower,upper);
    }else if(u.projectionPolicy.z>=1.5f){derivativeRay=cam.xy*invZ;}
    float3 jx=float3(fx*invZ,skew*invZ,-(fx*derivativeRay.x+skew*derivativeRay.y)*invZ);
    float3 jy=float3(0,fy*invZ,-fy*derivativeRay.y*invZ);
    float3 ex=float3(dot(jx,basis[0]),dot(jx,basis[1]),dot(jx,basis[2]))*scale;
    float3 ey=float3(dot(jy,basis[0]),dot(jy,basis[1]),dot(jy,basis[2]))*scale;
    float cxx=dot(ex,ex)+u.filterPolicy.x,cxy=dot(ex,ey),cyy=dot(ey,ey)+u.filterPolicy.x;
    float halfTrace=.5f*(cxx+cyy),delta=sqrt(max(.25f*(cxx-cyy)*(cxx-cyy)+cxy*cxy,0.f));
    float lambda1=max(halfTrace+delta,.01f),lambda2=max(halfTrace-delta,.01f);
    // A circular covariance has no preferred eigendirection. Metal fast math
    // does not give a usable basis for atan2(0,0); choose the image axes.
    float angle=(cxy==0.f&&cxx==cyy)?0.f:.5f*atan2(2*cxy,cxx-cyy);
    float2 major=float2(cos(angle),sin(angle))*sqrt(lambda1);
    float2 minor=float2(-sin(angle),cos(angle))*sqrt(lambda2);
    float2 bound=3.f*sqrt(float2(cxx,cyy));
    if(!all(isfinite(bound))||center.x+bound.x<0||center.y+bound.y<0||center.x-bound.x>u.viewport.x||center.y-bound.y>u.viewport.y){projected[i]=p;return;}
    p.centerDepth=float4(center,cam.z,opacity);p.axes=float4(major,minor);
    p.color=mix(ga.color,gb.color,t);p.color.xyz*=exp2(u.controls.z);
    p.info.y=1;p.info.z=hasSelection?uint(selected[i]):uint(i==u.counts.y);projected[i]=p;indices[i]=uint2(~as_type<uint>(cam.z),i);atomic_fetch_add_explicit(visible,1,memory_order_relaxed);
}

// Eight stable LSD radix passes. Every pass is linear in N; no CPU point scan.
kernel void radixHistogram(device const Projected *p [[buffer(0)]],device const uint2 *indices [[buffer(1)]],
                           device uint *histograms [[buffer(2)]],constant uint4 &args [[buffer(3)]],
                           uint local [[thread_index_in_threadgroup]],uint group [[threadgroup_position_in_grid]]){
    threadgroup atomic_uint bins[16];if(local<16)atomic_store_explicit(&bins[local],0,memory_order_relaxed);
    threadgroup_barrier(mem_flags::mem_threadgroup);
    uint i=group*256+local;
    if(i<args.x){uint digit=(indices[i].x>>args.y)&15;atomic_fetch_add_explicit(&bins[digit],1,memory_order_relaxed);}
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if(local<16)histograms[group*16+local]=atomic_load_explicit(&bins[local],memory_order_relaxed);
}
kernel void radixPrefix(device const uint *histograms [[buffer(0)]],device uint *prefixes [[buffer(1)]],
                        device uint *totals [[buffer(2)]],constant uint4 &args [[buffer(3)]],uint bin [[thread_position_in_grid]]){
    if(bin>=16)return;uint sum=0;
    for(uint group=0;group<args.z;group++){uint index=group*16+bin;prefixes[index]=sum;sum+=histograms[index];}
    totals[bin]=sum;
}
kernel void radixScatter(device const Projected *p [[buffer(0)]],device const uint2 *source [[buffer(1)]],
                         device uint2 *destination [[buffer(2)]],device const uint *prefixes [[buffer(3)]],
                         device const uint *totals [[buffer(4)]],constant uint4 &args [[buffer(5)]],
                         uint local [[thread_index_in_threadgroup]],uint group [[threadgroup_position_in_grid]],
                         uint lane [[thread_index_in_simdgroup]],uint simdGroup [[simdgroup_index_in_threadgroup]]){
    threadgroup uint simdCounts[128];
    if(local<128)simdCounts[local]=0;
    threadgroup_barrier(mem_flags::mem_threadgroup);
    uint i=group*256+local;uint2 pair=i<args.x?source[i]:uint2(0xffffffffu,0);
    uint digit=i<args.x?((pair.x>>args.y)&15):16;
    // Four warp ballots encode all 16 bins. Intersecting their masks yields
    // the caller's same-digit lanes, avoiding 16 separate prefix reductions.
    uint active=uint((simd_vote::vote_t)simd_ballot(i<args.x));
    uint bit0=uint((simd_vote::vote_t)simd_ballot((digit&1)!=0));
    uint bit1=uint((simd_vote::vote_t)simd_ballot((digit&2)!=0));
    uint bit2=uint((simd_vote::vote_t)simd_ballot((digit&4)!=0));
    uint bit3=uint((simd_vote::vote_t)simd_ballot((digit&8)!=0));
    uint same=active&((digit&1)?bit0:~bit0)&((digit&2)?bit1:~bit1)&((digit&4)?bit2:~bit2)&((digit&8)?bit3:~bit3);
    uint localRank=popcount(same&((1u<<lane)-1u));
    if(digit<16&&localRank==0)simdCounts[simdGroup*16+digit]=popcount(same);
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if(i>=args.x)return;
    for(uint earlier=0;earlier<simdGroup;earlier++)localRank+=simdCounts[earlier*16+digit];
    uint base=0;for(uint bin=0;bin<digit;bin++)base+=totals[bin];
    destination[base+prefixes[group*16+digit]+localRank]=pair;
}
kernel void makeDrawArguments(device const atomic_uint *visible [[buffer(0)]],device uint4 *args [[buffer(1)]]){
    args[0]=uint4(6,atomic_load_explicit(visible,memory_order_relaxed),0,0);
}
struct Raster {float4 position [[position]];float2 local;float4 color;float opacity;uint selected [[flat]];};
vertex Raster gaussianVertex(uint vertexID [[vertex_id]],uint instanceID [[instance_id]],
                             device const Projected *p [[buffer(0)]],device const uint2 *indices [[buffer(1)]],
                             constant Uniforms &u [[buffer(2)]]){
    const float2 corners[6]={float2(-3,-3),float2(3,-3),float2(-3,3),float2(-3,3),float2(3,-3),float2(3,3)};
    Projected g=p[indices[instanceID].y];float2 c=corners[vertexID];
    float2 pixel=g.centerDepth.xy+g.axes.xy*c.x+g.axes.zw*c.y;
    Raster out;out.position=float4(2*pixel.x/u.viewport.x-1,1-2*pixel.y/u.viewport.y,0,1);
    out.local=c;out.color=g.color;out.opacity=g.centerDepth.w;out.selected=g.info.z;return out;
}
fragment float4 gaussianFragment(Raster in [[stage_in]]){
    float r2=dot(in.local,in.local);if(r2>9)discard_fragment();
    float alpha=min(.999f,in.opacity*exp(-.5f*r2));if(alpha<1.f/255.f)discard_fragment();
    float3 color=in.color.xyz;if(in.selected&&r2>5.5f)color=mix(color,float3(1,.8,.12),.85f);
    return float4(color*alpha,alpha);
}

struct GridRaster {float4 position [[position]];float4 color;};
vertex GridRaster gridVertex(uint i [[vertex_id]],constant Uniforms &u [[buffer(0)]]){
    uint line=i/2,endpoint=i%2;float3 position;float4 color=float4(.18,.22,.28,.6);
    if(line<42){float offset=float(int(line%21)-10);position=line<21?float3(offset,0,endpoint?10:-10):float3(endpoint?10:-10,0,offset);}
    else{uint axis=line-42;position=float3(0);position[axis]=endpoint?2.f:0.f;color=float4(axis==0?.9f:.15f,axis==1?.9f:.15f,axis==2?.9f:.15f,.9f);}
    if(line<42){if(u.counts.w==0)position=float3(0,position.x,position.z);else if(u.counts.w==2)position=float3(position.x,position.z,0);}
    float3 cam=(u.worldToCamera*float4(position,1)).xyz;
    float2 pixel=float2((u.intrinsics.x*cam.x+u.viewport.z*cam.y)/cam.z+u.intrinsics.z,u.intrinsics.y*cam.y/cam.z+u.intrinsics.w);
    GridRaster out;out.position=cam.z>u.controls.w?float4(2*pixel.x/u.viewport.x-1,1-2*pixel.y/u.viewport.y,0,1):float4(4,4,0,1);
    out.color=float4(color.xyz*color.w,color.w);return out;
}
fragment float4 gridFragment(GridRaster in [[stage_in]]){return in.color;}
vertex GridRaster scaffoldLineVertex(uint i [[vertex_id]],device const float4 *points [[buffer(0)]],constant Uniforms &u [[buffer(1)]]){
    uint segment=i/2;float4 a=points[segment],b=points[segment+1];
    float3 ca=(u.worldToCamera*float4(a.xyz,1)).xyz,cb=(u.worldToCamera*float4(b.xyz,1)).xyz;
    float3 cam=(i%2)?cb:ca;bool valid=a.w>.5f&&b.w>.5f&&ca.z>u.controls.w&&cb.z>u.controls.w;
    float2 pixel=float2((u.intrinsics.x*cam.x+u.viewport.z*cam.y)/cam.z+u.intrinsics.z,u.intrinsics.y*cam.y/cam.z+u.intrinsics.w);
    GridRaster out;out.position=valid?float4(2*pixel.x/u.viewport.x-1,1-2*pixel.y/u.viewport.y,0,1):float4(4,4,0,1);
    out.color=float4(1,.76,.12,1);return out;
}

bool pickContains(Projected p,float2 pixel){
    if(!p.info.y)return false;float2 d=pixel-p.centerDepth.xy;
    float determinant=p.axes.x*p.axes.w-p.axes.y*p.axes.z;if(abs(determinant)<1e-12)return false;
    float2 local=float2(p.axes.w*d.x-p.axes.z*d.y,-p.axes.y*d.x+p.axes.x*d.y)/determinant;
    float r2=dot(local,local);float alpha=p.centerDepth.w*exp(-.5f*r2);return r2<=9&&alpha>=.1f;
}
kernel void pickDepth(device const Projected *p [[buffer(0)]],device atomic_uint *pick [[buffer(1)]],
                      constant uint4 &args [[buffer(2)]],uint i [[thread_position_in_grid]]){
    if(i<args.x&&pickContains(p[i],float2(args.yz)+.5f))atomic_fetch_min_explicit(&pick[1],as_type<uint>(p[i].centerDepth.z),memory_order_relaxed);
}
kernel void pickIndex(device const Projected *p [[buffer(0)]],device atomic_uint *pick [[buffer(1)]],
                      constant uint4 &args [[buffer(2)]],uint i [[thread_position_in_grid]]){
    if(i<args.x&&p[i].info.y&&as_type<uint>(p[i].centerDepth.z)==atomic_load_explicit(&pick[1],memory_order_relaxed)&&pickContains(p[i],float2(args.yz)+.5f))
        atomic_fetch_min_explicit(&pick[0],i,memory_order_relaxed);
}

// Fixed-size navigation marker. It is display metadata, never a source primitive.
vertex GridRaster pivotVertex(uint i [[vertex_id]], constant Uniforms &u [[buffer(0)]], constant float4 &pivot [[buffer(1)]]) {
    float3 cam=(u.worldToCamera*pivot).xyz;
    float2 pixel=float2((u.intrinsics.x*cam.x+u.viewport.z*cam.y)/cam.z+u.intrinsics.z,u.intrinsics.y*cam.y/cam.z+u.intrinsics.w);
    float2 offsets[4]={float2(-9,0),float2(9,0),float2(0,-9),float2(0,9)};
    pixel+=offsets[i];
    GridRaster out;
    out.position=cam.z>u.controls.w?float4(2*pixel.x/u.viewport.x-1,1-2*pixel.y/u.viewport.y,0,1):float4(4,4,0,1);
    out.color=float4(1,.65,.12,1);return out;
}

// Preview metadata is deliberately separate from the scientific RGB compositor.
// A Gaussian volume has no unique opaque surface. Display depth uses alpha>=0.1
// at center depth, the same depth model as the splat compositor and picking.
vertex Raster gaussianDepthVertex(uint vertexID [[vertex_id]], uint instanceID [[instance_id]],
                                  device const Projected *p [[buffer(0)]], device const uint2 *indices [[buffer(1)]],
                                  constant Uniforms &u [[buffer(2)]]) {
    const float2 corners[6]={float2(-3,-3),float2(3,-3),float2(-3,3),float2(-3,3),float2(3,-3),float2(3,3)};
    Projected g=p[indices[instanceID].y];float2 c=corners[vertexID];
    float2 pixel=g.centerDepth.xy+g.axes.xy*c.x+g.axes.zw*c.y;
    Raster out;out.position=float4(2*pixel.x/u.viewport.x-1,1-2*pixel.y/u.viewport.y,g.centerDepth.z>0 ? max(0.f,1.f-u.controls.w/g.centerDepth.z) : 1.f,1);
    out.local=c;out.color=g.color;out.opacity=g.centerDepth.w;out.selected=0;return out;
}
fragment void gaussianDepthFragment(Raster in [[stage_in]]) {
    float r2=dot(in.local,in.local);
    if(r2>9 || in.opacity*exp(-.5f*r2)<.1f)discard_fragment();
}
struct TrajectorySegment { float4 startPositionTime; float4 endPositionTime; float4 startColor; float4 endColor; };
struct LineRaster { float4 position [[position]]; float4 color; float cameraDepth; };
// Drawing and wireframe picking share camera-space clipping and projection.
// The two consumers must not diverge for calibrated/skewed cameras or near/far cuts.
struct LineProjection { float3 cameraA; float3 cameraB; float2 pixelA; float2 pixelB; float start; float end; bool valid; };
LineProjection projectLineEndpoints(float3 a,float3 b,constant Uniforms &u) {
    LineProjection line;line.valid=false;line.start=0;line.end=1;
    float3 ca=(u.worldToCamera*float4(a,1)).xyz,cb=(u.worldToCamera*float4(b,1)).xyz;
    float nearZ=max(u.controls.w,1e-7f),farZ=u.depthPolicy.x;
    if(!all(isfinite(ca))||!all(isfinite(cb))||
       (u.depthPolicy.y>.5f ? max(ca.z,cb.z)<nearZ:max(ca.z,cb.z)<=nearZ)||
       (u.depthPolicy.z>.5f ? min(ca.z,cb.z)>farZ:min(ca.z,cb.z)>=farZ))return line;
    float delta=cb.z-ca.z;
    if(abs(delta)>1e-12f){
        float nearT=(nearZ-ca.z)/delta,farT=(farZ-ca.z)/delta;
        if(delta>0){line.start=max(line.start,nearT);line.end=min(line.end,farT);}
        else{line.end=min(line.end,nearT);line.start=max(line.start,farT);}
    }
    if(line.end<line.start)return line;
    line.cameraA=mix(ca,cb,line.start);line.cameraB=mix(ca,cb,line.end);
    float za=line.cameraA.z+u.depthPolicy.w,zb=line.cameraB.z+u.depthPolicy.w;
    if(za<=0||zb<=0)return line;
    line.pixelA=float2((u.intrinsics.x*line.cameraA.x+u.viewport.z*line.cameraA.y)/za+u.intrinsics.z,u.intrinsics.y*line.cameraA.y/za+u.intrinsics.w)+u.projectionPolicy.xy;
    line.pixelB=float2((u.intrinsics.x*line.cameraB.x+u.viewport.z*line.cameraB.y)/zb+u.intrinsics.z,u.intrinsics.y*line.cameraB.y/zb+u.intrinsics.w)+u.projectionPolicy.xy;
    line.valid=all(isfinite(line.pixelA))&&all(isfinite(line.pixelB));return line;
}
LineRaster projectLine(float3 a,float3 b,float4 colorA,float4 colorB,uint endpoint,constant Uniforms &u) {
    LineRaster out;out.position=float4(4,4,1,1);out.color=0;out.cameraDepth=1;
    LineProjection line=projectLineEndpoints(a,b,u);if(!line.valid)return out;
    float3 cam=endpoint?line.cameraB:line.cameraA;float2 pixel=endpoint?line.pixelB:line.pixelA;
    // Homogeneous coordinates retain perspective-correct interpolated depth.
    out.position=float4((2*pixel.x/u.viewport.x-1)*cam.z,(1-2*pixel.y/u.viewport.y)*cam.z,cam.z-max(u.controls.w,1e-7f),cam.z);
    out.color=mix(colorA,colorB,endpoint?line.end:line.start);out.color.xyz*=u.filterPolicy.z;out.cameraDepth=cam.z;
    return out;
}
struct CameraPickSegment { float4 startPosition; float4 endPosition; uint4 identity; };
// Click tolerance applies to frustum edges only, never to decorative motion paths.
// Surface occlusion is tested at the closest visible edge location, rather than
// at the cursor, which may be several pixels away from the one-pixel line.
bool visibleCameraPick(CameraPickSegment edge,constant Uniforms &u,float4 args,
                       depth2d<float,access::read> surface,thread float &cameraDepth) {
    if(edge.identity.x==0xffffffffu)return false;
    LineProjection line=projectLineEndpoints(edge.startPosition.xyz,edge.endPosition.xyz,u);
    if(!line.valid)return false;
    float2 delta=line.pixelB-line.pixelA;float lengthSquared=dot(delta,delta);
    // Rasterization clips to the image rectangle as well. Restrict the click
    // sample to that visible span, so a closest point just outside a corner
    // does not incorrectly reject the nearby visible endpoint.
    float first=0,last=1;
    for(uint axis=0;axis<2;axis++){
        if(abs(delta[axis])<1e-12f){if(line.pixelA[axis]<0||line.pixelA[axis]>=u.viewport[axis])return false;}
        else{
            float a=-line.pixelA[axis]/delta[axis],b=(u.viewport[axis]-1e-4f-line.pixelA[axis])/delta[axis];
            first=max(first,min(a,b));last=min(last,max(a,b));
        }
    }
    if(last<first)return false;
    float t=lengthSquared>1e-12f?clamp(dot(args.xy-line.pixelA,delta)/lengthSquared,first,last):first;
    float2 closest=mix(line.pixelA,line.pixelB,t);
    if(dot(closest-args.xy,closest-args.xy)>args.z*args.z ||
       any(closest<0.f)||any(closest>=u.viewport.xy))return false;
    // The display vertices use camera z as homogeneous w. Interpolate inverse
    // z in screen space, matching raster depth even for strongly slanted edges.
    cameraDepth=1.f/mix(1.f/line.cameraA.z,1.f/line.cameraB.z,t);
    if(!isfinite(cameraDepth)||cameraDepth<=0)return false;
    float lineDepth=max(0.f,1.f-max(u.controls.w,1e-7f)/cameraDepth);
    float surfaceDepth=surface.read(uint2(closest));
    return lineDepth<=surfaceDepth+2e-7f;
}
kernel void cameraPickDepth(device const CameraPickSegment *edges [[buffer(0)]],device atomic_uint *pick [[buffer(1)]],
                            constant Uniforms &u [[buffer(2)]],constant float4 &args [[buffer(3)]],
                            constant uint &count [[buffer(4)]],depth2d<float,access::read> surface [[texture(0)]],
                            uint i [[thread_position_in_grid]]) {
    float depth;if(i<count&&visibleCameraPick(edges[i],u,args,surface,depth))
        atomic_fetch_min_explicit(&pick[3],as_type<uint>(depth),memory_order_relaxed);
}
kernel void cameraPickIndex(device const CameraPickSegment *edges [[buffer(0)]],device atomic_uint *pick [[buffer(1)]],
                            constant Uniforms &u [[buffer(2)]],constant float4 &args [[buffer(3)]],
                            constant uint &count [[buffer(4)]],depth2d<float,access::read> surface [[texture(0)]],
                            uint i [[thread_position_in_grid]]) {
    float depth;if(i<count&&visibleCameraPick(edges[i],u,args,surface,depth)&&
        as_type<uint>(depth)==atomic_load_explicit(&pick[3],memory_order_relaxed))
        atomic_fetch_min_explicit(&pick[2],edges[i].identity.x,memory_order_relaxed);
}
vertex LineRaster coloredTrajectoryVertex(uint i [[vertex_id]], device const TrajectorySegment *segments [[buffer(0)]],
                                           constant Uniforms &u [[buffer(1)]], constant float4 &playback [[buffer(2)]]) {
    TrajectorySegment s=segments[i/2];float a=s.startPositionTime.w,b=s.endPositionTime.w;
    float start=0,end=1;float alpha=playback.z;
    if(playback.w>.5f){
        float duration=b-a;
        if(duration<=0 || b<playback.y || a>playback.x){LineRaster hidden;hidden.position=float4(4,4,1,1);hidden.color=0;hidden.cameraDepth=1;return hidden;}
        start=clamp((playback.y-a)/duration,0.f,1.f);end=clamp((playback.x-a)/duration,0.f,1.f);alpha=1;
        if(end<=start){LineRaster hidden;hidden.position=float4(4,4,1,1);hidden.color=0;hidden.cameraDepth=1;return hidden;}
    }
    float3 pa=mix(s.startPositionTime.xyz,s.endPositionTime.xyz,start),pb=mix(s.startPositionTime.xyz,s.endPositionTime.xyz,end);
    float4 ca=float4(mix(s.startColor.xyz,s.endColor.xyz,start),alpha),cb=float4(mix(s.startColor.xyz,s.endColor.xyz,end),alpha);
    return projectLine(pa,pb,ca,cb,i%2,u);
}
vertex LineRaster coloredCameraVertex(uint i [[vertex_id]], device const TrajectorySegment *segments [[buffer(0)]], constant Uniforms &u [[buffer(1)]]) {
    TrajectorySegment s=segments[i/2];return projectLine(s.startPositionTime.xyz,s.endPositionTime.xyz,s.startColor,s.endColor,i%2,u);
}
fragment float4 cameraOpaqueFragment(LineRaster in [[stage_in]]) { if(in.color.w<.999f)discard_fragment(); return float4(in.color.xyz,1); }
fragment float4 lineFragment(LineRaster in [[stage_in]]) { return float4(in.color.xyz*in.color.w,in.color.w); }
struct PathAccumulation { float4 weighted [[color(0)]]; float revealage [[color(1)]]; };
PathAccumulation accumulatePath(LineRaster in) {
    float alpha=clamp(in.color.w,0.f,1.f);
    // Weighted blended OIT preserves total coverage without a millions-segment
    // sort. Mixed-color translucent intersections are an OIT approximation.
    float weight=clamp(1.f/(1.f+.03f*in.cameraDepth*in.cameraDepth),.01f,1.f);
    PathAccumulation out;out.weighted=float4(clamp(in.color.xyz,0.f,1.f)*alpha*weight,alpha*weight);out.revealage=alpha;return out;
}
fragment PathAccumulation pathAccumulationFragment(LineRaster in [[stage_in]]) {return accumulatePath(in);}
fragment PathAccumulation cameraAccumulationFragment(LineRaster in [[stage_in]]) {
    if(in.color.w>=.999f)discard_fragment();
    return accumulatePath(in);
}
struct CompositeRaster { float4 position [[position]]; };
vertex CompositeRaster pathCompositeVertex(uint i [[vertex_id]]) {
    float2 p[3]={float2(-1,-1),float2(3,-1),float2(-1,3)};CompositeRaster out;out.position=float4(p[i],0,1);return out;
}
fragment float4 pathCompositeFragment(CompositeRaster in [[stage_in]],texture2d<float> accumulation [[texture(0)]],texture2d<float> revealage [[texture(1)]]) {
    uint2 pixel=uint2(in.position.xy);float4 value=accumulation.read(pixel);float alpha=1-clamp(revealage.read(pixel).x,0.f,1.f);
    if(value.w<1e-7f||alpha<1e-6f)return float4(0);
    return float4(value.xyz/value.w*alpha,alpha);
}
struct MarkerRaster {float4 position [[position]];float2 local;};
vertex MarkerRaster selectionMarkerVertex(uint vertexID [[vertex_id]],uint instanceID [[instance_id]],
                                          device const Projected *projected [[buffer(0)]],device const uint *selected [[buffer(1)]],constant Uniforms &u [[buffer(2)]]) {
    const float2 c[6]={float2(-1,-1),float2(1,-1),float2(-1,1),float2(-1,1),float2(1,-1),float2(1,1)};
    MarkerRaster out;out.position=float4(4,4,1,1);out.local=c[vertexID];uint index=selected[instanceID];
    if(index>=u.counts.x)return out;Projected g=projected[index];if(!g.info.y)return out;
    float radius=max(6.f,min(80.f,3.f*max(length(g.axes.xy),length(g.axes.zw))+2.f));
    float2 pixel=g.centerDepth.xy+c[vertexID]*radius;
    out.position=float4(2*pixel.x/u.viewport.x-1,1-2*pixel.y/u.viewport.y,max(0.f,1.f-u.controls.w/g.centerDepth.z-1e-7f),1);
    return out;
}
fragment float4 selectionMarkerFragment(MarkerRaster in [[stage_in]]) {
    float radius=length(in.local);if(radius<.84f||radius>1.f)discard_fragment();
    return radius>.96f ? float4(.09,.05,0,1) : float4(1,.78,.08,1);
}
