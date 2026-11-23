import os

import customtkinter as ctk

from core.config_manager import get_resource_path
from gui.native_window import OpaqueWindow, disable_native_transitions
from gui.theme import (
    COLOR_BG,
    COLOR_TEXT_HEADING,
    COLOR_TEXT_PRIMARY,
    FONT_FAMILY,
    SECTION_GAP,
    WINDOW_PADDING,
)
from gui.widgets import ActionButton


class DialogSurface(OpaqueWindow, ctk.CTkToplevel):
    """Lay out the whole dialog before its first native paint."""

    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self._parent = parent
        self.transient(parent)
        self.configure(fg_color=COLOR_BG)
        try:
            self.iconbitmap(get_resource_path("assets", "icon.ico"))
        except Exception:
            pass
        self.bind("<Map>", self._on_map, add="+")

    def _on_map(self, event):
        if event.widget is self and os.name == "nt":
            disable_native_transitions(self.winfo_id())

    def center_over_parent(self, width: int, height: int):
        height = min(height, int(self._reverse_window_scaling(self.winfo_screenheight() - 84)))
        physical_width = round(self._apply_window_scaling(width))
        physical_height = round(self._apply_window_scaling(height))
        x = self._parent.winfo_x() + (self._parent.winfo_width() - physical_width) // 2
        y = self._parent.winfo_y() - 24
        x = max(0, min(x, self.winfo_screenwidth() - physical_width))
        y = max(0, min(y, self.winfo_screenheight() - physical_height - 48))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def present(self):
        if self.state() != "normal":
            self.update_idletasks()
            if os.name == "nt":
                disable_native_transitions(self.winfo_id())
            self.deiconify()
        self.lift()
        self.focus_force()


def replace_content(previous, prepared):
    """Place a fully laid-out frame in the viewport in one Tk update."""
    prepared.update_idletasks()
    if previous is not None:
        previous.pack_forget()
    prepared.pack(fill="x")
    if previous is not None:
        previous.destroy()


class Prompt(DialogSurface):
    def __init__(self, parent, title: str, message: str, confirm: bool):
        super().__init__(parent)
        self.confirmed = False
        self.title(title)
        self.resizable(False, False)
        ctk.CTkLabel(
            self, text=title, anchor="w", text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
        ).pack(fill="x", padx=WINDOW_PADDING, pady=(16, 6))
        ctk.CTkLabel(
            self, text=message, height=20, anchor="w", justify="left", wraplength=420,
            text_color=COLOR_TEXT_PRIMARY, font=ctk.CTkFont(family=FONT_FAMILY, size=13),
        ).pack(fill="x", padx=WINDOW_PADDING)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=WINDOW_PADDING, pady=(SECTION_GAP, 16))
        primary = ActionButton(
            actions, text="继续" if confirm else "确定", width=76, kind="primary",
            command=self._accept,
        )
        primary.pack(side="right")
        first_focus = primary
        if confirm:
            first_focus = ActionButton(actions, text="取消", width=76, command=self.destroy)
            first_focus.pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())
        self.update_idletasks()
        height = round(self._reverse_window_scaling(self.winfo_reqheight()))
        self.center_over_parent(466, max(140, height))
        self.present()
        if confirm:
            self.grab_set()
        first_focus._canvas.focus_set()

    def _accept(self):
        self.confirmed = True
        self.destroy()


def ask_confirmation(owner, title: str, message: str) -> bool:
    prompt = Prompt(owner, title, message, confirm=True)
    prompt.wait_window()
    return prompt.confirmed


def show_message(owner, title: str, message: str):
    return Prompt(owner, title, message, confirm=False)
