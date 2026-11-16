#!/usr/bin/env python3
"""Opt-in native desktop acceptance of one existing, read-only production GSV.

No synthetic source, source replacement, application internals or chooser mocks
are used. Every artifact/configuration file is written beneath a new --output
directory. --inventory-only reads source metadata without opening a desktop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'packages/gsv/src'), str(ROOT/'viewer/ubuntu')]


def source_state(path, *, checksum=False):
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('The production source must be a regular local file')
    result = {'device':value.st_dev, 'inode':value.st_ino, 'bytes':value.st_size,
              'mtime_ns':value.st_mtime_ns, 'ctime_ns':value.st_ctime_ns}
    if checksum:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(8*1024**2), b''):
                digest.update(block)
        result['sha256'] = digest.hexdigest()
    return result


def inventory(path, owner=None, identity=None):
    from gsv import Scene
    from gsv.container import HEADER
    source_state(path)
    with Scene(path, verify=False) as scene:
        objects = scene.objects
        fields = [obj for obj in objects if obj['type'] in ('gaussian','scaffold') and 'ids' in obj['data']]
        selected = next((obj for obj in fields if obj['id']==owner), None) if owner else next(iter(fields),None)
        if selected is None:
            raise ValueError('Choose --owner identifying a Gaussian/scaffold field with stored identities')
        data = selected['data']
        ids = scene.manifest['arrays'][data['ids']]
        if ids['dtype'] != 'i64' or len(ids['shape']) != 1 or ids['shape'][0] == 0:
            raise ValueError('Selected owner has no signed int64 identity table')
        if identity is None:
            buffer = scene.manifest['buffers'][ids['buffer']]
            raw = os.pread(scene._file.fileno(),8,buffer['offset']+ids.get('byte_offset',0))
            if len(raw)!=8:
                raise ValueError('Selected identity table was truncated')
            identity = struct.unpack('<q',raw)[0]
        times = scene.array(data['times']).copy().tolist() if 'times' in data else [0.]
        if not times or any(not math.isfinite(value) for value in times):
            raise ValueError('Selected owner has no finite native timestamps')
        header = HEADER.unpack(os.pread(scene._file.fileno(),HEADER.size,0))
        return {'path':str(path),'manifest_sha256':header[4].hex(),
                'object_count':len(objects), 'objects':[{'id':obj['id'],'label':obj.get('label',obj['id']),
                    'type':obj['type'],'encoding':obj['encoding'],'space':obj.get('space'),
                    'clock':obj.get('clock')} for obj in objects],
                'owner':selected['id'],'owner_label':selected.get('label',selected['id']),
                'identity':identity,'source_identities':ids['shape'][0],
                'clock':selected.get('clock'),'space':selected.get('space'),
                'clock_descriptor':scene.manifest.get('clocks',{}).get(selected.get('clock'),{}),
                'native_times':times,
                'validation_scope':'Metadata inventory only; native Open validates supported object semantics and checksums of accessed buffers, not every opaque payload'}


def process_resources(pid):
    values = {}
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith(('VmRSS:','VmHWM:','VmSize:','Threads:')):
            key,value,*unit = line.split()
            values[key.rstrip(':')] = int(value)*(1024 if unit and unit[0]=='kB' else 1)
    # comm may contain spaces/parentheses; fields after its final ')' begin at 3.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    values['cpu_seconds'] = (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
    return values


def code_hashes():
    names=('application.py','model.py','camera.py','viewport.py','gl_renderer.py','gpu_sort.py',
           'gpu_visibility.py')
    return {name:hashlib.sha256((ROOT/'viewer/ubuntu/gs_studio'/name).read_bytes()).hexdigest()
            for name in names}


def gpu_load():
    try:
        result=subprocess.run(['nvidia-smi','--query-gpu=index,name,utilization.gpu,memory.used,memory.total',
            '--format=csv,noheader'],capture_output=True,text=True,timeout=5,check=True)
        return {'scope':'Instantaneous host GPU load; no exclusive-load control','csv':result.stdout.strip()}
    except (OSError,subprocess.SubprocessError) as error:
        return {'unavailable':str(error)}


def run(args):
    import gi
    gi.require_version('Atspi','2.0')
    from gi.repository import Atspi, GLib
    from test_desktop_session import Desktop, VirtualOutput, action, pump, text, wait_for, walk
    from desktop_coordinates import activate_owned_x11, configure_existing_output
    from desktop_eis import EISInput
    from desktop_parity import edit, named, select_object, showing, tab

    if not args.allow_desktop_input:
        raise RuntimeError('Native acceptance requires --allow-desktop-input')
    desktop = Desktop()
    if desktop.owner_pid():
        raise RuntimeError('An existing Studio owns the application ID; refusing to attach or replace its source')
    source = args.scene.resolve(strict=True)
    before = source_state(source,checksum=args.hash_source)
    plan = inventory(source,args.owner,args.identity)
    output = args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    report = {'source':plan,'source_before':before,'backend':args.backend,'requested_prime':args.prime,'checks':[],
              'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
              'source_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
              'working_tree_changes':subprocess.check_output(['git','-C',str(ROOT),'status','--short'],text=True).splitlines(),
              'product_code_sha256':code_hashes(),
              'tool_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'workload_note':args.workload_note,'gpu_load_before':gpu_load(),
              'methodology':{
                  'source':'Opened exactly once through the native chooser, read-only; no document replacement',
                  'cache':'Inventory reads timestamps/one identity; optional full source hash warms OS cache. No cold-disk claim.',
                  'time_commit':'Native Return to public timeline Value update; not GPU completion/presentation',
                  'completed_png':'Seek to completed viewport PNG is an upper bound including portal automation, readback, encoding and file I/O',
                  'gpu':'Public completed GPU timing labels; asynchronous samples are not matched per-input latency or presentation FPS'},
              'initial_logical_monitors':desktop.monitor_count(),'resources':[],'artifacts':{}}
    desktop.backend = args.backend
    virtual = VirtualOutput(desktop)
    process = log = None
    stopped = threading.Event()
    sampler = None
    public_handles = {'timeline':None, 'gpu':None}
    def sample():
        while not stopped.is_set():
            try:report['resources'].append(dict(seconds=time.monotonic()-started,**process_resources(desktop.pid)))
            except OSError:break
            stopped.wait(.1)
    def check(identifier, operation):
        start=time.monotonic();observed=operation()
        report['checks'].append({'id':identifier,'passed':True,'seconds':time.monotonic()-start,'observed':observed})
        print('production desktop passed: '+identifier,flush=True)
        return observed
    def timeline():
        if public_handles['timeline'] is None:
            public_handles['timeline']=wait_for(lambda:desktop.find(lambda node:node.get_role_name()=='slider'
                and node.get_name().startswith('Timeline') and showing(node) and 'Value' in node.get_interfaces()),
                'public native-time timeline',args.timeout)
        public_handles['timeline'].clear_cache()
        return public_handles['timeline']
    def committed(value):
        node=timeline();node.clear_cache()
        return float(Atspi.Value.get_current_value(node))==value
    def public_stats():
        if public_handles['gpu'] is None:
            public_handles['gpu']=desktop.find(lambda node:showing(node) and
                node.get_role_name()=='label' and node.get_name().startswith('GPU '))
        node=public_handles['gpu']
        if node is None:return []
        node.clear_cache()
        return [{'text':node.get_name(),'description':node.get_description()}]
    def save_png(name,workspace=False):
        target=output/name
        desktop.native_save('workspace-snapshot' if workspace else 'screenshot',
                            'Save Workspace Snapshot…' if workspace else 'Save Screenshot…',target)
        data=target.read_bytes()
        if data[:8]!=b'\x89PNG\r\n\x1a\n':raise AssertionError('Native export is not PNG')
        dimensions=struct.unpack('>II',data[16:24])
        if min(dimensions)<=0:raise AssertionError('Native export has an empty raster')
        return {'path':target.name,'dimensions':list(dimensions),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    def seek(value,name):
        started_seek=time.monotonic()
        edit(desktop,'TIME',format(value,'.17g'))
        wait_for(lambda:committed(value),'native source time is published',args.timeout)
        published=time.monotonic()
        png=save_png(name)
        return {'native_time':value,'native_input_to_public_timeline_ms':(published-started_seek)*1000,
                'native_input_to_completed_png_ms_upper_bound':(time.monotonic()-started_seek)*1000,
                'png':png,'gpu_observation':public_stats()}
    def native_open():
        # Unicode production paths may contain characters absent from the active
        # XKB keymap. Edit the real native Location GtkText via AT-SPI, then use
        # native Return; do not fake a chooser completion or remap the keyboard.
        activate_owned_x11(desktop)
        desktop.window_action('open')
        dialog=wait_for(lambda:desktop.portal('Open GSV Scene'),'native production source chooser')
        desktop.chord(ord('l'),control=True)
        def location():
            return next((node for node in walk(dialog) if 'EditableText' in node.get_interfaces()
                         and node.get_name()!='Search' and node.get_state_set().contains(Atspi.StateType.FOCUSED)),None)
        entry=wait_for(location,'focused native Location entry',10)
        if not Atspi.EditableText.set_text_contents(entry,str(source)):
            raise RuntimeError('Native Location entry refused the production path')
        if text(entry)!=str(source):raise AssertionError('Native chooser path differs from requested source')
        desktop.chord(0xff0d)
        pump(.5)
        if desktop.portal('Open GSV Scene') is not None:
            # GNOME's Location Return can resolve/select the file without
            # accepting the chooser. Use its real enabled Open response next.
            opened=wait_for(lambda:desktop.button('Open',dialog),'native chooser Open response',10)
            def open_enabled():
                opened.clear_cache()
                return opened.get_state_set().contains(Atspi.StateType.SENSITIVE)
            wait_for(open_enabled,
                     'native chooser resolves the Unicode file selection',30)
            action(opened)
        try:
            wait_for(lambda:desktop.portal('Open GSV Scene') is None,'native source chooser closes',30)
        except RuntimeError:
            controls=[{'name':node.get_name(),'role':node.get_role_name(),
                       'description':node.get_description(),
                       'states':[state.value_nick for state in node.get_state_set().get_states()]}
                      for node in walk(dialog)]
            (output/'native-open-controls.json').write_text(json.dumps(controls,indent=2,ensure_ascii=False)+'\n')
            raise
        wait_for(lambda:desktop.find(lambda node:node.get_role_name()=='label'
                 and re.search(r'Gaussians.*t=',node.get_name())), 'validated production scene/frame ready',args.timeout)
        return {'native_path':str(source),'gpu_observation':public_stats()}
    started=time.monotonic()
    try:
        if report['initial_logical_monitors']==0:
            if not args.virtual_output:raise RuntimeError('No existing output; opt in with --virtual-output')
            virtual.start();report['temporary_virtual_output']=True
        desktop.keyboard_start()
        desktop.eis=EISInput.from_remote_session(desktop.bus,desktop.remote)
        report['input_devices']=desktop.eis.diagnostics()
        if not getattr(desktop,'virtual_size',None):
            report['coordinate_mapping']=configure_existing_output(desktop)
        env=os.environ.copy()
        env.update(GDK_BACKEND=args.backend,PYTHONPATH=str(ROOT/'viewer/ubuntu')+':'+str(ROOT/'packages/gsv/src')+
                   (':'+env['PYTHONPATH'] if env.get('PYTHONPATH') else ''),LANGUAGE='en_US',
                   XDG_CONFIG_HOME=str(output/'test-config'),XDG_CACHE_HOME=str(output/'test-cache'),
                   PYTHONDONTWRITEBYTECODE='1',__GL_SHADER_DISK_CACHE_PATH=str(output/'test-cache/nvidia'),
                   MESA_SHADER_CACHE_DIR=str(output/'test-cache/mesa'))
        env.pop('PYOPENGL_PLATFORM',None)
        if args.prime:env.update(__NV_PRIME_RENDER_OFFLOAD='1',__GLX_VENDOR_LIBRARY_NAME='nvidia',
            __EGL_VENDOR_LIBRARY_FILENAMES='/usr/share/glvnd/egl_vendor.d/10_nvidia.json')
        versions=subprocess.check_output([str(args.python),'-c',
            'import json,sys,numpy,OpenGL; print(json.dumps({"python":sys.version.split()[0],'
            '"numpy":numpy.__version__,"PyOpenGL":OpenGL.__version__,"PyOpenGL_file":OpenGL.__file__}))'],
            cwd=output,env=env,text=True)
        report['runtime_libraries']=json.loads(versions)
        log=(output/'application.log').open('w')
        process=subprocess.Popen([str(args.python),'-m','gs_studio'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        desktop.pid=process.pid;desktop.allow_close=True
        wait_for(lambda:desktop.owner_pid()==desktop.pid and desktop.window(),'normal Studio launch')
        action(desktop.window(),'window.toggle-maximized');desktop.maximized=True;pump(.4)
        report['pid']=desktop.pid
        sampler=threading.Thread(target=sample,daemon=True);sampler.start()
        check('native-open-production-source',native_open)
        timeline()  # One public-widget discovery; this document is never replaced.
        desktop.window_action('graphics-information')
        def graphics_body():
            return desktop.find(lambda node:node.get_role_name()=='label' and showing(node)
                and 'renderer:' in node.get_name().lower() and 'vendor:' in node.get_name().lower())
        graphics=wait_for(graphics_body,'public actual graphics backend information')
        report['graphics_information']=graphics.get_name()
        action(wait_for(lambda:desktop.button('OK'),'graphics information OK'))
        if args.prime and 'NVIDIA' not in report['graphics_information']:
            raise AssertionError('Requested NVIDIA PRIME but the actual graphics backend is different')
        first=float(Atspi.Value.get_minimum_value(timeline()))
        last=float(Atspi.Value.get_maximum_value(timeline()))
        times=[value for value in plan['native_times'] if first<=value<=last]
        if not times:raise AssertionError('Selected source owner has no samples in the active application clock')
        indices=sorted({round(index*(len(times)-1)/max(1,args.seek_count-1)) for index in range(args.seek_count)})
        for index in indices:
            check('native-seek-'+str(index),lambda index=index:seek(times[index],f'seek-{index}.png'))
        if last>first:
            def playback():
                edit(desktop,'TIME',format(first,'.17g'));wait_for(lambda:committed(first),'playback start frame',args.timeout)
                start=time.monotonic();desktop.window_action('play');observed=[]
                while time.monotonic()-start<args.duration:
                    pump(.1);observed.append(float(Atspi.Value.get_current_value(timeline())))
                desktop.window_action('play');pump(.3)
                duration=time.monotonic()-start
                if duration>args.duration+2:
                    raise AssertionError(f'Native playback sampling exceeded its bounded window: {duration:.3f}s > {args.duration+2:.3f}s')
                if not any(value!=first for value in observed):raise AssertionError('Production playback never advanced its displayed native time')
                paused=float(Atspi.Value.get_current_value(timeline()))
                pump(.3)
                paused_again=float(Atspi.Value.get_current_value(timeline()))
                if paused_again!=paused:
                    raise AssertionError('Native pause did not leave the displayed timeline stable')
                return {'requested_duration_seconds':args.duration,'duration_seconds':duration,
                        'observed_native_times':observed,'pause_confirmed_stable_time':paused,
                        'timeline_handle_reused':True,'gpu_observation':public_stats()}
            check('production-play-pause',playback)
        def selection():
            tab(desktop,'Selection');select_object(desktop,plan['owner_label'])
            edit(desktop,'Exact element ID',str(plan['identity']))
            def property_matches(title,value):
                return desktop.find(lambda node:node.get_role_name()=='list item' and showing(node)
                    and title in {child.get_name() for child in walk(node)}
                    and value in {child.get_name() for child in walk(node)})
            wait_for(lambda:property_matches('Exact element ID',str(plan['identity'])) and
                     property_matches('Object ID',plan['owner']),
                     'owner-qualified exact identity inspected',args.timeout)
            return {'owner':plan['owner'],'identity':plan['identity'],'png':save_png('selected-identity.png')}
        check('production-owner-and-exact-id',selection)
        def camera():
            import numpy as np
            from gsv import Scene
            edit(desktop,'TIME',format(times[0],'.17g'));wait_for(lambda:committed(times[0]),'camera first time',args.timeout)
            desktop.window_action('create-camera');desktop.allow_discard=True
            initial_pose=None
            if len(times)==1:
                before_orbit=output/'camera-before-orbit.gsv'
                desktop.native_save('save','Save Camera Animation',before_orbit)
                with Scene(before_orbit) as exported:
                    exported.validate();obj=exported.objects[0]
                    initial_pose=exported.array(obj['data']['pose']['camera_to_world'])[0,0].copy()
            desktop.orbit(args.backend)
            if len(times)>1:
                edit(desktop,'TIME',format(times[-1],'.17g'));wait_for(lambda:committed(times[-1]),'camera last time',args.timeout)
            desktop.window_action('record')
            target=output/'authored-cameras.gsv';desktop.native_save('save','Save Camera Animation',target)
            with Scene(target) as exported:
                exported.validate();obj=exported.objects[0];actual=exported.array(obj['data']['times']).copy().tolist()
                if actual!=sorted(set((times[0],times[-1]))):raise AssertionError('Native camera export changed the recorded times')
                poses=exported.array(obj['data']['pose']['camera_to_world']).copy()
            if initial_pose is None:initial_pose=poses[0,0]
            if np.allclose(initial_pose,poses[-1,0],rtol=0,atol=1e-7):
                raise AssertionError('Native middle-drag orbit did not change the exported camera pose')
            return {'path':target.name,'recorded_native_times':actual,
                    'native_middle_drag_verified_by_exported_c2w':True,
                    'first_pose':initial_pose.tolist(),'last_pose':poses[-1,0].tolist(),
                    'pointer_target':getattr(desktop,'pointer_target',None),
                    'workspace':save_png('workspace.png',True)}
        check('production-camera-record-export-and-workspace',camera)
        report['source_after']=source_state(source,checksum=args.hash_source)
        if before!=report['source_after']:raise AssertionError('Production source changed during acceptance')
        report['passed']=True
    except BaseException as error:
        report.update(passed=False,error=str(error),traceback=traceback.format_exc())
    finally:
        stopped.set()
        if sampler:sampler.join(timeout=2)
        try:desktop.close()
        except Exception as error:report.update(passed=False,cleanup_error=str(error))
        try:
            virtual.close();desktop.keyboard_stop()
            if report.get('temporary_virtual_output'):
                wait_for(lambda:desktop.monitor_count()==report['initial_logical_monitors'],'temporary output removal')
            report['final_logical_monitors']=desktop.monitor_count()
        except Exception as error:report.update(passed=False,output_cleanup_error=str(error))
        if log:log.close()
        report['gpu_load_after']=gpu_load()
        report['product_code_sha256_after']=code_hashes()
        report['product_code_unchanged']=report['product_code_sha256_after']==report['product_code_sha256']
        if not report['product_code_unchanged']:
            report.update(passed=False,code_changed_during_run=True)
        if 'source_after' not in report:
            try:
                report['source_after']=source_state(source,checksum=args.hash_source)
                if before!=report['source_after']:
                    report.update(passed=False,source_changed=True)
            except OSError as error:
                report.update(passed=False,source_check_error=str(error))
        if report['resources']:
            report['peak_sampled_RSS_bytes']=max(sample['VmRSS'] for sample in report['resources'])
            report['process_VmHWM_bytes']=max(sample['VmHWM'] for sample in report['resources'])
        (output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='New validation directory; existing directories are refused')
    parser.add_argument('--backend',choices=('x11','wayland'),default='x11')
    parser.add_argument('--python',type=Path,default=Path(sys.executable))
    parser.add_argument('--owner');parser.add_argument('--identity',type=int)
    parser.add_argument('--duration',type=float,default=5.)
    parser.add_argument('--seek-count',type=int,default=5)
    parser.add_argument('--timeout',type=float,default=180.)
    parser.add_argument('--prime',action='store_true')
    parser.add_argument('--virtual-output',action='store_true')
    parser.add_argument('--hash-source',action='store_true',help='Hash entire source before/after; warms OS cache before application open')
    parser.add_argument('--workload-note',default='',help='Operator-supplied concurrent workload context')
    parser.add_argument('--allow-desktop-input',action='store_true')
    parser.add_argument('--inventory-only',action='store_true')
    args=parser.parse_args()
    if args.seek_count<1 or not 0<args.duration<=300 or not 0<args.timeout<=3600:
        parser.error('Invalid seek count, duration or timeout')
    if args.identity is not None and not -(1<<63)<=args.identity<(1<<63):
        parser.error('--identity must be a signed int64 integer')
    if args.inventory_only:
        result=inventory(args.scene.resolve(strict=True),args.owner,args.identity)
        args.output.mkdir(parents=True,exist_ok=False)
        (args.output/'inventory.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    else:
        result=run(args)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0 if result.get('passed',args.inventory_only) else 1


if __name__=='__main__':
    raise SystemExit(main())
