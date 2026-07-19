"""Small macOS integration helpers that must run before Qt initializes Cocoa."""

from __future__ import annotations

import sys


def configure_bundle_localizations(bundle=None) -> None:
    """Declare Chinese and English before AppKit chooses framework resources.

    Source launches inherit Homebrew Python.app's English-only bundle metadata.
    NSOpenPanel then stays English even when macOS prefers Simplified Chinese.
    The declaration must happen before QApplication initializes Cocoa because
    NSBundle caches its preferred localization on first use.
    """
    if bundle is None:
        if sys.platform != "darwin":
            return
        try:
            from Foundation import NSBundle
        except ImportError:
            return
        bundle = NSBundle.mainBundle()

    try:
        info = bundle.infoDictionary()
        info["CFBundleAllowMixedLocalizations"] = True
        info["CFBundleDevelopmentRegion"] = "zh-Hans"
        info["CFBundleLocalizations"] = ["zh-Hans", "en"]
    except Exception:
        pass
