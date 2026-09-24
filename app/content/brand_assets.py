from pathlib import Path

# Single resolution point for brand image assets used by the video
# pipeline. Scene renderers (app/content/scene_renderer.py) must never
# hardcode a brand asset path directly -- they call
# get_logo_asset_path()/get_logo_render_mode() here instead. Swapping
# in finalized brand assets later (real alpha transparency, a
# purpose-cut mark, etc.) means editing ONLY this module, never
# storyboard_generator.py / scene_renderer.py / storyboard_composer.py.

# TEMPORARY PLACEHOLDER: app/dashboard/branding/icon-gradient-master.png
# is an opaque, full-bleed square icon tile (verified directly: no
# real alpha transparency -- its background is a near-white fill, not
# transparent), used only as a stand-in until finalized brand assets
# are supplied separately. This constant is the only thing that needs
# to change to update branding across every scene render.
_PLACEHOLDER_LOGO_PATH = Path("app/dashboard/branding/icon-gradient-master.png")


def get_logo_asset_path() -> Path:
    return _PLACEHOLDER_LOGO_PATH


def get_logo_render_mode() -> str:
    """
    "badge" (today: an opaque tile with its own background -- render
    with a small border/box so it reads as a deliberate badge, not a
    stray screenshot) vs a future "cutout" (real alpha transparency --
    paste directly with no border/box). Scene renderers branch on this
    value so a later asset swap with real transparency doesn't require
    rewriting compositing logic -- only this function's return value
    changes.
    """
    return "badge"
