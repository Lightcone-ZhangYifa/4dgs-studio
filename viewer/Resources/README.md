# Studio icon and localization

The editable vector icon and the ten-size macOS ICNS asset are generated from
the same Core Graphics artwork. From the repository root, regenerate them with:

```sh
icon_work="$(mktemp -d)"
xcrun swiftc viewer/tools/generate_app_icon.swift -O -o "$icon_work/generate-icon"
"$icon_work/generate-icon" "$icon_work/AppIcon.iconset" viewer/Resources/AppIcon.svg
iconutil -c icns "$icon_work/AppIcon.iconset" -o viewer/Resources/GSVStudio.icns
```

Localizable strings use the same semantic keys in both catalogs. Enumerate
computed keys in `DynamicKeys.json`. Run `python3 viewer/tools/audit_localization.py`
to check coverage, English fallbacks and format arguments. The build ships both
language bundles.
