"""Native desktop branding derived from the shared application identity.

The shared SVG is the only maintained artwork. The Ubuntu source tree refers
to it through a hicolor symlink; Debian builds rasterize that same SVG and put
it in the normal system icon theme. No user icon cache or setting is rewritten.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from . import APPLICATION_ID, PRODUCT_IDENTITY


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _https(value):
    value = _text(value)
    address = urlsplit(value)
    return value if (address.scheme == 'https' and address.hostname
                     and address.username is None and address.password is None) else ''


APPLICATION_NAME = (_text(PRODUCT_IDENTITY.get('CFBundleDisplayName'))
                    or _text(PRODUCT_IDENTITY.get('CFBundleName')) or '4DGS Studio')
_author = PRODUCT_IDENTITY.get('GSVStudioAuthor', {})
AUTHOR_NAME = _text(_author.get('Name'))
AUTHOR_NATIVE_NAME = _text(_author.get('NativeName'))
AUTHOR_DISPLAY_NAME = ' · '.join(dict.fromkeys(name for name in (AUTHOR_NAME, AUTHOR_NATIVE_NAME) if name))
AUTHOR_PROFILE_URL = _https(_author.get('ProfileURL'))
# This address identifies the Debian maintainer; it is not a support inbox.
AUTHOR_EMAIL = _text(_author.get('Email'))
REPOSITORY_URL = _https(_author.get('RepositoryURL'))
ISSUES_URL = _https(_author.get('IssuesURL'))
COPYRIGHT = _text(PRODUCT_IDENTITY.get('NSHumanReadableCopyright'))


def icon_search_paths():
    """Find source or relocated installed hicolor roots without CWD lookups."""
    module = Path(__file__).resolve()
    roots = (module.parents[1] / 'data/icons', module.parents[3] / 'icons')
    return tuple(root for root in roots
                 if (root / 'hicolor/scalable/apps' / (APPLICATION_ID + '.svg')).is_file())


def register_icons(display):
    """Expose the application icon to GTK in source and detached package runs."""
    if display is None:
        return
    from gi.repository import Gtk
    theme = Gtk.IconTheme.get_for_display(display)
    existing = set(theme.get_search_path())
    for root in icon_search_paths():
        if str(root) not in existing:
            theme.add_search_path(str(root))
