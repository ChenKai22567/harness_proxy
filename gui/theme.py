"""Shared neutral desktop design tokens for every window.

The palette is deliberately Windows-aligned: surfaces stay within one
cool-grey family, and colour is reserved for compact status feedback.
GUI modules import with ``from gui.theme import *``; ``__all__`` below is
the authoritative list of public names, so a token can never silently
diverge between the main window and the dialogs again.
"""

FONT_FAMILY = "Microsoft YaHei UI"

# Surfaces and borders
COLOR_BG = "#F3F3F3"
COLOR_CARD_BG = "#FFFFFF"
COLOR_CARD_SOFT = "#F7F7F7"
COLOR_CARD_HOVER = "#F0F0F0"
COLOR_CARD_BORDER = "#E1E1E1"
COLOR_BORDER_STRONG = "#C7C7C7"

# Text
COLOR_TEXT_HEADING = "#1F1F1F"
COLOR_TEXT_PRIMARY = "#242424"
COLOR_TEXT_SECTION = "#3A3A3A"
COLOR_TEXT_MUTED = "#666666"
COLOR_TEXT_SUBTLE = "#8A8A8A"

# Accent (primary actions stay neutral grey)
COLOR_ACCENT = "#3B3B3B"
COLOR_ACCENT_HOVER = "#2F2F2F"
COLOR_ACCENT_SOFT = "#EEEEEE"

# Semantic status colours, used on small badges only
COLOR_SUCCESS = "#0AA36D"          # refined emerald green
COLOR_SUCCESS_HOVER = "#088A5C"
COLOR_SUCCESS_BG = "#E7F7F0"
COLOR_SUCCESS_TEXT = "#0A8055"

COLOR_WARN = "#F28A00"             # warm tech amber
COLOR_WARN_BG = "#FFF7EB"
COLOR_WARN_TEXT = "#B45309"

COLOR_ERROR = "#D95765"            # soft rose-red
COLOR_ERROR_BG = "#FDF0F1"
COLOR_ERROR_BORDER = "#F8CCD1"
COLOR_ERROR_TEXT = "#C53041"
COLOR_ERROR_HOVER = "#FCE1E4"

COLOR_INACTIVE_BG = "#EEEEEE"
COLOR_INACTIVE_TEXT = "#808080"

# Log console
COLOR_LOG_BG = "#F7F8FA"
COLOR_LOG_BORDER = "#E1E5EA"
COLOR_LOG_TEXT = "#263548"
COLOR_LOG_CONTROL = "#EEF1F4"
COLOR_LOG_CONTROL_HOVER = "#E2E6EA"
COLOR_SCROLLBAR = "#C8D0DA"
COLOR_SCROLLBAR_HOVER = "#AEB8C5"

__all__ = [
    "FONT_FAMILY",
    "COLOR_BG",
    "COLOR_CARD_BG",
    "COLOR_CARD_SOFT",
    "COLOR_CARD_HOVER",
    "COLOR_CARD_BORDER",
    "COLOR_BORDER_STRONG",
    "COLOR_TEXT_HEADING",
    "COLOR_TEXT_PRIMARY",
    "COLOR_TEXT_SECTION",
    "COLOR_TEXT_MUTED",
    "COLOR_TEXT_SUBTLE",
    "COLOR_ACCENT",
    "COLOR_ACCENT_HOVER",
    "COLOR_ACCENT_SOFT",
    "COLOR_SUCCESS",
    "COLOR_SUCCESS_HOVER",
    "COLOR_SUCCESS_BG",
    "COLOR_SUCCESS_TEXT",
    "COLOR_WARN",
    "COLOR_WARN_BG",
    "COLOR_WARN_TEXT",
    "COLOR_ERROR",
    "COLOR_ERROR_BG",
    "COLOR_ERROR_BORDER",
    "COLOR_ERROR_TEXT",
    "COLOR_ERROR_HOVER",
    "COLOR_INACTIVE_BG",
    "COLOR_INACTIVE_TEXT",
    "COLOR_LOG_BG",
    "COLOR_LOG_BORDER",
    "COLOR_LOG_TEXT",
    "COLOR_LOG_CONTROL",
    "COLOR_LOG_CONTROL_HOVER",
    "COLOR_SCROLLBAR",
    "COLOR_SCROLLBAR_HOVER",
]
