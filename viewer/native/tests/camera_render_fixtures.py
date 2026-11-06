"""Generate small cross-language camera/render parser fixtures without a GPU.

Usage: python3 viewer/native/tests/camera_render_fixtures.py OUTPUT
Run from the repository root. OUTPUT must not already contain fixture files.
Malformed files are deliberately envelope-valid but semantically invalid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from gsv import (Writer,Scene,GSVError,add_camera,add_camera_rgb,add_camera_affine,
                 add_persistent_gaussians,add_render_profile,add_render_view)


def profile_parameters():
    return dict(
        visibility={'center_depth':{'near':.2,'far':None,'near_inclusive':False,'far_inclusive':True},'camera_volume':'ignore'},
        projection={'divide_epsilon':1e-7,'jacobian':{'mode':'symmetric-fov','factor':1.3},'pixel_offset':[0.,0.]},
        filter={'covariance_dilation':.3,'radius_clip':0.,'antialias_compensation':False},
        support={'bounds':'tile-major-radius','sigma':3.,'tile_size':[16,16],'mahalanobis_cutoff':None},
        alpha={'minimum':1/255,'maximum':.99},
        compositing={'direction':'front-to-back','depth_sort':'tile-stable','tie_break':'source-order',
                     'transmittance':{'threshold':1e-4,'inclusive':False,'stage':'before-contribution'}})


def camera(out,*,full_tracks=False,distorted=False):
    times=[0.,1.] if full_tracks else [0.]
    count=len(times)
    K=np.repeat(np.array([[28.,.3,10.],[0.,27.,6.],[0.,0.,1.]])[None,None],count,axis=0)
    pose=np.tile(np.eye(4),(count,1,1,1))
    if full_tracks:K[-1,0,0,0]=31.;pose[-1,0,0,3]=.1
    kwargs={}
    if full_tracks:
        kwargs=dict(depth_mapping={'near':.001,'far':None,'ndc_range':'zero-to-one','direction':'reversed'},
            view_volume={'near':[[.1],[.2]],'far':[[10.],[0.]],'far_unbounded':[[False],[True]]},
            optics={'focal_length_mm':[[35.],[38.]],'sensor_width_mm':[[36.],[36.]],'sensor_height_mm':[[24.],[24.]],
                    'f_number':[[2.8],[4.]],'focus_distance':[[2.],[3.]]},
            acquisition={'exposure_seconds':[[.01],[.02]],'iso':[[100.],[200.]],'shutter_open':[[-.005],[-.01]],
                         'shutter_close':[[.005],[.01]],'rolling_readout':[[.01],[.01]],'rolling_direction':'top-to-bottom'})
    if distorted:kwargs['distortion']={'model':'opencv-brown','coefficients':np.array([[[.01,0,0,0,0]]])}
    return add_camera(out,'camera',times,K,pose,[[20,12]],pixel_center='half',view_ids=['camera-acquisition-17'],**kwargs)


def field(out,*,static=False,single=False,color_space='srgb'):
    if static:
        raw={'ids':np.array([7],np.int64),'means':np.array([[0.,0.,2.]],np.float32),
             'scales':np.full((1,3),.05,np.float32),'quaternions':np.array([[1.,0,0,0]],np.float32),
             'colors':np.array([[.2,.4,.6]],np.float32),'opacity':np.array([.8],np.float32)}
        data={name:out.add_array('field/'+name,value) for name,value in raw.items()};data['color_space']=color_space
        return out.add_object('field','gaussian','gsv.gaussian.static',data,space='world')
    frame=np.zeros((1,16),np.float32);frame[:,2]=2.;frame[:,3]=.8;frame[:,4:7]=.05
    frame[:,7:9]=1.;frame[:,12:15]=[.2,.4,.6];frame[:,15]=-1
    times=[0.] if single else [0.,.5,1.]
    return add_persistent_gaussians(out,'field',np.array([7],np.int64),times,[frame]*len(times),color_space=color_space,
                                    metadata={'purpose':'synthetic parser fixture'})


def observation(out,*,full_tracks=False,color_space='srgb',times=None):
    times=([0.,1.] if full_tracks else [0.,.5,1.]) if times is None else times
    samples=list(range(len(times))) if full_tracks else [0]*len(times)
    images=[np.full((12,20,3),i*40,np.uint8) for i in range(len(times))]
    return add_camera_rgb(out,'observations','camera',samples,[0]*len(times),images,times=times,color_space=color_space,
                          role='reference',metadata={'evidence':'synthetic parser pixels, not measured reconstruction GT'})


def build(out,kind):
    if kind=='empty':return
    full=kind=='full_camera_tracks'
    cam=camera(out,full_tracks=full,distorted=kind=='distorted_unsupported')
    if kind=='camera_only':return
    color='linear-srgb' if kind=='typed_domain_over_metadata' else 'srgb'
    geom=field(out,static=kind=='static_gaussian',single=kind=='metadata_cannot_extend_time',color_space=color)
    obs=observation(out,full_tracks=full,color_space=color,
                    times=[.5] if kind=='metadata_cannot_extend_time' else None)
    kwargs=profile_parameters()
    if kind=='symmetric_factor_half':kwargs['projection']['jacobian']['factor']=.5
    if kind=='image_bounds':kwargs['projection']['jacobian']['mode']='image-bounds'
    if full:kwargs['visibility']['camera_volume']='gaussian-center'
    if kind=='finite_inclusive_far':kwargs['visibility']['center_depth']={'near':0.,'far':2.,'near_inclusive':True,'far_inclusive':True}
    if kind=='finite_exclusive_far':kwargs['visibility']['center_depth']={'near':0.,'far':2.,'near_inclusive':True,'far_inclusive':False}
    profile=add_render_profile(out,'profile',**kwargs)
    view_kwargs=dict(observation_ids=['observations'],color_space=color)
    if kind=='integrated_unsupported':view_kwargs['temporal']={'mode':'integrated','shutter_open':-.01,'shutter_close':.01,'samples':8,'sampling':'uniform-midpoint'}
    if kind=='cropped_unsupported':view_kwargs.update(image_size=[10,6],camera_to_output=[[.5,0,0],[0,.5,0],[0,0,1]])
    if kind in ('affine','bad_field_radiometry'):
        add_camera_affine(out,'appearance','camera',np.ones((1,1,3),np.float32),np.full((1,1,3),.02,np.float32),color_space=color)
        view_kwargs['radiometry_id']='appearance'
    view=add_render_view(out,'view','camera',['field'],'profile',**view_kwargs)
    # The following mutations deliberately bypass helper validation to exercise
    # the native reader's own boundaries, while keeping envelope links valid.
    if kind=='bad_field_radiometry':geom.setdefault('links',{})['radiometry']='appearance'
    elif kind=='bad_missing_volume':profile['data']['visibility']['camera_volume']='gaussian-center'
    elif kind=='bad_boolean_profile':profile['data']['visibility']['center_depth']['near_inclusive']=1
    elif kind=='bad_missing_view_clock':view.pop('clock')
    elif kind=='bad_missing_view_space':view.pop('space')
    elif kind=='bad_field_domain':geom['data']['color_space']='linear-srgb'
    elif kind=='bad_missing_field_domain':geom['metadata']['color_space']='srgb';geom['data'].pop('color_space')
    elif kind=='bad_missing_observation_times':obs['data']['samples'].pop('times')
    elif kind=='bad_duplicate_observation_times':obs['data']['samples']['times']=out.add_array('bad_times',np.zeros(3,np.float64))
    elif kind=='bad_dynamic_observation_time':
        # A second calibration makes this a moving camera: three observations
        # still point to sample zero but must not invent nonzero sample times.
        cam['data']['times']=out.add_array('dynamic_times',np.array([0.,1.]))
        old=cam['data']['projection'];old['intrinsics']=out.add_array('dynamic_K',np.tile(np.eye(3),(2,1,1,1)))
        cam['data']['pose']['camera_to_world']=out.add_array('dynamic_poses',np.tile(np.eye(4),(2,1,1,1)))
    elif kind=='bad_extra_profile_field':profile['data']['alpha']['implicit_exposure']=2.
    elif kind=='bad_flat_camera_key':cam['data']['intrinsics']=cam['data']['projection']['intrinsics']
    elif kind=='bad_image_bounds_factor':profile['data']['projection']['jacobian']={'mode':'image-bounds','factor':.5}
    elif kind=='metadata_cannot_extend_time':geom['metadata']['static']=True
    elif kind=='typed_domain_over_metadata':geom['metadata']['color_space']='srgb'


def generate(destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    kinds=['empty','camera_only','full_camera_tracks','static_camera_observation_times','static_gaussian','affine',
           'symmetric_factor_half','image_bounds','finite_inclusive_far','finite_exclusive_far',
           'distorted_unsupported','integrated_unsupported','cropped_unsupported','metadata_cannot_extend_time','typed_domain_over_metadata',
           'bad_missing_volume','bad_boolean_profile','bad_missing_view_clock','bad_missing_view_space','bad_field_domain',
           'bad_missing_field_domain','bad_missing_observation_times','bad_duplicate_observation_times','bad_dynamic_observation_time',
           'bad_extra_profile_field','bad_flat_camera_key','bad_image_bounds_factor','bad_field_radiometry']
    paths=[destination/(kind+'.gsv') for kind in kinds]
    if any(path.exists() for path in paths) or (destination/'expectations.json').exists():
        raise FileExistsError('Use a fresh fixture destination')
    records=[]
    unsupported={'empty','camera_only','distorted_unsupported','integrated_unsupported','cropped_unsupported','metadata_cannot_extend_time'}
    for kind,path in zip(kinds,paths):
        accepted=not kind.startswith('bad_')
        with Writer(path,validate=False,metadata={'purpose':'synthetic native parser fixture'}) as out:build(out,kind)
        error=None
        try:
            with Scene(path) as scene:scene.validate()
        except GSVError as exc:error=str(exc)
        if (error is None) != accepted:raise AssertionError((kind,accepted,error))
        records.append({'name':kind,'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'expected_native_accept':accepted,'expected_sdk_valid':accepted,
                        'expected_comparison_supported':accepted and kind not in unsupported,
                        'expected_sdk_error':error})
    manifest={'fixture_contract':'gsv-camera-render','synthetic':True,'cases':records,
              'notes':['expected_comparison_supported includes exact geometry-time availability, not only view configuration',
                       'static_gaussian requires native static RGB field support',
                       'finite_*_far contains center z=2 at the far boundary: inclusive retains, exclusive removes',
                       'No expected image-quality metric or source-render parity is asserted']}
    (destination/'expectations.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest



def write_clock_domains(destination):
    """Two fields with disjoint times but shared inspection semantics."""
    with Writer(destination) as out:
        for domain, times in [('a', [0., 1.]), ('b', [10., 20.])]:
            frame = np.zeros((1, 16), np.float32)
            frame[0, 2:4] = [2., .8]
            frame[0, 4:7] = .08
            frame[0, 7:9] = 1
            frame[0, 12:15] = .5
            frame[0, 15] = -1
            field = 'field-' + domain
            camera = 'camera-' + domain
            observation = 'observation-' + domain
            profile = 'profile-' + domain
            add_persistent_gaussians(out, field, np.array([7], np.int64), times,
                                     [frame, frame], space=domain, clock=domain)
            add_camera(out, camera, [times[0]], np.array([[28., 0, 10], [0, 27, 6],
                       [0, 0, 1]])[None, None], np.eye(4)[None, None], [[20, 12]],
                       space=domain, clock=domain, pixel_center='half')
            images = [np.zeros((12, 20, 3), np.uint8)] * len(times)
            add_camera_rgb(out, observation, camera, [0, 0], [0, 0], images,
                           times=times, color_space='srgb')
            add_render_profile(out, profile, **profile_parameters())
            add_render_view(out, 'view-' + domain, camera, [field], profile,
                            observation_ids=[observation])
        add_render_view(out, 'preview-b', 'camera-b', ['field-b'], 'profile-b')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output')
    result=generate(parser.parse_args().output)
    print(json.dumps({'cases':len(result['cases']),'valid':sum(x['expected_sdk_valid'] for x in result['cases'])}))
