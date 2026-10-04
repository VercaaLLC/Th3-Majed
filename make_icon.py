"""Generate AppIcon.icns for Th3 Majed — a gradient rounded-square badge
with a thermometer glyph, rendered natively via AppKit/Core Graphics.
No external tools beyond macOS's own `iconutil` (used after this script)."""
import os
from AppKit import (
    NSImage, NSBitmapImageRep, NSBitmapImageFileTypePNG, NSGraphicsContext,
    NSColor, NSBezierPath, NSMakeRect, NSFont, NSAttributedString, NSShadow,
    NSFontAttributeName, NSForegroundColorAttributeName, NSShadowAttributeName,
    NSParagraphStyleAttributeName, NSMutableParagraphStyle, NSCenterTextAlignment,
)
from Foundation import NSGradient

SIZES = [16, 32, 128, 256, 512]
OUT_DIR = os.path.expanduser("~/Th3Majed/AppIcon.iconset")
os.makedirs(OUT_DIR, exist_ok=True)


def render(size, scale):
    px = size * scale
    img = NSImage.alloc().initWithSize_((px, px))
    img.lockFocus()

    rect = NSMakeRect(0, 0, px, px)
    radius = px * 0.225  # macOS "squircle"-ish rounding
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
    path.addClip()

    top = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.20, 0.55, 1.0, 1.0)
    bottom = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.30, 0.80, 1.0)
    gradient = NSGradient.alloc().initWithStartingColor_endingColor_(top, bottom)
    gradient.drawInRect_angle_(rect, 90)

    # A bold, standalone "3" — the glyph from "Th3 Majed" itself, not a
    # borrowed emoji. Clean single-mark icon, Apple system font at Black
    # weight for a proper native look.
    glyph = "3"
    font_size = px * 0.62
    try:
        font = NSFont.systemFontOfSize_weight_(font_size, 0.70)  # NSFontWeightBlack-ish
    except Exception:
        font = NSFont.boldSystemFontOfSize_(font_size)
    style = NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(NSCenterTextAlignment)

    # subtle drop shadow for depth, matching modern macOS icon conventions
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.35))
    shadow.setShadowOffset_((0, -px * 0.012))
    shadow.setShadowBlurRadius_(px * 0.02)

    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.whiteColor(),
        NSParagraphStyleAttributeName: style,
        NSShadowAttributeName: shadow,
    }
    s = NSAttributedString.alloc().initWithString_attributes_(glyph, attrs)
    text_size = s.size()
    x = (px - text_size.width) / 2
    y = (px - text_size.height) / 2 - px * 0.015
    s.drawAtPoint_((x, y))

    img.unlockFocus()

    rep = NSBitmapImageRep.alloc().initWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    return png


for size in SIZES:
    for scale, suffix in [(1, ""), (2, "@2x")]:
        png = render(size, scale)
        name = f"icon_{size}x{size}{suffix}.png"
        with open(os.path.join(OUT_DIR, name), "wb") as f:
            f.write(bytes(png))
        print("wrote", name)
