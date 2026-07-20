#!/usr/bin/env python3
"""Generate the floor's isometric sprite kit (site/assets/**.svg).

Every graphic on the factory floor comes out of this file, deterministically,
in one shared projection and palette — that is the extension contract:

  * projection: 2:1 dimetric. Local grid is 24x24 footprint units;
    pt(x,y,z) -> screen. 1 unit = 2px horizontal, 1px vertical, z*2 tall.
  * decor sprites: 128x128 viewBox, footprint diamond anchored at (64,104)
    (south corner). The renderer scales sprites by room size and pins this
    anchor to the room's front tile corner.
  * workers: 32x40 viewBox, feet anchored at (16,38).
  * to add a room kind for a new node: write one `def deco_<name>(s)` below,
    add its keyword to ARCHETYPE_RULES in app.py (or CORE_ROOMS), rerun
    `python3 tools/gen_assets.py`. Nothing else. Unknown kinds fall back to
    the generic `workshop` sprite, so a new service is never invisible.

Run from the app root:  python3 tools/gen_assets.py
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "site" / "assets"

# --- palette (see site/assets/ASSETS.md) -------------------------------------
WOOD = "#b98a54"
WOOD_DARK = "#8a6238"
METAL = "#8d99ae"
STEEL = "#5b667e"
IRON = "#3f485c"
BRASS = "#d9a441"
GLOW = "#ffd166"
SCREEN = "#7ce0d3"
RED = "#ef6461"
GREEN = "#8ac926"
PURPLE = "#9b5de5"
BLUE = "#4ea8de"
PAPER = "#f4ede4"
INK = "#2b2338"


def shade(hex_color, f):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(min(255, int(c * f)) for c in (r, g, b))


def pt(x, y, z=0):
    return (64 + (x - y) * 2, 104 - (x + y) - z * 2)


def poly(points, fill, opacity=None):
    pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
    op = f' fill-opacity="{opacity}"' if opacity is not None else ""
    return f'<polygon points="{pts}" fill="{fill}"{op}/>'


class Sprite:
    """Collects iso boxes + raw overlay elements, then emits one SVG."""

    def __init__(self, view="0 0 128 128"):
        self.view = view
        self.boxes = []   # (sortkey, [svg strings])
        self.over = []    # drawn after all boxes

    def box(self, x, y, z, w, d, h, color):
        top = [pt(x, y, z + h), pt(x + w, y, z + h),
               pt(x + w, y + d, z + h), pt(x, y + d, z + h)]
        se = [pt(x, y, z), pt(x + w, y, z),
              pt(x + w, y, z + h), pt(x, y, z + h)]
        sw = [pt(x, y, z), pt(x, y + d, z),
              pt(x, y + d, z + h), pt(x, y, z + h)]
        faces = [poly(se, shade(color, 0.68)), poly(sw, shade(color, 0.88)),
                 poly(top, shade(color, 1.12))]
        # painter's algorithm: farther (bigger x+y) first, lower z first
        self.boxes.append((-(x + y) * 100 + z, faces))
        return self

    def raw(self, svg):
        self.over.append(svg)
        return self

    def shadow(self):
        self.over.insert(0, '<ellipse cx="64" cy="80" rx="47" ry="24" '
                            'fill="#000" fill-opacity="0.13"/>')
        return self

    def write(self, path):
        body = []
        if self.over and self.over[0].startswith("<ellipse"):
            body.append(self.over.pop(0))
        for _, faces in sorted(self.boxes, key=lambda b: b[0]):
            body.extend(faces)
        body.extend(self.over)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{self.view}">'
               + "".join(body) + "</svg>")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg + "\n")


def screen_quad(x, y, z, w, h, color=SCREEN):
    """A glowing panel on the SE face plane (for monitors, signs)."""
    return poly([pt(x, y, z), pt(x + w, y, z), pt(x + w, y, z + h), pt(x, y, z + h)], color)


# --- decor archetypes ---------------------------------------------------------
# Each function receives a fresh Sprite with shadow, composes it, returns it.

def deco_workshop(s):
    s.box(2, 18, 0, 20, 3, 15, WOOD_DARK)                    # tool wall
    s.box(4, 6, 0, 16, 8, 6, WOOD)                           # bench
    s.box(16, 2, 0, 6, 5, 5, WOOD_DARK)                      # crate
    s.raw(screen_quad(5, 18, 4, 6, 5, BRASS))                # pegboard tools
    s.raw(screen_quad(13, 18, 6, 4, 4, RED))
    return s


def deco_gatehouse(s):
    s.box(1, 4, 0, 6, 16, 20, STEEL)                         # west pillar
    s.box(17, 4, 0, 6, 16, 20, STEEL)                        # east pillar
    s.box(1, 4, 20, 22, 16, 4, IRON)                         # lintel
    s.raw(screen_quad(7, 8, 0, 10, 14, shade(BRASS, 0.8)))   # the door
    s.raw('<circle cx="64" cy="42" r="5" fill="%s"/>' % GLOW)  # lantern
    s.raw(screen_quad(8.5, 8, 6, 7, 1.5, INK))               # door bands
    return s


def deco_identity(s):
    s.box(3, 6, 0, 18, 8, 7, WOOD)                           # desk
    s.box(6, 16, 0, 12, 4, 16, STEEL)                        # key totem
    s.raw('<circle cx="64" cy="52" r="7" fill="none" stroke="%s" stroke-width="4"/>' % BRASS)
    s.raw('<rect x="61" y="58" width="6" height="14" fill="%s"/>' % BRASS)
    s.raw('<rect x="64" y="66" width="7" height="3" fill="%s"/>' % BRASS)
    return s


def deco_stamp(s):
    s.box(5, 8, 0, 14, 7, 6, WOOD)
    s.box(9, 10, 6, 5, 4, 4, BRASS)                          # the stamp
    s.raw(screen_quad(15, 8, 7, 3, 2, PAPER))
    return s


def deco_reactor(s):
    s.box(2, 2, 0, 20, 20, 3, IRON)                          # plinth
    s.box(6, 6, 3, 12, 12, 16, STEEL)                        # core housing
    s.box(9, 9, 19, 6, 6, 4, IRON)                           # cap
    s.box(1, 16, 0, 4, 4, 12, METAL)                         # pipe
    s.box(19, 1, 0, 4, 4, 9, METAL)                          # pipe
    s.raw('<ellipse cx="64" cy="66" rx="13" ry="6.5" fill="none" '
          'stroke="%s" stroke-width="3" class="glow"/>' % GLOW)
    s.raw(screen_quad(8, 6, 8, 8, 6, shade(GLOW, 1.0)))      # inspection window
    return s


def deco_vault(s):
    s.box(3, 3, 0, 18, 18, 12, IRON)
    s.raw('<circle cx="64" cy="82" r="8" fill="%s"/>' % shade(IRON, 1.3))
    s.raw('<circle cx="64" cy="82" r="4.5" fill="none" stroke="%s" stroke-width="2.5"/>' % BRASS)
    s.raw('<circle cx="64" cy="82" r="1.5" fill="%s"/>' % BRASS)
    return s


def deco_archive(s):
    s.box(2, 14, 0, 9, 8, 20, WOOD_DARK)                     # tall shelf west
    s.box(13, 14, 0, 9, 8, 16, WOOD_DARK)                    # shelf east
    s.box(4, 2, 0, 14, 6, 4, METAL)                          # sorting conveyor
    for i, c in enumerate([RED, GREEN, BLUE, BRASS, PURPLE, SCREEN]):
        s.raw(screen_quad(3 + (i % 3) * 2.6, 14, 6 + (i // 3) * 6, 2, 4, c))  # spines
    for i, c in enumerate([PAPER, BRASS, PAPER]):
        s.raw(screen_quad(14 + i * 2.6, 14, 5, 2, 3.5, c))
    return s


def deco_catalog(s):
    s.box(5, 10, 0, 14, 8, 12, WOOD)
    for r in range(3):
        for c in range(3):
            s.raw(screen_quad(6.5 + c * 4.2, 10, 1.5 + r * 3.6, 3, 2, WOOD_DARK))
    s.raw(screen_quad(8, 10, 12.5, 8, 2.5, PAPER))           # open drawer card
    return s


def deco_console(s):
    s.box(3, 16, 0, 18, 4, 12, IRON)                         # monitor wall
    s.box(4, 6, 0, 16, 6, 5, STEEL)                          # desk
    s.raw(screen_quad(4.5, 16, 5.5, 5, 4.5, SCREEN))
    s.raw(screen_quad(10.5, 16, 5.5, 5, 4.5, GREEN))
    s.raw(screen_quad(16.5, 16, 5.5, 4, 4.5, BRASS))
    s.raw(screen_quad(6, 16, 1, 12, 3, shade(SCREEN, 0.6)))
    return s


def deco_periscope(s):
    s.box(7, 7, 0, 10, 10, 8, STEEL)
    s.box(10, 10, 8, 4, 4, 10, METAL)                        # tube
    s.box(8, 12, 18, 8, 4, 3, METAL)                         # eyepiece
    s.raw('<circle cx="57" cy="47" r="2.5" fill="%s"/>' % SCREEN)
    return s


def deco_bell(s):
    s.box(10, 10, 0, 4, 4, 14, WOOD_DARK)                    # post
    s.raw('<path d="M56 52 q8 -12 16 0 l2 4 h-20 z" fill="%s"/>' % BRASS)
    s.raw('<circle cx="64" cy="58" r="2" fill="%s"/>' % shade(BRASS, 0.6))
    return s


def deco_workbench(s):
    s.box(2, 16, 0, 20, 4, 12, STEEL)                        # parts rack
    s.box(3, 5, 0, 18, 8, 6, WOOD)                           # bench
    s.box(8, 8, 6, 3, 3, 5, METAL)                           # arm base
    s.raw('<path d="M60 62 l8 -8 l6 3" stroke="%s" stroke-width="3" fill="none"/>' % METAL)
    s.raw('<path d="M74 55 l3 3 m0 -3 l-3 3" stroke="%s" stroke-width="2"/>' % GLOW)  # weld spark
    s.raw(screen_quad(4, 16, 3, 5, 4, SCREEN))
    return s


def deco_frontdesk(s):
    s.box(3, 5, 0, 18, 7, 7, WOOD)
    s.box(17, 13, 0, 5, 5, 4, WOOD_DARK)                     # side table
    s.raw('<path d="M58 60 a7 7 0 0 1 12 0" stroke="%s" stroke-width="3" fill="none"/>' % BLUE)
    s.raw('<circle cx="58" cy="61" r="2.5" fill="%s"/>' % BLUE)   # headset
    s.raw('<circle cx="70" cy="61" r="2.5" fill="%s"/>' % BLUE)
    s.raw(screen_quad(6, 5, 7, 5, 3.5, SCREEN))
    return s


def deco_dock(s):
    s.box(2, 2, 0, 20, 20, 2, shade(WOOD, 0.9))              # platform
    s.box(2, 16, 2, 20, 6, 16, STEEL)                        # gate housing
    for i in range(5):
        s.raw(screen_quad(4 + i * 3.4, 16, 3, 2.6, 11, shade(METAL, 1.0 - i * 0.04)))
    s.box(15, 4, 2, 5, 4, 4, WOOD_DARK)                      # waiting parcel
    return s


def deco_worldgate(s):
    s.box(1, 6, 0, 5, 12, 22, IRON)
    s.box(18, 6, 0, 5, 12, 22, IRON)
    s.box(1, 6, 22, 22, 12, 3, STEEL)
    s.raw('<circle cx="64" cy="62" r="12" fill="%s" fill-opacity="0.9"/>' % BLUE)
    s.raw('<path d="M52 62 h24 M64 50 v24 M55 54 q9 6 18 0 M55 70 q9 -6 18 0" '
          'stroke="%s" stroke-width="1.6" fill="none"/>' % PAPER)   # the globe
    return s


def deco_pad(s):
    s.box(2, 2, 0, 20, 20, 2, STEEL)
    for i in range(4):
        s.raw(poly([pt(3 + i * 5, 2, 2), pt(6 + i * 5, 2, 2),
                    pt(4 + i * 5, 0, 2), pt(1 + i * 5, 0, 2)], BRASS))  # hazard chevrons
    s.raw('<circle cx="64" cy="80" r="7" fill="none" stroke="%s" stroke-width="2" '
          'stroke-dasharray="4 3"/>' % GLOW)                 # landing ring
    return s


def deco_calendar(s):
    s.box(3, 16, 0, 18, 3, 16, WOOD_DARK)
    s.raw(screen_quad(4.5, 16, 3, 15, 11, PAPER))
    for r in range(3):
        for c in range(5):
            s.raw(screen_quad(5.5 + c * 2.8, 16, 4.5 + r * 2.8, 2, 1.8,
                              RED if (r, c) == (1, 2) else shade(PAPER, 0.85)))
    s.raw(screen_quad(4.5, 16, 12.2, 15, 1.8, RED))          # month band
    s.box(8, 6, 0, 8, 6, 4, WOOD)
    return s


def deco_antenna(s):
    s.box(8, 8, 0, 8, 8, 4, STEEL)
    s.box(11, 11, 4, 2, 2, 16, METAL)                        # mast
    s.raw('<path d="M64 46 l10 -7 l-2 9 z" fill="%s"/>' % METAL)   # dish
    s.raw('<path d="M76 38 a10 10 0 0 1 6 8 M79 33 a15 15 0 0 1 9 12" '
          'stroke="%s" stroke-width="2" fill="none"/>' % GLOW)     # waves
    s.box(3, 4, 0, 5, 4, 3, WOOD_DARK)                       # parcel of prints
    return s


def deco_pinboard(s):
    s.box(3, 16, 0, 18, 3, 14, WOOD)
    s.raw(screen_quad(4.5, 16, 2.5, 15, 9.5, shade(WOOD, 1.25)))
    for i, c in enumerate([PAPER, SCREEN, BRASS, PAPER, GREEN]):
        s.raw(screen_quad(6 + (i % 3) * 4.4, 16, 4 + (i // 3) * 4.4, 3, 3, c))
    s.box(9, 6, 0, 7, 5, 4, WOOD_DARK)                       # stool
    return s


def deco_switchboard(s):
    s.box(3, 14, 0, 18, 6, 15, IRON)
    for r in range(3):
        for c in range(6):
            s.raw('<circle cx="%.1f" cy="%.1f" r="1.3" fill="%s"/>' %
                  (48 + c * 5.4, 56 + r * 6 + c * 0.0, BRASS))
    s.raw('<path d="M50 62 q8 10 20 4 M56 56 q10 12 16 12" stroke="%s" '
          'stroke-width="1.8" fill="none"/>' % RED)          # patch cords
    s.box(6, 4, 0, 12, 5, 5, WOOD)                           # operator desk
    return s


def deco_arcade(s):
    s.box(6, 8, 0, 12, 9, 16, PURPLE)
    s.raw(screen_quad(7.5, 8, 8, 9, 5.5, SCREEN))
    s.raw('<rect x="59" y="86" width="3" height="2.5" fill="%s"/>' % GREEN)
    s.raw('<circle cx="70" cy="86" r="1.8" fill="%s"/>' % RED)     # buttons
    s.raw(screen_quad(7.5, 8, 14.5, 9, 1.6, GLOW))           # marquee
    return s


def deco_radar(s):
    s.box(4, 6, 0, 16, 8, 6, STEEL)
    s.raw(screen_quad(5.5, 6, 1.5, 13, 4, shade(GREEN, 0.55)))
    s.raw('<circle cx="64" cy="88" r="0.1" fill="none"/>')
    s.box(9, 15, 0, 6, 6, 8, METAL)
    s.raw('<ellipse cx="61" cy="53" rx="9" ry="4.5" fill="none" stroke="%s" '
          'stroke-width="2"/>' % GREEN)                      # dish rim
    s.raw('<path d="M61 53 l7 -3" stroke="%s" stroke-width="2"/>' % GLOW)  # sweep
    return s


def deco_mailroom(s):
    s.box(3, 14, 0, 18, 5, 14, WOOD)
    for r in range(3):
        for c in range(4):
            s.raw(screen_quad(4.8 + c * 4, 14, 2 + r * 3.8, 3.2, 3,
                              shade(WOOD, 0.65)))
    s.raw(screen_quad(9, 14, 6, 3.2, 3, PAPER))              # one letter showing
    s.box(14, 4, 0, 6, 5, 3, METAL)                          # mail cart
    return s


def deco_drafting(s):
    s.box(4, 8, 0, 16, 8, 5, WOOD)
    s.raw(poly([pt(5, 8, 5), pt(19, 8, 5), pt(19, 15, 11), pt(5, 15, 11)], BLUE))  # tilted board
    s.raw(poly([pt(6.5, 8.7, 5.7), pt(17.5, 8.7, 5.7), pt(17.5, 14, 10), pt(6.5, 14, 10)],
               shade(BLUE, 1.35)))                           # blueprint sheet
    s.raw('<path d="M56 70 h10 M56 74 h14 M56 78 h7" stroke="%s" stroke-width="1.2"/>' % PAPER)
    s.box(19, 17, 0, 3, 3, 9, METAL)                         # lamp pole
    s.raw('<circle cx="60" cy="56" r="3" fill="%s"/>' % GLOW)
    return s


DECOR = {name[5:]: fn for name, fn in list(globals().items())
         if name.startswith("deco_")}


# --- workers ------------------------------------------------------------------
def worker(chest, item_svg):
    s = Sprite(view="0 0 32 40")
    s.raw('<ellipse cx="16" cy="37" rx="8" ry="2.5" fill="#000" fill-opacity="0.18"/>')
    s.raw('<rect x="11" y="28" width="4" height="8" rx="1.5" fill="%s"/>' % IRON)   # legs
    s.raw('<rect x="17" y="28" width="4" height="8" rx="1.5" fill="%s"/>' % IRON)
    s.raw('<rect x="8" y="14" width="16" height="16" rx="6" fill="%s"/>' % METAL)   # body
    s.raw('<rect x="11" y="19" width="10" height="7" rx="2" fill="%s"/>' % chest)   # chest plate
    s.raw('<rect x="9" y="4" width="14" height="11" rx="5" fill="%s"/>' % shade(METAL, 1.15))
    s.raw('<rect x="11.5" y="7.5" width="9" height="4.5" rx="2" fill="%s"/>' % INK)  # visor
    s.raw('<circle cx="14" cy="9.7" r="1.1" fill="%s"/>' % SCREEN)                  # eyes
    s.raw('<circle cx="18" cy="9.7" r="1.1" fill="%s"/>' % SCREEN)
    s.raw('<line x1="16" y1="4" x2="16" y2="1.5" stroke="%s" stroke-width="1.4"/>' % METAL)
    s.raw('<circle cx="16" cy="1.4" r="1.4" fill="%s"/>' % chest)                   # antenna
    if item_svg:
        s.raw(item_svg)
    return s


WORKERS = {
    "courier": worker(RED, '<rect x="20" y="20" width="9" height="8" rx="1" fill="%s"/>'
                           '<rect x="20" y="23" width="9" height="1.6" fill="%s"/>'
                           % (WOOD, WOOD_DARK)),
    "spark": worker(GLOW, '<path d="M25 18 l-3 5 h3 l-4 7 l7 -8 h-3 l3 -4 z" fill="%s"/>' % GLOW),
    "scroll": worker(PURPLE, '<rect x="21" y="19" width="8" height="10" rx="1" fill="%s"/>'
                             '<path d="M22.5 21.5 h5 M22.5 24 h5 M22.5 26.5 h3.5" '
                             'stroke="%s" stroke-width="1"/>' % (PAPER, INK)),
    "inspector": worker(BLUE, '<rect x="21" y="18" width="8" height="11" rx="1" fill="%s"/>'
                              '<path d="M22.5 21 h5 M22.5 24 h5 M22.5 27 h4" stroke="%s" '
                              'stroke-width="1.2"/><circle cx="23" cy="19.5" r="0.8" fill="%s"/>'
                              % (PAPER, GREEN, RED)),
}


# --- props --------------------------------------------------------------------
def prop_crate():
    s = Sprite(view="0 0 64 64")
    s.raw('<ellipse cx="32" cy="46" rx="18" ry="9" fill="#000" fill-opacity="0.12"/>')
    s.box(8, 8, 0, 8, 8, 7, WOOD)
    return s


def prop_plant():
    s = Sprite(view="0 0 64 64")
    s.raw('<ellipse cx="32" cy="46" rx="12" ry="6" fill="#000" fill-opacity="0.12"/>')
    s.box(10, 10, 0, 4, 4, 4, shade(RED, 0.7))
    s.raw('<path d="M32 36 q-8 -8 -3 -16 q5 6 3 16 q8 -10 12 -6 q-6 8 -12 6" fill="%s"/>' % GREEN)
    return s


def prop_lamp():
    s = Sprite(view="0 0 64 64")
    s.raw('<ellipse cx="32" cy="46" rx="10" ry="5" fill="#000" fill-opacity="0.12"/>')
    s.box(11, 11, 0, 2, 2, 14, IRON)
    s.raw('<circle cx="32" cy="14" r="5" fill="%s"/>' % GLOW)
    return s


PROPS = {"crate": prop_crate, "plant": prop_plant, "lamp": prop_lamp}


if __name__ == "__main__":
    for name, fn in sorted(DECOR.items()):
        fn(Sprite().shadow()).write(OUT / "decor" / f"{name}.svg")
    for name, sprite in sorted(WORKERS.items()):
        sprite.write(OUT / "workers" / f"{name}.svg")
    for name, fn in sorted(PROPS.items()):
        fn().write(OUT / "props" / f"{name}.svg")
    n = len(DECOR) + len(WORKERS) + len(PROPS)
    print(f"generated {n} sprites into {OUT}")
