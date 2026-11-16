"""4DGS Studio's native GTK frontend. GSV semantics live in the shared SDK."""
from pathlib import Path
import json
import plistlib

# The native bundle owns product identity on both platforms. Packaging records
# the same metadata locally so installed applications need no source tree.
_product = Path(__file__).with_name("product.json")
if _product.is_file():
    _metadata = json.loads(_product.read_text())
    VERSION = _metadata["version"]
    PRODUCT_IDENTITY = _metadata.get("identity", {})
else:
    with (Path(__file__).resolve().parents[2] / "native/Info.plist").open("rb") as stream:
        PRODUCT_IDENTITY = plistlib.load(stream)
    VERSION = PRODUCT_IDENTITY["CFBundleShortVersionString"]
APPLICATION_ID = "io.github.LightconeZhangYifa.GSVStudio"
