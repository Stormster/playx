"""Minimal libretro frontend for the mGBA core (headless, single instance)."""
import ctypes as C
import os

import numpy as np

# libretro constants
ENV_GET_CAN_DUPE = 3
ENV_GET_SYSTEM_DIRECTORY = 9
ENV_SET_PIXEL_FORMAT = 10
ENV_GET_VARIABLE = 15
ENV_GET_VARIABLE_UPDATE = 17
ENV_GET_LOG_INTERFACE = 27
ENV_GET_SAVE_DIRECTORY = 31
ENV_EXPERIMENTAL = 0x10000
DEVICE_JOYPAD = 1
MEMORY_SAVE_RAM = 0

# joypad ids
BUTTONS = {
    "b": 0, "select": 2, "start": 3, "up": 4, "down": 5,
    "left": 6, "right": 7, "a": 8, "l": 10, "r": 11,
}

WIDTH, HEIGHT = 240, 160


class MemDesc(C.Structure):
    _fields_ = [("flags", C.c_uint64), ("ptr", C.c_void_p), ("offset", C.c_size_t), ("start", C.c_size_t),
                ("select", C.c_size_t), ("disconnect", C.c_size_t), ("len", C.c_size_t), ("addrspace", C.c_char_p)]


class MemMap(C.Structure):
    _fields_ = [("descriptors", C.POINTER(MemDesc)), ("num", C.c_uint)]


ENV_SET_MEMORY_MAPS = 36


class GameInfo(C.Structure):
    _fields_ = [("path", C.c_char_p), ("data", C.c_void_p), ("size", C.c_size_t), ("meta", C.c_char_p)]


class GameGeometry(C.Structure):
    _fields_ = [("base_width", C.c_uint), ("base_height", C.c_uint), ("max_width", C.c_uint),
                ("max_height", C.c_uint), ("aspect_ratio", C.c_float)]


class SystemTiming(C.Structure):
    _fields_ = [("fps", C.c_double), ("sample_rate", C.c_double)]


class AVInfo(C.Structure):
    _fields_ = [("geometry", GameGeometry), ("timing", SystemTiming)]


ENV_CB = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO_CB = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
AUDIO_BATCH_CB = C.CFUNCTYPE(C.c_size_t, C.POINTER(C.c_int16), C.c_size_t)
POLL_CB = C.CFUNCTYPE(None)
STATE_CB = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)


