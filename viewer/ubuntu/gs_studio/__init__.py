"""4DGS Studio's native GTK frontend. GSV semantics live in the shared SDK."""
from pathlib import Path
import json
import plistlib

# The source tree has one canonical product version. Debian packaging records
# that value locally so installed applications do not depend on a source tree.
_product = Path(__file__).with_name("product.json")
if _product.is_file():
    VERSION = json.loads(_product.read_text())["version"]
else:
    with (Path(__file__).resolve().parents[2] / "native/Info.plist").open("rb") as stream:
        VERSION = plistlib.load(stream)["CFBundleShortVersionString"]
APPLICATION_ID = "io.github.LightconeZhangYifa.GSVStudio"
