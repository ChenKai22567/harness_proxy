import tkinter as tk
from typing import Dict, List, Optional, Tuple

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk


def draw_straight_glyph(draw, kind: str, color, size: int = 20, line_width: int = 2):
    """Draw the launcher's angular check/cross glyph onto a PIL image.

    The dropdown menu and the status chips share this exact two-segment
    geometry, so the mark is guaranteed to look the same everywhere.
    """
    if kind == "check":
        draw.line((3, 10, 7, 14), fill=color, width=line_width)
        draw.line((7, 14, 17, 4), fill=color, width=line_width)
    else:  # cross
        draw.line((4, 4, 16, 16), fill=color, width=line_width)
        draw.line((16, 4, 4, 16), fill=color, width=line_width)


def create_status_glyph_image(kind: str, color, display_size: int = 10) -> ctk.CTkImage:
    """Render one angular glyph as a CTkImage for use inside labels."""
    canvas = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    draw_straight_glyph(ImageDraw.Draw(canvas), kind, color)
    return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=(display_size, display_size))


def render_straight_glyph_photo(box_size: int, color: str) -> ImageTk.PhotoImage:
    """Rasterise the dropdown-style check at native pixel size.

    PIL draws both arms as exact 45-degree whole-pixel segments with a filled
    joint, avoiding the canvas polyline MITER artefacts (bent arm, offset
    joint) that appear around fractional scaling factors.
    """
    image = Image.new("RGBA", (box_size, box_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = box_size / 20.0
    arm = max(3, round(4 * unit))        # short arm step (down-right)
    long_arm = max(6, round(10 * unit))  # long arm step (up-right)
    elbow_x = max(arm, (box_size - (arm + long_arm)) // 2 + arm)
    elbow_y = max(long_arm, (box_size - long_arm) // 2 + long_arm)
    draw.line(
        [
            (elbow_x - arm, elbow_y - arm),
            (elbow_x, elbow_y),
            (elbow_x + long_arm, elbow_y - long_arm),
        ],
        fill=color,
        width=max(2, round(2 * unit)),
        joint="curve",
    )
    return ImageTk.PhotoImage(image)


def _install_crisp_geometry_patch():
    """Snap DrawEngine geometry to whole pixels under fractional DPI factors.

    At 160% scaling every 1 px card border becomes a 1.604 px polygon stroke.
    Tk's canvas does not anti-alias and rounds each vertex independently, so
    the border lands on different pixels on every edge and dense card layouts
    (the component dialog) read as fuzzy.  Rounding the engine inputs keeps
    the vertices on whole pixels; colours and canvas tags are untouched.
    customtkinter is pinned to 6.0.0, so this signature is stable.
    """
    original = ctk.DrawEngine.draw_rounded_rect_with_border
    if getattr(original, "_crisp_snapped", False):
        return

    def snapped(self, width, height, corner_radius, border_width, *args, **kwargs):
        # border_width == 0 means "no border" and must stay 0; only fractional
        # positive widths (1.604 px at 160% DPI) snap down to a whole pixel.
        snapped_border = border_width if not border_width else max(1, int(border_width))
        return original(
            self,
            round(width),
            round(height),
            round(corner_radius),
            snapped_border,
            *args,
            **kwargs,
        )

    snapped._crisp_snapped = True
    ctk.DrawEngine.draw_rounded_rect_with_border = snapped


_install_crisp_geometry_patch()


class PolishedComboBox(ctk.CTkComboBox):
    """Compact read-only picker with a custom, non-modal popup.

    CustomTkinter normally delegates the expanded list to ``tk.Menu``, whose
    Windows rendering cannot match the rest of the interface. This control
    keeps CTkComboBox's small API while drawing a clean outlined button section
    and a real CustomTkinter list. The popup never takes a Tk grab, so a failed
    or hidden popup cannot block the app.
    """

    def __init__(
        self,
        *args,
        focus_color: str = "#A8A8A8",
        arrow_color: Optional[str] = None,
        menu_active_text_color: Optional[str] = None,
        toggle_border_color: str = "#BEBEBE",
        popup_border_color: str = "#D0D0D0",
        popup_selected_color: str = "#EDEDED",
        **kwargs,
    ):
        self._focus_color = focus_color
        self._arrow_color = arrow_color or "#5F5F5F"
        self._menu_active_text_color = menu_active_text_color or "#242424"
        self._toggle_border_color = toggle_border_color
        self._popup_border_color = popup_border_color
        self._popup_selected_color = popup_selected_color
        self._popup_fg_color = kwargs.get("dropdown_fg_color", "#FFFFFF")
        self._popup_hover_color = kwargs.get("dropdown_hover_color", "#F0F0F0")
        self._popup_text_color = kwargs.get("dropdown_text_color", "#242424")
        self._popup_font = kwargs.get("dropdown_font") or kwargs.get("font")
        self._toggle_hover_color = kwargs.get("button_hover_color", "#F0F0F0")
        self._popup_transparent_key: Optional[str] = None

        kwargs.setdefault("state", "readonly")
        kwargs.setdefault("justify", "left")
        kwargs.setdefault("height", 30)
        kwargs.setdefault("corner_radius", 6)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", "#C7C7C7")
        kwargs.setdefault("fg_color", "#FFFFFF")
        kwargs.setdefault("button_color", "#FFFFFF")
        kwargs.setdefault("button_hover_color", "#FFFFFF")
        kwargs.setdefault("dropdown_fg_color", "#FFFFFF")
        kwargs.setdefault("dropdown_hover_color", "#F0F0F0")
        kwargs.setdefault("dropdown_text_color", "#242424")
        super().__init__(*args, **kwargs)

        if self._values and not self.get():
            self.set(self._values[0])

        self._check_image, self._empty_check_image = self._create_check_images()
        self._resting_border_color = self.cget("border_color")
        self._popup: Optional[tk.Toplevel] = None
        self._popup_frame: Optional[ctk.CTkFrame] = None
        self._popup_buttons: List[ctk.CTkButton] = []
        self._popup_index = 0
        self._owner = self.winfo_toplevel()
        self._owner_click_binding = self._owner.bind(
            "<ButtonPress-1>", self._on_owner_click, add="+"
        )
        self._owner_focus_binding = self._owner.bind(
            "<FocusOut>", self._schedule_focus_check, add="+"
        )

        self._create_accessible_bindings()
        self._draw()

    def _create_check_images(self):
        """Build a straight two-segment check and an equal-size spacer."""
        size = 20
        selected = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        # Draw the two strokes separately: no curved font outline and no
        # rounded joint interpolation between the short and long segments.
        draw_straight_glyph(
            ImageDraw.Draw(selected),
            "check",
            self._apply_appearance_mode(self._menu_active_text_color),
        )
        empty = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        return (
            ctk.CTkImage(light_image=selected, dark_image=selected, size=(10, 10)),
            ctk.CTkImage(light_image=empty, dark_image=empty, size=(10, 10)),
        )

    def _create_accessible_bindings(self):
        try:
            self._entry.configure(cursor="hand2")
            self._entry.bind("<Button-1>", self._on_entry_click, add="+")
            self._entry.bind("<Return>", self._on_keyboard_open, add="+")
            self._entry.bind("<space>", self._on_keyboard_open, add="+")
            self._entry.bind("<Down>", self._on_keyboard_open, add="+")
            self._entry.bind("<Up>", self._on_keyboard_open, add="+")
            self._entry.bind("<Home>", self._on_keyboard_open, add="+")
            self._entry.bind("<End>", self._on_keyboard_open, add="+")
            self._entry.bind("<Alt-Down>", self._on_keyboard_open, add="+")
            self._entry.bind("<Escape>", self._on_escape, add="+")
            self._entry.bind("<FocusIn>", self._on_focus_in, add="+")
            self._entry.bind("<FocusOut>", self._on_focus_out, add="+")
        except tk.TclError:
            pass

    def _on_entry_click(self, _event=None):
        self._clicked()
        return "break"

    def _on_keyboard_open(self, _event=None):
        if self._is_popup_open():
            keysym = str(getattr(_event, "keysym", "")).lower()
            if keysym in {"return", "space"}:
                return self._commit_popup_selection()
            if keysym == "down":
                return self._move_popup_selection(1)
            if keysym == "up":
                return self._move_popup_selection(-1)
            if keysym == "home":
                return self._set_popup_selection(0)
            if keysym == "end":
                return self._set_popup_selection(len(self._values) - 1)
        else:
            self._open_dropdown_menu()
        return "break"

    def _on_escape(self, _event=None):
        if self._is_popup_open():
            self._close_popup(restore_focus=True)
            return "break"
        return None

    def _on_focus_in(self, _event=None):
        try:
            super().configure(border_color=self._focus_color)
        except tk.TclError:
            pass

    def _on_focus_out(self, _event=None):
        if self._is_popup_open():
            return
        try:
            super().configure(border_color=self._resting_border_color)
        except tk.TclError:
            pass

    def _on_enter(self, event=0):
        """Hover only the button interior; never replace its outer outline."""
        super()._on_enter(event)
        try:
            color = self._popup_selected_color if self._is_popup_open() else self._toggle_hover_color
            self._canvas.itemconfigure(
                "inner_parts_right",
                outline=self._apply_appearance_mode(color),
                fill=self._apply_appearance_mode(color),
            )
            self._restore_button_outline()
        except tk.TclError:
            pass

    def _on_leave(self, event=0):
        """Restore the neutral button interior without creating a white rim."""
        super()._on_leave(event)
        try:
            color = self._popup_selected_color if self._is_popup_open() else self._button_color
            self._canvas.itemconfigure(
                "inner_parts_right",
                outline=self._apply_appearance_mode(color),
                fill=self._apply_appearance_mode(color),
            )
            self._restore_button_outline()
        except tk.TclError:
            pass

    def _restore_button_outline(self):
        border_color = self._apply_appearance_mode(self._border_color)
        self._canvas.itemconfigure(
            "border_parts_right",
            outline=border_color,
            fill=border_color,
        )

    def _set_toggle_visual(self, *, opened: bool):
        """Use the combo canvas as the button so no child-widget halo appears."""
        color = self._popup_selected_color if opened else self._button_color
        self._canvas.itemconfigure(
            "inner_parts_right",
            outline=self._apply_appearance_mode(color),
            fill=self._apply_appearance_mode(color),
        )
        self._restore_button_outline()

        width = self._apply_widget_scaling(self._current_width)
        height = self._apply_widget_scaling(self._current_height)
        center_x = width - (height / 2)
        center_y = height / 2
        # A compact 7 px chevron reads closer to Windows' system controls than
        # CTk's default height-derived glyph, which becomes visually heavy at
        # 150–175% display scaling.
        size = self._apply_widget_scaling(7)
        if opened:
            points = (
                center_x - (size / 2), center_y + (size / 5),
                center_x, center_y - (size / 5),
                center_x + (size / 2), center_y + (size / 5),
            )
        else:
            points = (
                center_x - (size / 2), center_y - (size / 5),
                center_x, center_y + (size / 5),
                center_x + (size / 2), center_y - (size / 5),
            )
        self._canvas.coords("polished_chevron", *points)

    @staticmethod
    def _is_widget_within(widget, ancestor) -> bool:
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    def _on_owner_click(self, event=None):
        """Close only for a genuine click outside this combo and its popup."""
        if not self._is_popup_open():
            return None
        widget = getattr(event, "widget", None)
        if self._is_widget_within(widget, self):
            return None
        if self._is_widget_within(widget, self._popup):
            return None
        self._close_popup(restore_focus=False)
        return None

    def _is_popup_open(self) -> bool:
        try:
            return self._popup is not None and bool(self._popup.winfo_exists())
        except tk.TclError:
            return False

    def _open_dropdown_menu(self):
        if self._state == tk.DISABLED or not self._values or self._is_popup_open():
            return

        self.update_idletasks()
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.resizable(False, False)
        # A Toplevel is rectangular even when the CTkFrame inside it is
        # rounded.  Painting the window itself with the grey border colour
        # exposes four rectangular corner pixels around the rounded frame.
        # Chroma-key those outer pixels on Windows; use the white menu surface
        # as a graceful fallback on platforms without transparentcolor.
        popup_corner_color = self._popup_fg_color
        if self._windowingsystem == "win32":
            transparent_key = "#010203"
            try:
                popup.configure(bg=transparent_key)
                popup.wm_attributes("-transparentcolor", transparent_key)
                popup_corner_color = transparent_key
                self._popup_transparent_key = transparent_key
            except tk.TclError:
                popup.configure(bg=self._popup_fg_color)
                self._popup_transparent_key = None
        else:
            popup.configure(bg=self._popup_fg_color)
            self._popup_transparent_key = None
        try:
            popup.transient(self.winfo_toplevel())
        except tk.TclError:
            pass

        self._popup = popup
        self._popup_buttons = []
        try:
            self._popup_index = self._values.index(self.get())
        except ValueError:
            self._popup_index = 0

        frame = ctk.CTkFrame(
            popup,
            bg_color=popup_corner_color,
            fg_color=self._popup_fg_color,
            border_width=1,
            border_color=self._popup_border_color,
            corner_radius=7,
        )
        frame.pack(fill="both", expand=True)
        self._popup_frame = frame

        row_width = max(120, int(self._desired_width) - 10)
        for index, value in enumerate(self._values):
            button = ctk.CTkButton(
                frame,
                text=self._popup_label(index, str(value)),
                image=self._check_image if index == self._popup_index else self._empty_check_image,
                compound="left",
                width=row_width,
                height=34,
                anchor="w",
                corner_radius=5,
                border_width=0,
                border_spacing=9,
                fg_color=self._popup_selected_color if index == self._popup_index else "transparent",
                hover_color=self._popup_hover_color,
                text_color=self._popup_text_color,
                font=self._popup_font,
                command=lambda selected=str(value): self._select_from_popup(selected),
            )
            button.pack(
                fill="x",
                padx=4,
                pady=(4 if index == 0 else 1, 4 if index == len(self._values) - 1 else 1),
            )
            self._popup_buttons.append(button)

        popup.bind("<Escape>", lambda _event: self._close_popup(restore_focus=True) or "break")
        popup.bind("<Up>", lambda _event: self._move_popup_selection(-1))
        popup.bind("<Down>", lambda _event: self._move_popup_selection(1))
        popup.bind("<Home>", lambda _event: self._set_popup_selection(0))
        popup.bind("<End>", lambda _event: self._set_popup_selection(len(self._values) - 1))
        popup.bind("<Return>", self._commit_popup_selection)

        popup.update_idletasks()
        popup_width = max(self.winfo_width(), popup.winfo_reqwidth())
        popup_height = popup.winfo_reqheight()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 4
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = max(8, min(x, screen_width - popup_width - 8))
        if y + popup_height > screen_height - 8:
            y = max(8, self.winfo_rooty() - popup_height - 4)
        popup.geometry(f"{popup_width}x{popup_height}+{x}+{y}")
        popup.deiconify()
        popup.lift()

        try:
            # Keep focus on the entry.  Forcing an override-redirect popup to
            # take focus during ButtonPress makes the matching ButtonRelease
            # return focus to the owner and instantly fires a false FocusOut.
            self._entry.focus_set()
            super().configure(border_color=self._focus_color)
            self._set_toggle_visual(opened=True)
        except tk.TclError:
            pass

    def _popup_label(self, index: int, value: str) -> str:
        return value

    def _set_popup_selection(self, index: int):
        if not self._is_popup_open() or not self._popup_buttons:
            return "break"
        self._popup_index = max(0, min(index, len(self._popup_buttons) - 1))
        for item_index, button in enumerate(self._popup_buttons):
            button.configure(
                text=self._popup_label(item_index, str(self._values[item_index])),
                image=self._check_image if item_index == self._popup_index else self._empty_check_image,
                fg_color=self._popup_selected_color if item_index == self._popup_index else "transparent",
            )
        return "break"

    def _move_popup_selection(self, delta: int):
        if not self._popup_buttons:
            return "break"
        return self._set_popup_selection((self._popup_index + delta) % len(self._popup_buttons))

    def _commit_popup_selection(self, _event=None):
        if self._values:
            self._select_from_popup(str(self._values[self._popup_index]))
        return "break"

    def _select_from_popup(self, value: str):
        self._close_popup(restore_focus=False)
        self._dropdown_callback(value)
        try:
            self._entry.focus_set()
        except tk.TclError:
            pass

    def _schedule_focus_check(self, _event=None):
        try:
            self.after(60, self._close_if_focus_left)
        except tk.TclError:
            pass

    def _close_if_focus_left(self):
        if not self._is_popup_open():
            return
        try:
            current = self.focus_get()
            if self._is_widget_within(current, self) or self._is_widget_within(current, self._popup):
                return
        except tk.TclError:
            pass
        self._close_popup(restore_focus=False)

    def _close_popup(self, *, restore_focus: bool = False):
        popup = self._popup
        self._popup = None
        self._popup_frame = None
        self._popup_buttons = []
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        try:
            super().configure(border_color=self._focus_color if restore_focus else self._resting_border_color)
            self._set_toggle_visual(opened=False)
            if restore_focus:
                self._entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def _clicked(self, event=None):
        if self._is_popup_open():
            self._close_popup(restore_focus=True)
            return
        try:
            self._entry.focus_set()
        except tk.TclError:
            pass
        if self._state != tk.DISABLED and self._values:
            self._open_dropdown_menu()

    def configure(self, require_redraw=False, **kwargs):
        values_changed = "values" in kwargs
        state_changed = "state" in kwargs
        result = super().configure(require_redraw=require_redraw, **kwargs)
        if values_changed:
            self._close_popup(restore_focus=False)
        if state_changed:
            self._draw()
        return result

    config = configure

    def destroy(self):
        self._close_popup(restore_focus=False)
        try:
            if self._owner_click_binding:
                self._owner.unbind("<ButtonPress-1>", self._owner_click_binding)
            if self._owner_focus_binding:
                self._owner.unbind("<FocusOut>", self._owner_focus_binding)
        except tk.TclError:
            pass
        super().destroy()

    def _draw(self, no_color_updates=False):
        super()._draw(no_color_updates)
        try:
            self._canvas.itemconfigure("dropdown_arrow", state="hidden")
            if not self._canvas.find_withtag("polished_chevron"):
                self._canvas.create_line(
                    0, 0, 0, 0, 0, 0,
                    tags="polished_chevron",
                    width=1,
                    capstyle="round",
                    joinstyle="round",
                )
                self._canvas.tag_bind("polished_chevron", "<Enter>", self._on_enter)
                self._canvas.tag_bind("polished_chevron", "<Leave>", self._on_leave)
                self._canvas.tag_bind("polished_chevron", "<Button-1>", self._clicked)
            self._canvas.itemconfigure(
                "polished_chevron",
                fill=self._apply_appearance_mode(
                    self._text_color_disabled if self._state == tk.DISABLED else self._arrow_color
                ),
            )
            self._set_toggle_visual(opened=getattr(self, "_popup", None) is not None)

            separator_x = self._apply_widget_scaling(self._current_width - self._current_height)
            top = self._apply_widget_scaling(max(3, self._border_width + 2))
            bottom = self._apply_widget_scaling(self._current_height) - top
            if not self._canvas.find_withtag("polished_toggle_separator"):
                self._canvas.create_line(
                    separator_x,
                    top,
                    separator_x,
                    bottom,
                    tags="polished_toggle_separator",
                    width=max(1, round(self._apply_widget_scaling(1))),
                )
                self._canvas.tag_bind("polished_toggle_separator", "<Enter>", self._on_enter)
                self._canvas.tag_bind("polished_toggle_separator", "<Leave>", self._on_leave)
                self._canvas.tag_bind("polished_toggle_separator", "<Button-1>", self._clicked)
            else:
                self._canvas.coords("polished_toggle_separator", separator_x, top, separator_x, bottom)
            self._canvas.itemconfigure(
                "polished_toggle_separator",
                fill=self._apply_appearance_mode(self._toggle_border_color),
            )
            self._canvas.tag_raise("polished_toggle_separator")
            self._canvas.tag_raise("polished_chevron")
        except tk.TclError:
            pass


class CrispCheckBox(ctk.CTkCheckBox):
    """Standard rectangular checkbox with an integer-pixel rendering.

    The stock draw engine multiplies every geometry by the display scaling
    factor (1.604 at 160% DPI), so the 18 px box becomes a 28.9 px canvas with
    a 1.6 px polygon border and a round-capped check whose vertices all carry
    fractions.  Tk's canvas does not anti-alias and rounds each vertex
    independently, which reads as a wobbly rounded box with a fuzzy check.
    This subclass repaints the box itself with integer-coordinate rectangles
    and draws the same two-segment straight check as the dropdown menu, while
    keeping CTkCheckBox's variable/command/text/hover behaviour.
    """

    # Class attribute: CTkCheckBox.__init__ calls _draw() before subclass
    # instance attributes exist, so the hover flag must already resolve.
    _hovering = False

    def __init__(self, *args, **kwargs):
        # corner_radius stays accepted for call-site compatibility but is
        # deliberately unused: the box is drawn as a true rectangle.
        super().__init__(*args, **kwargs)

    def _box_pixel_size(self):
        scaled_width = self._apply_widget_scaling(self._checkbox_width)
        scaled_height = self._apply_widget_scaling(self._checkbox_height)
        width = max(3, round(scaled_width))
        height = max(3, round(scaled_height))
        if self._checkbox_width == self._checkbox_height:
            # The box is designed square; rounding the two scaled values
            # independently could still yield W != H at fractional DPI.
            height = width
        return width, height

    def _draw(self, no_color_updates=False):
        width, height = self._box_pixel_size()
        canvas = self._canvas
        try:
            if int(float(canvas.cget("width"))) != width or int(float(canvas.cget("height"))) != height:
                canvas.configure(width=width, height=height)
            canvas.delete("border_parts", "inner_parts", "checkmark", "crisp_parts")

            background = self._apply_appearance_mode(self._bg_color)
            if self._check_state:
                fill = self._fg_color
                if self._hovering and self._state == tk.NORMAL:
                    fill = self._hover_color
                if self._state == tk.DISABLED:
                    fill = self._border_color
                fill = self._apply_appearance_mode(fill)
                self._draw_rounded_fill(canvas, width, height, fill)
                mark = background if self._state == tk.DISABLED else self._checkmark_color
                self._draw_straight_mark(canvas, width, height, self._apply_appearance_mode(mark))
            else:
                border = self._border_color
                if self._hovering and self._state == tk.NORMAL:
                    border = self._fg_color
                self._draw_rounded_outline(
                    canvas, width, height, self._apply_appearance_mode(border), background
                )
            canvas.configure(bg=background)
            if self._bg_canvas is not None:
                self._bg_canvas.configure(bg=background)
            if self._text_label is not None:
                if self._state == tk.DISABLED:
                    text_color = self._text_color_disabled
                else:
                    text_color = self._text_color
                self._text_label.configure(
                    fg=self._apply_appearance_mode(text_color),
                    bg=background,
                )
        except tk.TclError:
            pass

    def _draw_rounded_fill(self, canvas, width: int, height: int, color: str):
        """Solid fill with a ~2 px corner cut, every coordinate an integer."""
        canvas.create_rectangle(2, 0, width - 3, height - 1, fill=color, outline=color, tags=("crisp_parts",))
        canvas.create_rectangle(0, 2, width - 1, height - 3, fill=color, outline=color, tags=("crisp_parts",))
        canvas.create_rectangle(1, 1, width - 2, height - 2, fill=color, outline=color, tags=("crisp_parts",))

    def _draw_rounded_outline(self, canvas, width: int, height: int, color: str, background: str):
        """1 px outline with a ~2 px corner cut, every coordinate an integer."""
        fill = background
        canvas.create_rectangle(2, 0, width - 3, height - 1, fill=fill, outline=fill, tags=("crisp_parts",))
        canvas.create_rectangle(0, 2, width - 1, height - 3, fill=fill, outline=fill, tags=("crisp_parts",))
        canvas.create_rectangle(1, 1, width - 2, height - 2, fill=fill, outline=fill, tags=("crisp_parts",))
        edges = (
            (2, 0, width - 3, 0),
            (2, height - 1, width - 3, height - 1),
            (0, 2, 0, height - 3),
            (width - 1, 2, width - 1, height - 3),
        )
        for x0, y0, x1, y1 in edges:
            canvas.create_line(x0, y0, x1, y1, fill=color, tags=("crisp_parts",))
        # One pixel per corner joins the two edges across the cut corner.
        # Anything more lands inside the cut-out and reads as a stray dot.
        corner_pixels = (
            (1, 1),
            (width - 2, 1),
            (1, height - 2),
            (width - 2, height - 2),
        )
        for x, y in corner_pixels:
            canvas.create_rectangle(x, y, x, y, fill=color, outline=color, tags=("crisp_parts",))

    def _draw_straight_mark(self, canvas, width: int, height: int, color: str):
        photo = self._get_mark_photo(width, color)
        self._active_mark_photo = photo  # prevent Tk image garbage collection
        canvas.create_image(width // 2, height // 2, image=photo, tags=("crisp_parts",))

    def _get_mark_photo(self, box_size: int, color: str) -> ImageTk.PhotoImage:
        cache: Dict[Tuple[int, str], ImageTk.PhotoImage] = getattr(self, "_mark_photo_cache", None)
        if cache is None:
            cache = self._mark_photo_cache = {}
        key = (box_size, color)
        if key not in cache:
            cache[key] = render_straight_glyph_photo(box_size, color)
        return cache[key]

    def _on_enter(self, event=0):
        if self._hover is True and self._state == tk.NORMAL:
            self._hovering = True
            self._draw()

    def _on_leave(self, event=0):
        self._hovering = False
        self._draw()
