"""Win32 helpers that keep the Tk window surface clean during DWM transitions.

CustomTkinter canvases can present black intermediate frames while Windows
minimises/restores the window.  The guards below:

1. Strip ``WS_EX_LAYERED`` so the window remains a pure standard native window.
2. Point ``GCLP_HBRBACKGROUND`` at a solid brush matching the app surface, so
   any GDI background erase is clean and light, never black.
3. Set ``DWMWA_TRANSITIONS_FORCEDISABLED`` so DWM skips the zoom animation
   that stretches an uninitialised black DirectX backbuffer from the taskbar.
"""

import ctypes
from ctypes import wintypes

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_GCLP_HBRBACKGROUND = -10
_DWMWA_TRANSITIONS_FORCEDISABLED = 3
_SWP_FRAMECHANGED_FLAGS = 0x0027  # NOMOVE | NOSIZE | NOZORDER | FRAMECHANGED

_RDW_INVALIDATE = 0x0001
_RDW_ERASE = 0x0004
_RDW_ALLCHILDREN = 0x0080
_RDW_UPDATENOW = 0x0100


def create_solid_background_brush(hex_color: str):
    """Create a GDI brush for a ``#RRGGBB`` colour.

    The handle is owned by the caller and must be released with
    :func:`delete_background_brush` before process exit.
    """
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return ctypes.windll.gdi32.CreateSolidBrush(r | (g << 8) | (b << 16))


def delete_background_brush(brush) -> None:
    if brush:
        try:
            ctypes.windll.gdi32.DeleteObject(brush)
        except Exception:
            pass


def apply_native_surface_guards(child_hwnd: int, background_brush) -> bool:
    """Apply all surface guards to the Tk child window's native parent.

    Returns False when the Tk window has no native parent (for example before
    mapping), in which case the caller should retry after the window maps.
    """
    user32 = ctypes.windll.user32
    parent_hwnd = user32.GetParent(child_hwnd)
    if not parent_hwnd:
        return False

    exstyle = user32.GetWindowLongW(parent_hwnd, _GWL_EXSTYLE)
    if exstyle & _WS_EX_LAYERED:
        user32.SetWindowLongW(parent_hwnd, _GWL_EXSTYLE, exstyle & ~_WS_EX_LAYERED)
        user32.SetWindowPos(parent_hwnd, 0, 0, 0, 0, 0, _SWP_FRAMECHANGED_FLAGS)

    user32.SetClassLongPtrW(parent_hwnd, _GCLP_HBRBACKGROUND, background_brush)
    user32.SetClassLongPtrW(child_hwnd, _GCLP_HBRBACKGROUND, background_brush)

    val = wintypes.BOOL(True)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        parent_hwnd,
        _DWMWA_TRANSITIONS_FORCEDISABLED,
        ctypes.byref(val),
        ctypes.sizeof(val),
    )
    return True


def force_native_repaint(child_hwnd: int) -> None:
    """Paint the completed Tk surface before DWM presents a restored frame."""
    parent_hwnd = ctypes.windll.user32.GetParent(child_hwnd) or child_hwnd
    ctypes.windll.user32.RedrawWindow(
        parent_hwnd,
        None,
        None,
        _RDW_INVALIDATE | _RDW_ERASE | _RDW_ALLCHILDREN | _RDW_UPDATENOW,
    )
    ctypes.windll.dwmapi.DwmFlush()
