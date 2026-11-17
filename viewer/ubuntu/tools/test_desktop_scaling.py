#!/usr/bin/env python3
"""Opt-in normal-application checks at actual temporary Mutter monitor scales.

An externally owned, sole virtual output must already exist. This tool never
creates/removes outputs, never sets GDK_SCALE, and never persists a monitor
configuration. Fractional framebuffer scaling requires its explicit opt-in;
both the original temporary configuration and exact GSettings override are
restored in finally. Public AT-SPI, native EIS and native file dialogs drive the
normal application; no chooser completion or application object is mocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'viewer/ubuntu/tools'), str(ROOT/'viewer/ubuntu'), str(ROOT/'packages/gsv/src')]

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, Gio, GLib
from test_desktop_session import Desktop, action, pump, text, wait_for, walk, make_fixture
from desktop_coordinates import activate_owned_x11, configure_existing_output
from desktop_eis import EISInput


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def translations(language):
    path = ROOT/'viewer/Resources'/((language if language == 'zh-Hans' else 'en')+'.lproj/Localizable.strings')
    return {json.loads('"'+key+'"'):json.loads('"'+value+'"')
            for key,value in re.findall(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;',path.read_text())}


def state(bus):
    return bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
        'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', None, None, 0, 5000, None).unpack()


def current_output(value, connector):
    _, monitors, logical, properties = value
    if len(monitors) != 1 or len(logical) != 1 or len(logical[0][5]) != 1:
        raise RuntimeError('Only one externally owned virtual output is allowed')
    monitor = monitors[0]
    if monitor[0][0] != connector or not connector.startswith('Meta-'):
        raise RuntimeError('The sole output does not match the explicitly owned virtual connector')
    modes = [mode for mode in monitor[1] if mode[-1].get('is-current')]
    if len(modes) != 1:
        raise RuntimeError('The owned virtual output has no unique current mode')
    return monitor, modes[0], logical[0], properties


def apply(bus, connector, scale, layout_mode, *, mode_id=None):
    current = state(bus)
    _, mode, _, _ = current_output(current, connector)
    payload = [(0, 0, float(scale), 0, True, [(connector, mode_id or mode[0], {})])]
    # Method 1 applies only to this session; method 2 would persist user state.
    bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
        'org.gnome.Mutter.DisplayConfig', 'ApplyMonitorsConfig',
        GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})',
            (current[0], 1, payload, {'layout-mode': GLib.Variant('u', layout_mode)})),
        None, 0, 10000, None)
    wait_for(lambda: math.isclose(float(current_output(state(bus),connector)[2][2]),scale,
                                 abs_tol=1e-7), 'temporary desktop scale '+str(scale), 10)
    pump(.8)
    return state(bus)


def rect(node):
    value = Atspi.Component.get_extents(node, Atspi.CoordType.WINDOW)
    return [value.x, value.y, value.width, value.height]


def showing(node):
    return node is not None and node.get_state_set().contains(Atspi.StateType.SHOWING)


def contained(outer, inner):
    return (min(inner[2:]) > 0 and inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[0]+inner[2] <= outer[0]+outer[2]
            and inner[1]+inner[3] <= outer[1]+outer[3])


class ScalingDesktop(Desktop):
    """Retain the verified owned main frame across native modal dialogs."""
    def __init__(self, labels):
        super().__init__()
        self.labels = labels
        self.main_frame = None

    def window(self):
        if self.main_frame is not None:
            self.main_frame.clear_cache()
            if not self.main_frame.get_state_set().contains(Atspi.StateType.DEFUNCT):
                return self.main_frame
        result = super().window()
        if result is not None:
            self.main_frame = result
        return result

    def close(self):
        if self.pid and self.owner_pid() == self.pid and self.allow_close:
            self.call('io.github.LightconeZhangYifa.GSVStudio', '/io/github/LightconeZhangYifa/GSVStudio',
                'org.gtk.Actions', 'Activate', GLib.Variant('(sava{sv})', ('quit', [], {})))
            pump(.2)
            discard = self.button(self.labels.get('common.discard','Discard')) if self.allow_discard else None
            if discard:
                action(discard)
            wait_for(lambda:self.owner_pid()!=self.pid, 'owned scaled application shutdown',20)


def png(path):
    raw = path.read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise AssertionError('Native screenshot is not PNG')
    return {'file':path.name, 'dimensions':list(struct.unpack('>II',raw[16:24])),
            'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}


def run_case(args, case, source, output):
    backend, scale, language = case
    labels = translations(language)
    d = ScalingDesktop(labels)
    d.backend = backend
    if d.owner_pid():
        raise RuntimeError('Existing Studio instance; refusing to attach')
    folder = output/(backend+'-'+str(round(scale*100))+'-'+language)
    folder.mkdir()
    config = folder/'config/4dgs-studio'
    config.mkdir(parents=True)
    (config/'preferences.json').write_text(json.dumps({'language':language,'appearance':0}))
    env = os.environ.copy()
    for key in ('GDK_SCALE','GDK_DPI_SCALE','PYOPENGL_PLATFORM','LIBGL_ALWAYS_SOFTWARE',
                '__NV_PRIME_RENDER_OFFLOAD','__GLX_VENDOR_LIBRARY_NAME','__EGL_VENDOR_LIBRARY_FILENAMES'):
        env.pop(key,None)
    module_root=args.module_root.resolve()
    runtime_paths=[str(module_root)]
    if module_root==(ROOT/'viewer/ubuntu').resolve():
        runtime_paths.append(str(ROOT/'packages/gsv/src'))
    else:
        env['XDG_DATA_DIRS']=str(module_root.parents[1])+':'+env.get('XDG_DATA_DIRS','/usr/local/share:/usr/share')
    if env.get('PYTHONPATH'):
        runtime_paths.append(env['PYTHONPATH'])
    env.update(GDK_BACKEND=backend, LANGUAGE='en_US', XDG_CONFIG_HOME=str(folder/'config'),
               PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=':'.join(runtime_paths))
    if backend == 'x11':
        env.update(__NV_PRIME_RENDER_OFFLOAD='1',__GLX_VENDOR_LIBRARY_NAME='nvidia',
                   __EGL_VENDOR_LIBRARY_FILENAMES='/usr/share/glvnd/egl_vendor.d/10_nvidia.json')
    result = {'backend':backend,'desktop_scale':scale,'language':language,
              'checks':[],'passed':False,'artifacts':[],'pointer_input_tested':False}
    process = log = None

    def check(name, operation):
        started = time.monotonic()
        observed = operation()
        result['checks'].append({'id':name,'passed':True,'seconds':time.monotonic()-started,
                                 'observed':observed})
        print('desktop scale passed: '+backend+' '+str(scale)+' '+language+' '+name,flush=True)
        return observed

    def save(operation, title, name):
        path = folder/name
        d.native_save(operation,title,path)
        value = png(path)
        result['artifacts'].append(value)
        return value

    def public_timeline():
        return wait_for(lambda:d.find(lambda node:node.get_role_name()=='slider'
            and node.get_name()==labels['timeline.canvas'] and 'Value' in node.get_interfaces()
            and showing(node)), 'public source-time range')

    def layout():
        frame = rect(d.window())
        tabs = [node for node in d.app_nodes() if node.get_role_name()=='page tab' and showing(node)]
        expected = [labels['inspector.selection'],labels['inspector.viewport'],labels['inspector.document']]
        if [node.get_name() for node in tabs] != expected:
            raise AssertionError('Inspector language or page layout changed')
        tab_rects = [rect(node) for node in tabs]
        if not all(contained(frame,value) for value in tab_rects):
            raise AssertionError('A required inspector tab is clipped outside its window')
        entries = [node for node in d.app_nodes() if 'EditableText' in node.get_interfaces()
            and node.get_attributes().get('placeholder-text')=='TIME' and showing(node)]
        if len(entries)!=1 or not contained(frame,rect(entries[0])):
            raise AssertionError('Time entry is missing or clipped')
        candidates = [(node,rect(node)) for node in d.app_nodes()
            if node.get_role_name() in ('panel','filler','drawing area') and node.get_child_count()==0
            and 'Component' in node.get_interfaces() and showing(node)
            and node.get_state_set().contains(Atspi.StateType.FOCUSABLE)]
        candidates = [(node,value) for node,value in candidates if value[2]>300 and value[3]>180]
        if not candidates:
            raise AssertionError('The scaled viewport has no usable allocation')
        viewport, bounds = max(candidates,key=lambda item:item[1][2]*item[1][3])
        if not contained(frame,bounds):
            raise AssertionError('The viewport is clipped outside the window')
        timeline = rect(public_timeline())
        if not contained(frame,timeline):
            raise AssertionError('The timeline is clipped outside the window')
        return {'window':frame,'tabs':expected,'tab_rects':tab_rects,'time_entry':rect(entries[0]),
                'viewport':bounds,'timeline':timeline}

    try:
        probe="import json,gs_studio,gsv;print(json.dumps({'app':gs_studio.__file__,'sdk':gsv.__file__,'version':gs_studio.VERSION}))"
        origins=json.loads(subprocess.check_output([str(args.python),'-c',probe],cwd=folder,env=env,text=True))
        expected_sdk=(ROOT/'packages/gsv/src') if module_root==(ROOT/'viewer/ubuntu').resolve() else module_root
        if not Path(origins['app']).resolve().is_relative_to(module_root) or not Path(origins['sdk']).resolve().is_relative_to(expected_sdk):
            raise RuntimeError('Runtime import origin differs from the requested source/payload')
        result['runtime_origins']=origins
        d.keyboard_start()
        d.eis = EISInput.from_remote_session(d.bus,d.remote)
        try:
            result['coordinate_mapping'] = configure_existing_output(d)
        except (ValueError,OSError) as error:
            # Ambiguous fractional Xwayland coordinates must never be guessed.
            result['pointer_mapping_unavailable'] = str(error)
            d.pointer_stream = None
        log = (folder/'application.log').open('w')
        process = subprocess.Popen([str(args.python),'-m','gs_studio'],cwd=ROOT,env=env,
            stdout=log,stderr=subprocess.STDOUT)
        d.pid=process.pid
        d.allow_close=d.allow_discard=True
        result['pid']=process.pid
        wait_for(lambda:d.owner_pid()==process.pid and d.window(),'normal scaled Studio launch')
        action(d.window(),'window.toggle-maximized')
        d.maximized=True
        pump(.7)
        # The empty native status page contains the application branding.
        check('empty-workspace-branding-export',lambda:save('workspace-snapshot',
            labels['file.workspaceSnapshot'],'empty-workspace.png'))

        def open_source():
            activate_owned_x11(d)
            d.window_action('open')
            title=labels['file.openGSV']
            dialog=wait_for(lambda:d.portal(title),'native localized Open chooser')
            d.chord(ord('l'),control=True)
            entry=wait_for(lambda:next((n for n in walk(dialog) if 'EditableText' in n.get_interfaces()
                and n.get_name()!='Search' and n.get_state_set().contains(Atspi.StateType.FOCUSED)),None),
                'native Location entry')
            if not Atspi.EditableText.set_text_contents(entry,str(source)):
                raise AssertionError('Native chooser rejected the source path')
            d.chord(0xff0d)
            pump(.5)
            if d.portal(title) is not None:
                opened=wait_for(lambda:d.button('Open',dialog),'native Open response')
                wait_for(lambda:opened.get_state_set().contains(Atspi.StateType.SENSITIVE),'resolved source file')
                action(opened)
            wait_for(lambda:d.portal(title) is None,'native source chooser dismissal')
            node=public_timeline()
            wait_for(lambda:Atspi.Value.get_maximum_value(node)>Atspi.Value.get_minimum_value(node),
                     'loaded source-native timeline',60)
            return {'native_chooser':True,'time_min':Atspi.Value.get_minimum_value(node),
                    'time_max':Atspi.Value.get_maximum_value(node)}
        check('native-open-source',open_source)
        allocation=check('scaled-workbench-layout-and-localization',layout)
        result['allocation']=allocation

        def source_time():
            node=public_timeline()
            if not Atspi.Value.set_current_value(node,.5):
                raise AssertionError('Public native timeline rejected a source time')
            wait_for(lambda:float(Atspi.Value.get_current_value(node))==.5,'committed source time')
            return {'value':float(Atspi.Value.get_current_value(node)),
                    'method':'Native GTK AccessibleRange / public AT-SPI Value'}
        check('native-time-seek',source_time)

        def raster_sizes():
            viewport=save('screenshot',labels['file.screenshot'],'viewport.png')
            workspace=save('workspace-snapshot',labels['file.workspaceSnapshot'],'workspace.png')
            window=allocation['window'];view=allocation['viewport']
            factor_x=workspace['dimensions'][0]/window[2]
            factor_y=workspace['dimensions'][1]/window[3]
            if not math.isclose(factor_x,factor_y,abs_tol=1e-7) or not math.isclose(factor_x,round(factor_x),abs_tol=1e-7):
                raise AssertionError('Workspace PNG does not preserve one native integer GTK backing scale')
            factor=round(factor_x)
            if viewport['dimensions'] != [view[2]*factor,view[3]*factor]:
                raise AssertionError('Viewport raster differs from its native GTK backing allocation')
            return {'gtk_backing_scale':factor,'desktop_scale':scale,
                    'viewport':viewport,'workspace':workspace}
        check('native-backing-scale-snapshot-sizes',raster_sizes)

        def cancel_dialog():
            before=float(Atspi.Value.get_current_value(public_timeline()))
            d.window_action('open')
            title=labels['file.openGSV']
            wait_for(lambda:d.portal(title),'native scaled file chooser')
            d.chord(0xff1b)
            wait_for(lambda:d.portal(title) is None,'native Escape cancels file chooser')
            after=float(Atspi.Value.get_current_value(public_timeline()))
            if before!=after:
                raise AssertionError('Canceling the chooser changed document time')
            return {'real_eis_escape':True,'source_time_unchanged':True}
        check('native-dialog-and-keyboard-cancel',cancel_dialog)

        def about():
            main=d.window()
            d.window_action('about')
            def about_window():
                return next((node for node in d.app_nodes() if node!=main
                    and node.get_role_name() in ('frame','dialog','window') and showing(node)
                    and any('Yifa Zhang' in child.get_name() for child in walk(node))),None)
            dialog=wait_for(about_window,'native About author information')
            bounds=rect(dialog)
            nodes=list(walk(dialog))
            labels_present=[node.get_name() for node in nodes if showing(node)]
            if not any('张亦法' in value for value in labels_present):
                raise AssertionError('Native author name missing from About')
            controls=[node for node in nodes if node.get_role_name() in ('push button','toggle button') and showing(node)]
            clipped=[node.get_name() for node in controls if not contained(bounds,rect(node))]
            if clipped:
                raise AssertionError('About controls clipped: '+repr(clipped))
            images=[rect(node) for node in nodes if node.get_role_name() in ('image','icon') and showing(node)]
            d.chord(0xff1b)
            wait_for(lambda:not showing(dialog),'native Escape closes About')
            return {'author_names_visible':True,'about_bounds':bounds,'visible_image_rects':images,
                    'required_controls_within_window':True,'real_eis_escape':True}
        check('native-about-author-and-dialog-layout',about)

        def scientific():
            d.window_action('compare')
            wait_for(lambda:d.find(lambda node:node.get_role_name()=='label' and showing(node)
                and 'PSNR' in node.get_name()),'completed calibrated comparison',60)
            value=save('screenshot',labels['file.screenshot'],'comparison.png')
            if value['dimensions'] != [1280,480]:
                raise AssertionError('Scientific comparison raster changed with desktop DPI')
            d.window_action('compare')
            return value
        check('scientific-raster-independent-of-desktop-scale',scientific)

        def pointer_orbit():
            if 'coordinate_mapping' not in result:
                return {'tested':False,'reason':'No unambiguous published native pointer mapping; native EIS keyboard checks passed separately'}
            from gsv import Scene
            import numpy as np
            def camera_pose(name):
                path=folder/name
                d.native_save('save',labels['camera.saveTitle'],path)
                with Scene(path) as scene:
                    scene.validate()
                    camera=scene.objects[0]
                    return scene.array(camera['data']['pose']['camera_to_world']).copy()
            d.window_action('create-camera')
            before=camera_pose('orbit-before.gsv')
            try:
                d.orbit(backend)
            except RuntimeError as error:
                if 'ambiguous' in str(error) or 'mapping' in str(error):
                    return {'tested':False,'reason':str(error),'no_pointer_events_sent':True}
                raise
            d.window_action('record')
            after=camera_pose('orbit-after.gsv')
            if np.array_equal(before,after):
                raise AssertionError('Scaled native pointer orbit did not change the exported camera pose')
            result['pointer_input_tested']=True
            return {'tested':True,'real_middle_drag':True,'exported_camera_pose_changed':True,
                    'verified_pointer_target':d.pointer_target}
        check('native-pointer-orbit-when-mapping-is-unambiguous',pointer_orbit)
        result['passed']=True
    except BaseException as error:
        result.update(error=str(error),traceback=traceback.format_exc())
    finally:
        try:
            d.close()
            if process is not None:
                process.wait(timeout=20)
        except Exception as error:
            result.update(passed=False,cleanup_error=str(error))
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        d.keyboard_stop()
        if log:
            log.close()
        result['remaining_studio_owner']=d.owner_pid()
        if result['remaining_studio_owner'] is not None:
            result['passed']=False
        (folder/'report.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='NEW evidence directory')
    parser.add_argument('--connector',required=True,help='Explicit externally owned sole virtual connector')
    parser.add_argument('--case',action='append',required=True,help='backend:desktop-scale:language, e.g. wayland:1.25:zh-Hans')
    parser.add_argument('--python',type=Path,default=ROOT/'.venv/bin/python')
    parser.add_argument('--module-root',type=Path,default=ROOT/'viewer/ubuntu',help='Source modules or detached deb ubuntu module directory')
    parser.add_argument('--allow-desktop-input',action='store_true')
    parser.add_argument('--allow-output-configuration',action='store_true')
    parser.add_argument('--allow-experimental-scaling',action='store_true')
    args=parser.parse_args()
    if not args.allow_desktop_input or not args.allow_output_configuration:
        parser.error('Actual native input and temporary owned-output configuration require explicit opt-ins')
    cases=[]
    for value in args.case:
        backend,scale,language=value.split(':')
        scale=float(scale)
        if backend not in ('wayland','x11') or language not in ('en','zh-Hans') or not math.isfinite(scale) or not 1<=scale<=3:
            parser.error('Invalid case '+value)
        cases.append((backend,scale,language))
    if len(set(cases)) != len(cases):
        parser.error('Duplicate scaling cases')
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    d=Desktop()
    if d.owner_pid():
        raise RuntimeError('Existing Studio instance; refusing scaling configuration')
    bus=d.bus
    initial=state(bus)
    _,initial_mode,initial_logical,initial_properties=current_output(initial,args.connector)
    if initial_logical[0:2] not in ((0,0),[0,0]) or initial_logical[3]!=0:
        raise RuntimeError('Owned output must have origin zero and no rotation')
    settings=Gio.Settings.new('org.gnome.mutter')
    original_feature_override=settings.get_user_value('experimental-features')
    original_features=settings.get_value('experimental-features').unpack()
    monitor_file=Path.home()/'.config/monitors.xml'
    monitor_file_before=digest(monitor_file) if monitor_file.exists() else None
    source=output/'scaling-fixture.gsv'
    make_fixture(source)
    source_before=digest(source)
    report={'format':'gsv.native-desktop-scaling/1','passed':False,'cases':[],
        'source_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
        'working_tree_changes':subprocess.check_output(['git','-C',str(ROOT),'status','--short'],text=True).splitlines(),
        'tool_sha256':digest(__file__),'initial_display_state':initial,
        'initial_experimental_features':original_features,'initial_feature_override':original_feature_override.unpack() if original_feature_override else None,
        'source_sha256':source_before,'methodology':'Real normal application, native AT-SPI/EIS and portals; actual Mutter monitor scale, no GDK_SCALE override. Screenshots are the owned application only.'}
    report['runtime_module_root']=str(args.module_root.resolve())
    report['runtime_sha256']={str(path.relative_to(args.module_root)):digest(path)
                             for path in sorted(args.module_root.glob('gs_studio/*.py'))}
    try:
        fractional=any(not scale.is_integer() for _,scale,_ in cases)
        if fractional and 'scale-monitor-framebuffer' not in original_features:
            if not args.allow_experimental_scaling:
                raise RuntimeError('Fractional desktop scaling requires explicit temporary experimental-feature opt-in')
            settings.set_strv('experimental-features',[*original_features,'scale-monitor-framebuffer'])
            Gio.Settings.sync()
            wait_for(lambda:state(bus)[3].get('layout-mode')==1,'logical framebuffer scaling mode',10)
        layout=1 if fractional or 'scale-monitor-framebuffer' in original_features else int(initial_properties['layout-mode'])
        for case in cases:
            backend,scale,language=case
            current=state(bus)
            _,mode,_,_=current_output(current,args.connector)
            if not any(math.isclose(scale,float(value),abs_tol=1e-7) for value in mode[5]):
                raise RuntimeError(f'Mutter does not advertise desktop scale {scale}; supported={mode[5]}')
            display=apply(bus,args.connector,scale,layout)
            item=run_case(args,case,source,output)
            item['display_state']=display
            report['cases'].append(item)
            if not item['passed']:
                raise RuntimeError('Native scaling case failed: '+backend+' '+str(scale)+' '+language)
        comparisons=[next(c['observed'] for c in row['checks'] if c['id']=='scientific-raster-independent-of-desktop-scale') for row in report['cases']]
        report['scientific_png_identical_across_scales']=len({row['sha256'] for row in comparisons})==1
        if not report['scientific_png_identical_across_scales']:
            raise AssertionError('Scientific output changed across desktop scale/backend/language')
        report['passed']=True
    except BaseException as error:
        report.update(error=str(error),traceback=traceback.format_exc())
    finally:
        try:
            if original_feature_override is None:
                settings.reset('experimental-features')
            else:
                settings.set_value('experimental-features',original_feature_override)
            Gio.Settings.sync()
            pump(.5)
            apply(bus,args.connector,float(initial_logical[2]),int(initial_properties['layout-mode']),mode_id=initial_mode[0])
            final=state(bus)
            report['final_display_state']=final
            report['final_experimental_features']=settings.get_value('experimental-features').unpack()
            report['final_feature_override']=settings.get_user_value('experimental-features').unpack() if settings.get_user_value('experimental-features') else None
            report['display_configuration_restored']=(final[1:]==initial[1:])
            report['feature_override_restored']=(report['final_feature_override']==report['initial_feature_override'])
            report['monitors_xml_unchanged']=(digest(monitor_file) if monitor_file.exists() else None)==monitor_file_before
            report['source_unchanged']=digest(source)==source_before
            report['runtime_unchanged']=report['runtime_sha256']=={
                str(path.relative_to(args.module_root)):digest(path)
                for path in sorted(args.module_root.glob('gs_studio/*.py'))}
            report['remaining_studio_owner']=d.owner_pid()
            if not all(report[key] for key in ('display_configuration_restored','feature_override_restored','monitors_xml_unchanged','source_unchanged','runtime_unchanged')) or report['remaining_studio_owner'] is not None:
                report['passed']=False
        except BaseException as error:
            report.update(passed=False,restore_error=str(error),restore_traceback=traceback.format_exc())
        (output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str)+'\n')
    print(json.dumps({'passed':report['passed'],'output':str(output),'cases_completed':len(report['cases']),
                      'error':report.get('error'),'restore_error':report.get('restore_error')}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
