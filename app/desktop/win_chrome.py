"""Native Windows window behaviour for InkDoc's custom title bar.

Windows only offers the Snap Layouts flyout to a window that looks like a normal
window *and* that names a maximise button through WM_NCHITTEST. pywebview's
frameless window failed both tests: measured on a live window its style was
0x16070000, missing WS_CAPTION and WS_SYSMENU that a stock window (Notepad,
0x14CF0000) carries, and with no window procedure of its own it could never name
a maximise button.

Adding those styles alone is not enough, and doing only that is actively worse:
DWM then draws the native caption buttons into whatever non-client strip is left
at the top, which shows up as slivers of a phantom min/max/close above the real
title bar -- hovering *those* is what produced Snap Layouts, from an invisible
control. So the top non-client strip is removed entirely (WM_NCCALCSIZE reclaims
the caption *and* the top frame), leaving DWM nowhere to draw them.

Hiding them is still not the same as giving them up. DWM went on reporting
CAPTION_BUTTON_BOUNDS of 146x30 for this window, and the flyout kept anchoring
to that invisible rectangle rather than to the real button. See _wanted_style:
clearing WS_SYSMENU is what makes DWM report zero-width bounds and hand the
decision back to our WM_NCHITTEST.

That still leaves the real obstacle. pywebview hosts WebView2 as a child HWND
covering the client area, and the window under the cursor is what Windows
hit-tests. Measured: WindowFromPoint over the maximise button returns
Chrome_RenderWidgetHostHWND, owned by msedgewebview2.exe -- a different process,
so it can neither be subclassed nor made hit-transparent. Our window was simply
never asked. The fix is to cut that button's rectangle out of the WebView2
control's window *region*, so the pixel genuinely belongs to us again.

The page cannot paint into a hole it no longer owns, so this module draws that
one button itself, from colours the page reports, which is why the title bar
still looks identical in both themes.

Hit testing asks the original window procedure first and only ever rewrites an
HTCLIENT answer, so every resize code Windows computed is passed through
untouched; the top edge, whose non-client strip we removed, is synthesised back.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
from collections.abc import Callable

_IS_WINDOWS = sys.platform == "win32"

# None off Windows: this module stays importable there, it just does nothing.
user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WINDOWS else None
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if _IS_WINDOWS else None

WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_PAINT = 0x000F
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCMOUSELEAVE = 0x02A2

HTCLIENT = 1
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14

GWLP_WNDPROC = -4
GWL_STYLE = -16

WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MAXIMIZEBOX = 0x00010000
WS_MAXIMIZE = 0x01000000

SM_CYCAPTION = 4
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

SW_MAXIMIZE = 3
SW_RESTORE = 9

TME_LEAVE = 0x00000002
TME_NONCLIENT = 0x00000010

RGN_DIFF = 4
PS_SOLID = 0
NULL_BRUSH = 5
# Comfortably larger than any window; a region may exceed the window it clips.
_HUGE = 1 << 15

_SWP_FRAMECHANGED = 0x0002 | 0x0001 | 0x0004 | 0x0020  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED

# Dark-theme fallbacks, used only until the page reports its own palette.
_DEFAULT_COLORS = {
    "bg": 0x1B1E18,
    "bg_hover": 0x0F110C,
    "fg": 0x8E968A,
    "fg_hover": 0xE8EDE6,
}


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", _RECT * 3), ("lppos", ctypes.c_void_p)]


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("hwndTrack", wt.HWND),
        ("dwHoverTime", wt.DWORD),
    ]


if _IS_WINDOWS:
    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    user32.CallWindowProcW.restype = ctypes.c_longlong
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
    ]
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetDC.restype = wt.HDC
    user32.GetDC.argtypes = [wt.HWND]
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    # GDI returns handles, which are pointer-sized. Without these the default
    # 32-bit restype silently truncates them on 64-bit Windows and every draw
    # call fails against a bogus handle.
    gdi32.CreateSolidBrush.restype = ctypes.c_void_p
    gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
    gdi32.CreatePen.restype = ctypes.c_void_p
    gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [wt.HDC, ctypes.c_void_p]
    gdi32.GetStockObject.restype = ctypes.c_void_p
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.Rectangle.argtypes = [
        wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    gdi32.RoundRect.argtypes = [
        wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int,
    ]
    gdi32.CreateRectRgn.restype = ctypes.c_void_p
    gdi32.CreateRectRgn.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    gdi32.CombineRgn.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
    ]
    user32.SetWindowRgn.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
    user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(_RECT), ctypes.c_void_p]
else:  # pragma: no cover
    _WNDPROC = None


def is_maximized(hwnd: int) -> bool:
    """Ask Windows, rather than trusting a cached flag that the shell can
    invalidate behind our back (Snap Layouts, Win+Up, double-click, aero snap).
    """
    if not _IS_WINDOWS or not hwnd:
        return False
    try:
        return bool(user32.GetWindowLongPtrW(wt.HWND(hwnd), GWL_STYLE) & WS_MAXIMIZE)
    except Exception:
        return False


def _frame_thickness() -> int:
    return user32.GetSystemMetrics(SM_CYSIZEFRAME) + user32.GetSystemMetrics(SM_CXPADDEDBORDER)


def _wanted_style(hwnd: int) -> int:
    """The style the shell needs to see, measured against windows that work.

    WS_SYSMENU is deliberately cleared. It is what makes DWM keep caption
    buttons of its own: with it set, DwmGetWindowAttribute reports
    CAPTION_BUTTON_BOUNDS of 146x30 for this window and the Snap Layouts flyout
    attaches to those invisible buttons instead of ours. VS Code, whose custom
    title bar does get the flyout, reports 0-width bounds and carries no
    WS_SYSMENU. Clearing it drops DWM's bounds to zero here too, at which point
    the shell uses the HTMAXBUTTON this module reports from WM_NCHITTEST.
    """
    style = user32.GetWindowLongPtrW(wt.HWND(hwnd), GWL_STYLE)
    return (style | WS_CAPTION | WS_MAXIMIZEBOX) & ~WS_SYSMENU


class WindowChrome:
    """Owns the window-procedure subclass for one top-level window."""

    def __init__(self) -> None:
        self._hwnd: int = 0
        self._old_proc: int = 0
        # Must outlive the subclass: Windows calls into this pointer, so letting
        # Python collect it would crash the process from inside the message loop.
        self._proc_ref = None
        self._button_rect: tuple[int, int, int, int] | None = None
        self._hole_rect: tuple[int, int, int, int] | None = None
        self._colors = dict(_DEFAULT_COLORS)
        self._lock = threading.Lock()
        self._hovering = False
        self._tracking = False
        self._on_toggle_maximize: Callable[[], None] | None = None
        self._on_state: Callable[[bool], None] | None = None

    # ─── Public API ──────────────────────────────────────────────────────────

    def install(
        self,
        hwnd: int,
        on_toggle_maximize: Callable[[], None],
        on_state: Callable[[bool], None],
    ) -> bool:
        """Subclass the window and give it the styles the shell looks for."""
        if not _IS_WINDOWS or not hwnd:
            return False
        self._hwnd = hwnd
        self._on_toggle_maximize = on_toggle_maximize
        self._on_state = on_state
        try:
            user32.SetWindowLongPtrW(wt.HWND(hwnd), GWL_STYLE, _wanted_style(hwnd))

            self._proc_ref = _WNDPROC(self._wnd_proc)
            old = user32.SetWindowLongPtrW(
                wt.HWND(hwnd),
                GWLP_WNDPROC,
                ctypes.cast(self._proc_ref, ctypes.c_void_p).value,
            )
            if not old:
                self._proc_ref = None
                return False
            self._old_proc = old
            user32.SetWindowPos(wt.HWND(hwnd), 0, 0, 0, 0, 0, _SWP_FRAMECHANGED)
            return True
        except Exception as exc:
            print(f"[Warn] Native window chrome unavailable: {exc}", file=sys.stderr)
            self._proc_ref = None
            return False

    def set_maximize_button_rect(self, left: int, top: int, right: int, bottom: int) -> None:
        """Record where the page draws its maximise button, in client pixels.

        The page is the only thing that knows this: it moves with layout, zoom
        and DPI, so it is reported from JavaScript rather than derived from the
        stylesheet here.
        """
        with self._lock:
            new = (int(left), int(top), int(right), int(bottom)) if (
                right > left and bottom > top
            ) else None
            changed = new != self._button_rect
            self._button_rect = new
        if changed:
            self._apply_hole()
            self._repaint_button()

    def set_titlebar_colors(self, bg: int, bg_hover: int, fg: int, fg_hover: int) -> None:
        """Take the title bar's palette from the page so the natively drawn
        button tracks the active theme instead of hardcoding one.
        """
        with self._lock:
            self._colors = {
                "bg": int(bg) & 0xFFFFFF,
                "bg_hover": int(bg_hover) & 0xFFFFFF,
                "fg": int(fg) & 0xFFFFFF,
                "fg_hover": int(fg_hover) & 0xFFFFFF,
            }
        self._repaint_button()

    # ─── The hole in the WebView2 control ────────────────────────────────────

    def _children_covering(self, screen: tuple[int, int, int, int]) -> list[int]:
        """The WebView2 host controls that belong to us *and* actually cover the
        button. The deeper Chromium windows belong to msedgewebview2.exe and
        cannot be touched, but clipping their host is enough: a child is clipped
        to its parent's region.

        The covering test is what keeps this honest. Clipping every in-process
        child was harmless while WebView2 kept two of them, but enabling
        non-client region support adds more (measured: seven Chrome_WidgetWin_0
        on one window), and handing a popup or tooltip a region cut for the
        title bar would clip that popup for real. Only a window covering the
        button can be hiding it, so only those are cut.
        """
        import os

        me = os.getpid()
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(child, _):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
            if pid.value == me:
                r = _RECT()
                if user32.GetWindowRect(wt.HWND(child), ctypes.byref(r)) and (
                    r.left <= screen[0] and r.top <= screen[1]
                    and r.right >= screen[2] and r.bottom >= screen[3]
                ):
                    found.append(child)
            return True

        user32.EnumChildWindows(wt.HWND(self._hwnd), cb, 0)
        return found

    def _apply_hole(self) -> None:
        """Cut the maximise button out of the WebView2 control so that pixel
        belongs to this window again and Windows will hit-test it against us.
        """
        with self._lock:
            rect = self._button_rect
        if not rect:
            return
        try:
            origin = wt.POINT(0, 0)
            user32.ClientToScreen(wt.HWND(self._hwnd), ctypes.byref(origin))
            screen = (origin.x + rect[0], origin.y + rect[1],
                      origin.x + rect[2], origin.y + rect[3])
            for child in self._children_covering(screen):
                # Deliberately not sized from the child's own rect. This runs
                # from WM_SIZE, where the child has not been re-laid-out yet, so
                # that rect is stale -- and a region smaller than the child clips
                # the WebView2 away for real. An oversized region is free: it is
                # intersected with the window anyway.
                full = gdi32.CreateRectRgn(0, 0, _HUGE, _HUGE)
                wr = _RECT()
                user32.GetWindowRect(wt.HWND(child), ctypes.byref(wr))
                hole = gdi32.CreateRectRgn(
                    screen[0] - wr.left, screen[1] - wr.top,
                    screen[2] - wr.left, screen[3] - wr.top,
                )
                gdi32.CombineRgn(full, full, hole, RGN_DIFF)
                gdi32.DeleteObject(hole)
                # The window owns the region handle once this succeeds.
                if not user32.SetWindowRgn(wt.HWND(child), full, True):
                    gdi32.DeleteObject(full)
            self._hole_rect = rect
        except Exception:
            pass

    # ─── Painting the one button the page can no longer draw ─────────────────

    def _repaint_button(self) -> None:
        with self._lock:
            rect = self._button_rect
        if not rect or not self._hwnd:
            return
        try:
            r = _RECT(rect[0], rect[1], rect[2], rect[3])
            user32.InvalidateRect(wt.HWND(self._hwnd), ctypes.byref(r), True)
        except Exception:
            pass

    def _paint_button(self) -> None:
        """Draw the maximise button: background, then the square glyph, matching
        what the stylesheet would have drawn.
        """
        with self._lock:
            rect = self._button_rect
            colors = dict(self._colors)
            hovering = self._hovering
        if not rect:
            return
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return

        hdc = user32.GetDC(wt.HWND(self._hwnd))
        if not hdc:
            return
        try:
            fg = colors["fg_hover"] if hovering else colors["fg"]
            area = _RECT(left, top, right, bottom)
            base = gdi32.CreateSolidBrush(_to_colorref(colors["bg"]))
            user32.FillRect(hdc, ctypes.byref(area), base)
            gdi32.DeleteObject(base)
            if hovering:
                # The stylesheet rounds this button by 5 CSS px; match it so the
                # highlight is the same shape as the page-drawn buttons beside it.
                radius = max(2, round(width * 5 / 28)) * 2
                hover_brush = gdi32.CreateSolidBrush(_to_colorref(colors["bg_hover"]))
                old = gdi32.SelectObject(hdc, hover_brush)
                pen = gdi32.CreatePen(PS_SOLID, 1, _to_colorref(colors["bg_hover"]))
                old_pen = gdi32.SelectObject(hdc, pen)
                gdi32.RoundRect(hdc, left, top, right, bottom, radius, radius)
                gdi32.SelectObject(hdc, old_pen)
                gdi32.SelectObject(hdc, old)
                gdi32.DeleteObject(pen)
                gdi32.DeleteObject(hover_brush)

            # The glyph is a 9x9 CSS-pixel square outline; scale it from the
            # button's measured size so it stays right at any DPI.
            side = max(6, round(width * 9 / 28))
            thickness = max(1, round(width * 1.3 / 28))
            gx = left + (width - side) // 2
            gy = top + (height - side) // 2
            pen = gdi32.CreatePen(PS_SOLID, thickness, _to_colorref(fg))
            old_pen = gdi32.SelectObject(hdc, pen)
            old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
            gdi32.Rectangle(hdc, gx, gy, gx + side, gy + side)
            gdi32.SelectObject(hdc, old_pen)
            gdi32.SelectObject(hdc, old_brush)
            gdi32.DeleteObject(pen)
        except Exception:
            pass
        finally:
            user32.ReleaseDC(wt.HWND(self._hwnd), hdc)

    # ─── Internals ───────────────────────────────────────────────────────────

    def _point_in_button(self, screen_x: int, screen_y: int) -> bool:
        with self._lock:
            rect = self._button_rect
        if not rect:
            return False
        pt = wt.POINT(screen_x, screen_y)
        if not user32.ScreenToClient(wt.HWND(self._hwnd), ctypes.byref(pt)):
            return False
        left, top, right, bottom = rect
        return left <= pt.x < right and top <= pt.y < bottom

    def _track_nc_leave(self) -> None:
        if self._tracking:
            return
        tme = _TRACKMOUSEEVENT()
        tme.cbSize = ctypes.sizeof(_TRACKMOUSEEVENT)
        tme.dwFlags = TME_LEAVE | TME_NONCLIENT
        tme.hwndTrack = wt.HWND(self._hwnd)
        tme.dwHoverTime = 0
        if user32.TrackMouseEvent(ctypes.byref(tme)):
            self._tracking = True

    def _set_hover(self, hovering: bool) -> None:
        if hovering == self._hovering:
            return
        self._hovering = hovering
        self._paint_button()

    def _call_old(self, hwnd, msg, wparam, lparam):
        return user32.CallWindowProcW(
            ctypes.c_void_p(self._old_proc), hwnd, msg, wparam, lparam
        )

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_NCCALCSIZE and wparam:
                result = self._call_old(hwnd, msg, wparam, lparam)
                params = ctypes.cast(lparam, ctypes.POINTER(_NCCALCSIZE_PARAMS)).contents
                # Reclaim the caption, and the top frame too unless maximised --
                # while maximised that frame hangs off-screen and is exactly what
                # keeps the client area aligned to the work area.
                reclaim = user32.GetSystemMetrics(SM_CYCAPTION)
                if not is_maximized(self._hwnd):
                    reclaim += _frame_thickness()
                params.rgrc[0].top -= reclaim
                return result

            if msg == WM_NCHITTEST:
                code = self._call_old(hwnd, msg, wparam, lparam)
                if code != HTCLIENT:
                    return code
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                # The top resize strip is client area now, so put it back.
                if not is_maximized(self._hwnd):
                    r = _RECT()
                    user32.GetWindowRect(wt.HWND(self._hwnd), ctypes.byref(r))
                    grip = _frame_thickness()
                    if y < r.top + grip:
                        if x < r.left + grip * 2:
                            return HTTOPLEFT
                        if x >= r.right - grip * 2:
                            return HTTOPRIGHT
                        return HTTOP
                if self._point_in_button(x, y):
                    return HTMAXBUTTON
                return code

            if msg == WM_PAINT:
                result = self._call_old(hwnd, msg, wparam, lparam)
                self._paint_button()
                return result

            if msg == WM_NCMOUSEMOVE and wparam == HTMAXBUTTON:
                self._set_hover(True)
                self._track_nc_leave()
                return 0

            if msg == WM_NCMOUSELEAVE:
                self._tracking = False
                self._set_hover(False)
                return self._call_old(hwnd, msg, wparam, lparam)

            if msg == WM_NCLBUTTONDOWN and wparam == HTMAXBUTTON:
                # Swallow it: letting DefWindowProc see this starts the system's
                # own caption-button loop, which would draw native button art.
                return 0

            if msg == WM_NCLBUTTONUP and wparam == HTMAXBUTTON:
                if self._on_toggle_maximize:
                    try:
                        self._on_toggle_maximize()
                    except Exception:
                        pass
                return 0

            if msg == WM_SIZE:
                result = self._call_old(hwnd, msg, wparam, lparam)
                # WinForms recomputes its own styles on state changes; if it puts
                # WS_SYSMENU back, DWM reclaims the caption buttons and the
                # flyout silently moves off our button again.
                wanted = _wanted_style(self._hwnd)
                if wanted != user32.GetWindowLongPtrW(wt.HWND(self._hwnd), GWL_STYLE):
                    user32.SetWindowLongPtrW(wt.HWND(self._hwnd), GWL_STYLE, wanted)
                # The child was resized with us, so its region hole must be recut.
                self._apply_hole()
                self._repaint_button()
                # The shell can maximise us without going through the page, so
                # tell the page what actually happened.
                if self._on_state:
                    try:
                        self._on_state(is_maximized(self._hwnd))
                    except Exception:
                        pass
                return result

            if msg == WM_DESTROY:
                self._detach(hwnd)
                return self._call_old(hwnd, msg, wparam, lparam)

            return self._call_old(hwnd, msg, wparam, lparam)
        except Exception:
            # Never let a Python error escape into the window procedure.
            try:
                return self._call_old(hwnd, msg, wparam, lparam)
            except Exception:
                return 0

    def _detach(self, hwnd) -> None:
        if self._old_proc:
            try:
                user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._old_proc)
            except Exception:
                pass
            self._old_proc = 0
        self._proc_ref = None


def _to_colorref(rgb: int) -> int:
    """Win32 COLORREF is 0x00BBGGRR, the reverse of an HTML colour."""
    return ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | ((rgb >> 16) & 0xFF)