class Emulator:
    """Wraps one mGBA core. Not thread safe: call everything from one thread."""

    def __init__(self, core_path, rom_path, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self._dir_bytes = C.create_string_buffer(self.data_dir.encode())
        self.lib = C.CDLL(core_path)
        self.pressed = set()          # names of buttons currently held
        self.frame = np.zeros((HEIGHT, WIDTH), dtype=np.uint16)   # RGB565
        self.audio = []               # list of int16 stereo arrays since last drain
        self.regions = []             # (start, length, pointer) for IWRAM/EWRAM etc.
        self._cbs = [
            ENV_CB(self._env), VIDEO_CB(self._video), AUDIO_CB(self._audio1),
            AUDIO_BATCH_CB(self._audio_batch), POLL_CB(lambda: None), STATE_CB(self._state),
        ]
        L = self.lib
        L.retro_set_environment(self._cbs[0])
        L.retro_set_video_refresh(self._cbs[1])
        L.retro_set_audio_sample(self._cbs[2])
        L.retro_set_audio_sample_batch(self._cbs[3])
        L.retro_set_input_poll(self._cbs[4])
        L.retro_set_input_state(self._cbs[5])
        L.retro_init()

        with open(rom_path, "rb") as f:
            self._rom = f.read()
        self._rom_buf = C.create_string_buffer(self._rom, len(self._rom))
        info = GameInfo(rom_path.encode(), C.cast(self._rom_buf, C.c_void_p), len(self._rom), None)
        L.retro_load_game.restype = C.c_bool
        if not L.retro_load_game(C.byref(info)):
            raise RuntimeError("core refused the ROM")
        av = AVInfo()
        L.retro_get_system_av_info(C.byref(av))
        self.fps = av.timing.fps
        self.sample_rate = int(av.timing.sample_rate)

        L.retro_serialize_size.restype = C.c_size_t
        L.retro_serialize.restype = C.c_bool
        L.retro_unserialize.restype = C.c_bool
        L.retro_get_memory_data.restype = C.c_void_p
        L.retro_get_memory_size.restype = C.c_size_t

    # ---- callbacks -------------------------------------------------------
    def _env(self, cmd, data):
        cmd &= ~ENV_EXPERIMENTAL
        if cmd == ENV_SET_PIXEL_FORMAT:
            return C.cast(data, C.POINTER(C.c_int))[0] == 2   # RGB565 only
        if cmd == ENV_GET_CAN_DUPE:
            C.cast(data, C.POINTER(C.c_bool))[0] = True
            return True
        if cmd in (ENV_GET_SYSTEM_DIRECTORY, ENV_GET_SAVE_DIRECTORY):
            C.cast(data, C.POINTER(C.c_char_p))[0] = C.cast(self._dir_bytes, C.c_char_p).value
            return True
        if cmd == ENV_SET_MEMORY_MAPS:
            m = C.cast(data, C.POINTER(MemMap))[0]
            for i in range(m.num):
                d = m.descriptors[i]
                if d.ptr and d.len:
                    self.regions.append((d.start, d.len, d.ptr + d.offset))
            return True
        if cmd == ENV_GET_VARIABLE_UPDATE:
            C.cast(data, C.POINTER(C.c_bool))[0] = False
            return True
        return False

    def _video(self, data, width, height, pitch):
        if not data:
            return   # duped frame, keep the last one
        buf = (C.c_uint8 * (pitch * height)).from_address(data)
        rows = np.frombuffer(buf, dtype=np.uint16).reshape(height, pitch // 2)
        self.frame = rows[:, :width].copy()

    def _audio1(self, left, right):
        self.audio.append(np.array([left, right], dtype=np.int16))

    def _audio_batch(self, data, frames):
        arr = np.ctypeslib.as_array(data, shape=(frames * 2,)).copy()
        self.audio.append(arr)
        return frames

    def _state(self, port, device, index, id_):
        if port != 0 or device != DEVICE_JOYPAD:
            return 0
        for name in self.pressed:
            if BUTTONS[name] == id_:
                return 1
        return 0

    # ---- API ---------------------------------------------------------------
    def run_frame(self):
        self.lib.retro_run()

    def drain_audio(self):
        """Stereo int16 samples produced since the last call, interleaved L,R."""
        if not self.audio:
            return np.zeros(0, dtype=np.int16)
        out = np.concatenate(self.audio)
        self.audio = []
        return out

    def frame_rgb(self):
        """Current frame as an HxWx3 uint8 array."""
        f = self.frame.astype(np.uint32)
        r = ((f >> 11) & 0x1F) * 255 // 31
        g = ((f >> 5) & 0x3F) * 255 // 63
        b = (f & 0x1F) * 255 // 31
        return np.stack([r, g, b], axis=-1).astype(np.uint8)

    def save_state(self):
        size = self.lib.retro_serialize_size()
        buf = C.create_string_buffer(size)
        if not self.lib.retro_serialize(buf, size):
            raise RuntimeError("serialize failed")
        return buf.raw

    def load_state(self, blob):
        buf = C.create_string_buffer(blob, len(blob))
        if not self.lib.retro_unserialize(buf, len(blob)):
            raise RuntimeError("unserialize failed")

    def read(self, addr, size):
        """Read emulated memory (IWRAM 0x03..., EWRAM 0x02...). Returns bytes."""
        for start, length, ptr in self.regions:
            if start <= addr and addr + size <= start + length:
                return C.string_at(ptr + addr - start, size)
        raise ValueError(f"unmapped address {addr:#x}")

    def write(self, addr, data):
        """Write emulated RAM (used by the name filter)."""
        for start, length, ptr in self.regions:
            if start <= addr and addr + len(data) <= start + length:
                C.memmove(ptr + addr - start, data, len(data))
                return
        raise ValueError(f"unmapped address {addr:#x}")

    def read_u32(self, addr):
        return int.from_bytes(self.read(addr, 4), "little")

    def save_ram(self):
        ptr = self.lib.retro_get_memory_data(MEMORY_SAVE_RAM)
        size = self.lib.retro_get_memory_size(MEMORY_SAVE_RAM)
        if not ptr or not size:
            return b""
        return C.string_at(ptr, size)

    def load_save_ram(self, blob):
        ptr = self.lib.retro_get_memory_data(MEMORY_SAVE_RAM)
        size = self.lib.retro_get_memory_size(MEMORY_SAVE_RAM)
        if ptr and size and blob:
            C.memmove(ptr, blob, min(size, len(blob)))
