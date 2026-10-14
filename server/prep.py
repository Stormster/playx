"""Drive the emulator directly (no crowd) for testing. Keeps state in ../data/prep/cur.st."""
import sys, os
from PIL import Image
from emu import Emulator
SHOTS = "../data/prep/shots"
os.makedirs(SHOTS, exist_ok=True)
e = Emulator("../core/mgba_libretro.so", "../roms/firered.gba", "../data/prep")
if os.path.exists("../data/prep/cur.st"):
    e.load_state(open("../data/prep/cur.st", "rb").read())
def run(n):
    for _ in range(n): e.run_frame()
def tap(b, hold=5, rel=20):
    e.pressed = {b}; run(hold); e.pressed = set(); run(rel)
shots = []
def shot(name):
    Image.fromarray(e.frame_rgb()).save(f"{SHOTS}/{name}.png"); shots.append(name)
for tok in sys.argv[1].split():
    if tok.startswith("w"): run(int(tok[1:]))
    elif tok.startswith("s:"): shot(tok[2:])
    elif "*" in tok:
        b, k = tok.split("*"); [tap(b) for _ in range(int(k))]
    else: tap(tok)
open("../data/prep/cur.st", "wb").write(e.save_state())
if shots:
    ims = [Image.open(f"{SHOTS}/{n}.png") for n in shots]
    g = Image.new("RGB", (240 * min(4, len(ims)), 160 * ((len(ims) + 3) // 4)))
    for i, im in enumerate(ims): g.paste(im, ((i % 4) * 240, (i // 4) * 160))
    g = g.resize((g.width * 2, g.height * 2), Image.NEAREST); g.save(f"{SHOTS}/grid.png")
