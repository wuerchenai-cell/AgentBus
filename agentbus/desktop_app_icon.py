"""Read the real Windows Shell icon for one explicitly selected app path.

This module never searches for installed products. The caller must provide an
absolute ``.exe`` path selected by the user. Shortcuts are intentionally not
accepted because Windows Shell can resolve their target outside the selected
file's checked path. Windows Shell supplies the icon; failures produce one
neutral, non-branded placeholder.
"""
from __future__ import annotations

import base64
import ctypes
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'viewBox="0 0 64 64"><rect x="9" y="11" width="46" height="42" '
    'rx="9" fill="#eef1f5" stroke="#8993a2" stroke-width="3"/>'
    '<path d="M10 22h44" stroke="#8993a2" stroke-width="3"/>'
    '<circle cx="17" cy="17" r="1.4" fill="#667085"/>'
    '<circle cx="22" cy="17" r="1.4" fill="#667085"/></svg>'
)
NEUTRAL_PLACEHOLDER_DATA_URI = (
    "data:image/svg+xml;base64," +
    base64.b64encode(_PLACEHOLDER_SVG.encode("utf-8")).decode("ascii")
)


@dataclass(frozen=True)
class DesktopIconResult:
    """Icon returned to a UI caller without exposing a local path."""

    status: str  # "ok" or "placeholder"
    data_uri: str
    reason: str = ""


def _is_windows() -> bool:
    return os.name == "nt"


def get_desktop_app_icon(path: str | os.PathLike[str]) -> DesktopIconResult:
    """Return the Shell icon for a user-selected absolute EXE.

    The target is only queried for its Shell icon; it is never launched. The
    result contains a PNG data URI on success, or a neutral SVG placeholder.
    """
    if not _is_windows():
        return _placeholder("not-windows")

    try:
        selected = Path(path)
    except (TypeError, ValueError, OSError):
        return _placeholder("invalid-path")
    if not selected.is_absolute():
        return _placeholder("path-must-be-absolute")
    if selected.suffix.casefold() != ".exe":
        return _placeholder("unsupported-file-type")
    try:
        if not selected.is_file():
            return _placeholder("file-not-found")
    except OSError:
        return _placeholder("file-not-found")

    try:
        png = _extract_shell_icon_png(str(selected))
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            return _placeholder("icon-unavailable")
    except Exception:
        # Shell and GDI failures are an ordinary unsupported-host condition.
        return _placeholder("icon-unavailable")
    return DesktopIconResult(
        status="ok",
        data_uri="data:image/png;base64," + base64.b64encode(png).decode("ascii"),
    )


def _placeholder(reason: str) -> DesktopIconResult:
    return DesktopIconResult("placeholder", NEUTRAL_PLACEHOLDER_DATA_URI, reason)


