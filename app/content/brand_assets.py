from pathlib import Path

# Single resolution point for brand image assets used by the video
# pipeline. Scene renderers (app/content/scene_renderer.py) must never
# hardcode a brand asset path directly -- they call
# get_logo_asset_path()/get_logo_render_mode() here instead. Swapping
# in finalized brand assets later means editing ONLY this module,
# never storyboard_generator.py / scene_renderer.py /
# storyboard_composer.py.

# Finalized brand kit (app/dashboard/branding/{icons,logo,instagram,
# youtube}/) supplied to replace the old opaque icon-*-master.png
# placeholders -- verified directly: real alpha transparency (alpha
# channel spans 0-255), unlike the old placeholder's opaque near-white
# fill. The old icon-*-master.png / brand-sheet-original.png /
# logo-vertical-original.jpg files remain alongside these as historical
# working material, not used by get_logo_asset_path() anymore.
_LOGO_PATH = Path("app/dashboard/branding/icons/5squarefeed-icon-gradient.png")


def get_logo_asset_path() -> Path:
    return _LOGO_PATH


def get_logo_render_mode() -> str:
    """
    "cutout" (real alpha transparency -- paste directly with no
    border/box) vs "badge" (an opaque tile with its own background --
    render with a small border/box so it reads as a deliberate badge,
    not a stray screenshot). Scene renderers branch on this value so a
    future asset swap doesn't require rewriting compositing logic --
    only this function's return value changes.
    """
    return "cutout"
