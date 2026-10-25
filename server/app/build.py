"""Application identity and supplied build metadata; no runtime Git dependency."""

import os
import re
from html import escape

APP_VERSION = "0.1.0"


def build_info():
    return {
        "version": APP_VERSION,
        "build_version": os.environ.get("STINTARA_BUILD_VERSION", "").strip() or "dev",
        "revision": os.environ.get("STINTARA_BUILD_REVISION", "").strip() or "unknown",
    }


def inject_build_label(html):
    info = build_info()
    revision = info["revision"]
    short = revision[:7] if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else revision
    label = escape(f"Stintara {info['build_version']} · {short}")
    # Only the footer marker before the script; a route parameter may contain the same text.
    return html.replace("__BUILD_LABEL__", label, 1)
