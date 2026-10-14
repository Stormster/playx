"""Read and police in-game names (trainer, rival, Pokemon nicknames) in FireRed.

The word list lives in <data>/blocklist.txt (one entry per line, # for comments).
It ships empty: the filter does nothing until the run owner adds entries.
"""
import os
import re

# Gen 3 character encoding (subset that names can use)
DECODE = {0x00: " ", 0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-", 0xB0: "...", 0xB4: "'", 0xB8: ",",
          0xBA: "/"}
DECODE.update({0xA1 + i: str(i) for i in range(10)})
DECODE.update({0xBB + i: chr(ord("A") + i) for i in range(26)})
DECODE.update({0xD5 + i: chr(ord("a") + i) for i in range(26)})
ENCODE = {v: k for k, v in DECODE.items() if len(v) == 1}

LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g",
                      "!": "i", "$": "s", "@": "a"})


def decode(raw):
    out = []
    for b in raw:
        if b == 0xFF:
            break
        out.append(DECODE.get(b, "?"))
    return "".join(out)


def encode(text, size):
    data = bytes(ENCODE.get(c, 0x00) for c in text[:size])
    return (data + b"\xff" * size)[:size]


def normalize(text):
    t = text.lower().translate(LEET)
    return re.sub(r"[^a-z]", "", t)


class NameFilter:
    PLAYER_NAME = 8       # SaveBlock2 + 0x00
    NICK_LEN = 10         # BoxPokemon + 0x08
    DEFAULT_PLAYER = "RED"
    DEFAULT_NICK = "REDACTED"

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "blocklist.txt")
        if not os.path.exists(self.path):
            with open(self.path, "w") as f:
                f.write("# One word per line. Matching ignores case, spaces and 0/1/3/4/5/7 swaps.\n")
        self._mtime = None
        self.words = []
        self.hits = 0

    def reload(self):
        m = os.path.getmtime(self.path)
        if m != self._mtime:
            self._mtime = m
            with open(self.path, encoding="utf-8") as f:
                self.words = [normalize(w) for w in f.read().splitlines()
                              if w.strip() and not w.lstrip().startswith("#")]
            self.words = [w for w in self.words if w]

    def bad(self, text):
        n = normalize(text)
        return any(w in n for w in self.words)

    def check(self, emu, party_addr, party_count, storage_ptr, sb2_ptr, rival_addr=None):
        """Scan names; overwrite offenders in RAM. Returns list of replaced names."""
        self.reload()
        if not self.words:
            return []
        fixed = []
        if sb2_ptr:
            raw = emu.read(sb2_ptr, self.PLAYER_NAME)
            if self.bad(decode(raw)):
                emu.write(sb2_ptr, encode(self.DEFAULT_PLAYER, self.PLAYER_NAME - 1) + b"\xff")
                fixed.append("trainer")
        if rival_addr:
            raw = emu.read(rival_addr, self.PLAYER_NAME)
            if self.bad(decode(raw)):
                emu.write(rival_addr, encode("GREEN", self.PLAYER_NAME - 1) + b"\xff")
                fixed.append("rival")
        slots = [party_addr + i * 100 for i in range(min(party_count, 6))]
        if storage_ptr:
            slots += [storage_ptr + 4 + i * 80 for i in range(14 * 30)]
        for base in slots:
            raw = emu.read(base + 8, self.NICK_LEN)
            if raw[0] in (0x00, 0xFF) and raw[1] in (0x00, 0xFF):
                continue
            if self.bad(decode(raw)):
                emu.write(base + 8, encode(self.DEFAULT_NICK, self.NICK_LEN))
                fixed.append("nickname")
        self.hits += len(fixed)
        return fixed
