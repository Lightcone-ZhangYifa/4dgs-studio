#!/usr/bin/env python3
"""Audit Studio's semantic string catalog without compiling or launching the app.

No machine translation or fallback catalog entries are generated. --extract
prints English literals missing from the English catalog for a human translator.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
QUOTED=r'"(?:\\.|[^"\\])*"'
CALL=re.compile(r'L10n\.(?:tr|format)\(\s*('+QUOTED+r')\s*,\s*('+QUOTED+r')',re.S)
ENTRY=re.compile(r'('+QUOTED+r')\s*=\s*('+QUOTED+r')\s*;')
FORMAT=re.compile(r'%(?:(\d+)\$)?[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(hh|ll|[hlqLzjt])?([@diuoxXfFeEgGaAcCsSp%])')

def catalog(path):
    if not path.exists():return {}
    text=path.read_text(); rows=ENTRY.findall(text); values={}
    for rawkey,rawvalue in rows:
        key,value=json.loads(rawkey),json.loads(rawvalue)
        if key in values:raise ValueError(f'Duplicate catalog key {key}: {path}')
        values[key]=value
    if not values:raise ValueError(f'No strings parsed: {path}')
    return values


def source_strings():
    values={}; locations={}; conflicts=[]
    for path in sorted((ROOT/'native').glob('*.swift')):
        text=path.read_text()
        for match in CALL.finditer(text):
            key,value=map(json.loads,match.groups())
            if '\\(' in key or '\\(' in value:raise ValueError(f'Interpolate arguments with L10n.format instead: {path}:{key}')
            if key in values and values[key]!=value:conflicts.append(f'{key}: {values[key]!r} / {value!r}')
            values[key]=value
            locations.setdefault(key,[]).append(f'{path.name}:{text.count(chr(10),0,match.start())+1}')
    dynamic=ROOT/'Resources/DynamicKeys.json'
    if dynamic.exists():
        for key,value in json.loads(dynamic.read_text()).items():
            if key in values and values[key]!=value:conflicts.append(f'{key}: source differs from dynamic key declaration')
            values[key]=value
    return values,locations,conflicts


def signature(value):
    result=[]; automatic=1
    for match in FORMAT.finditer(value):
        position,length,kind=match.groups()
        if kind=='%':continue
        index=int(position) if position else automatic
        if not position:automatic+=1
        result.append((index,(length or '')+kind))
    return sorted(result)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--extract',action='store_true')
    args=parser.parse_args()
    source,locations,conflicts=source_strings()
    catalogs={lang:catalog(ROOT/'Resources'/f'{lang}.lproj/Localizable.strings') for lang in ('en','zh-Hans')}
    if args.extract:
        print(json.dumps({key:value for key,value in source.items() if key not in catalogs['en']},ensure_ascii=False,indent=2));return 0
    errors=[f'Conflicting English fallback: {message}' for message in conflicts]
    for lang,values in catalogs.items():
        for key in sorted(set(values)-set(source)):errors.append(f'{lang}: unused catalog key {key}')
        for key in sorted(set(source)-set(values)):errors.append(f'{lang}: missing {key} ({", ".join(locations.get(key,[]))})')
        for key in sorted(set(catalogs['en'])-set(values)):errors.append(f'{lang}: catalog key missing {key}')
        for key in sorted(set(values)-set(catalogs['en'])):errors.append(f'{lang}: unexpected key {key}')
        for key,value in values.items():
            if not value.strip():errors.append(f'{lang}: empty {key}')
            english=catalogs['en'].get(key,source.get(key,''))
            if signature(value)!=signature(english):errors.append(f'{lang}: printf signature mismatch {key}: {signature(value)} != {signature(english)}')
    for key,value in source.items():
        if key in catalogs['en'] and catalogs['en'][key]!=value:errors.append(f'English source/catalog drift: {key}')
    print(json.dumps({'status':'failed' if errors else 'passed','source_keys':len(source),'catalog_keys':{k:len(v) for k,v in catalogs.items()},'errors':errors},ensure_ascii=False,indent=2))
    return int(bool(errors))

if __name__=='__main__':sys.exit(main())