def _extract_shell_icon_png(path: str) -> bytes:
    """Use SHGetFileInfoW + the icon bitmaps; available only on Windows."""
    if not _is_windows():
        raise OSError("Windows Shell is unavailable")

    from ctypes import wintypes

    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [
            ("hIcon", wintypes.HANDLE),
            ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", wintypes.WCHAR * 260),
            ("szTypeName", wintypes.WCHAR * 80),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HANDLE),
            ("hbmColor", wintypes.HANDLE),
        ]

    class BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", ctypes.c_long),
            ("bmWidth", ctypes.c_long),
            ("bmHeight", ctypes.c_long),
            ("bmWidthBytes", ctypes.c_long),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [("rgbBlue", wintypes.BYTE), ("rgbGreen", wintypes.BYTE),
                    ("rgbRed", wintypes.BYTE), ("rgbReserved", wintypes.BYTE)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 2)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.POINTER(SHFILEINFOW), wintypes.UINT,
                                       wintypes.UINT]
    shell32.SHGetFileInfoW.restype = ctypes.c_size_t
    user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HANDLE]
    user32.DestroyIcon.restype = wintypes.BOOL
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int

    info = SHFILEINFOW()
    handle = shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info),
                                    0x00000100 | 0x000000000)  # SHGFI_ICON | large icon
    if not handle or not info.hIcon:
        raise OSError("SHGetFileInfoW did not return an icon")

    icon_info = ICONINFO()
    color_bitmap = mask_bitmap = None
    dc = None
    try:
        if not user32.GetIconInfo(info.hIcon, ctypes.byref(icon_info)):
            raise OSError("GetIconInfo failed")
        color_bitmap, mask_bitmap = icon_info.hbmColor, icon_info.hbmMask
        bitmap_handle = color_bitmap or mask_bitmap
        bitmap = BITMAP()
        if not bitmap_handle or not gdi32.GetObjectW(bitmap_handle, ctypes.sizeof(bitmap),
                                                      ctypes.byref(bitmap)):
            raise OSError("GetObjectW failed")
        width = int(bitmap.bmWidth)
        height = int(bitmap.bmHeight)
        if not color_bitmap:
            height //= 2  # monochrome icon masks store AND and XOR planes
        if width <= 0 or height <= 0 or width > 512 or height > 512:
            raise OSError("Shell icon dimensions are invalid")
        dc = gdi32.CreateCompatibleDC(None)
        if not dc:
            raise OSError("CreateCompatibleDC failed")

        color = _read_bitmap(gdi32, dc, color_bitmap, width, height,
                             BITMAPINFO, BITMAPINFOHEADER, RGBQUAD, 32)
        mask = _read_bitmap(gdi32, dc, mask_bitmap, width, height,
                            BITMAPINFO, BITMAPINFOHEADER, RGBQUAD, 1)
        rgba = bytearray(width * height * 4)
        alpha_present = bool(color and any(color[i + 3] for i in range(0, len(color), 4)))
        for y in range(height):
            for x in range(width):
                offset = (y * width + x) * 4
                if color:
                    blue, green, red, alpha = color[offset:offset + 4]
                    if not alpha_present:
                        alpha = 0 if _mask_bit(mask, width, height, x, y) else 255
                else:
                    # Monochrome cursors/icons are unusual here; display their
                    # opaque mask plane as a neutral silhouette if encountered.
                    blue = green = red = 120
                    alpha = 0 if _mask_bit(mask, width, height, x, y) else 255
                rgba[offset:offset + 4] = bytes((red, green, blue, alpha))
        return _encode_rgba_png(width, height, bytes(rgba))
    finally:
        if dc:
            gdi32.DeleteDC(dc)
        if color_bitmap:
            gdi32.DeleteObject(color_bitmap)
        if mask_bitmap:
            gdi32.DeleteObject(mask_bitmap)
        user32.DestroyIcon(info.hIcon)


def _read_bitmap(gdi32, dc, handle, width, height, bitmap_info_type,
                 header_type, rgbquad_type, bit_count: int) -> bytes:
    if not handle:
        return b""
    info = bitmap_info_type()
    header = info.bmiHeader
    header.biSize = ctypes.sizeof(header_type)
    header.biWidth = width
    header.biHeight = -height if bit_count == 32 else height
    header.biPlanes = 1
    header.biBitCount = bit_count
    header.biCompression = 0  # BI_RGB
    if bit_count == 1:
        info.bmiColors[0] = rgbquad_type(0, 0, 0, 0)
        info.bmiColors[1] = rgbquad_type(255, 255, 255, 0)
        stride = ((width + 31) // 32) * 4
    else:
        stride = width * 4
    buffer = (ctypes.c_ubyte * (stride * height))()
    lines = gdi32.GetDIBits(dc, handle, 0, height, buffer, ctypes.byref(info), 0)
    if lines != height:
        raise OSError("GetDIBits failed")
    raw = bytes(buffer)
    if bit_count == 1:
        raw = b"".join(raw[row * stride:(row + 1) * stride]
                       for row in reversed(range(height)))
    return raw


def _mask_bit(mask: bytes, width: int, height: int, x: int, y: int) -> int:
    if not mask:
        return 0
    stride = ((width + 31) // 32) * 4
    index = (y * stride) + (x // 8)
    return (mask[index] >> (7 - x % 8)) & 1


def _encode_rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        raise ValueError("RGBA dimensions do not match pixel data")

    def chunk(name: bytes, body: bytes) -> bytes:
        payload = name + body
        return struct.pack(">I", len(body)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    scanlines = b"".join(b"\0" + pixels[y * width * 4:(y + 1) * width * 4]
                         for y in range(height))
    header = struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(scanlines, 9)) + chunk(b"IEND", b""))

