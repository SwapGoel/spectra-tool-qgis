"""Light and dark palettes for the Spectral Tool panel, mirrored from Blink's
:root tokens.

The panel follows whatever QGIS is showing. On macOS QGIS follows the system
appearance,
so switching the OS between Light and Dark flips the panel with it. A QGIS UI
theme that
is dark on its own (Night Mapping and friends) is honoured as well.
"""

from qgis.PyQt.QtCore import Qt, QRectF, QPointF
from qgis.PyQt.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QBrush,
)
from qgis.PyQt.QtWidgets import QApplication


def log(message):
    """Write to the 'Spectral Tool' tab of the QGIS Log Messages panel."""
    try:
        from qgis.core import Qgis, QgsMessageLog

        level = getattr(getattr(Qgis, "MessageLevel", Qgis), "Info")
        QgsMessageLog.logMessage(str(message), "Spectral Tool", level)
    except ImportError:  # outside QGIS
        print("Spectral Tool:", message)


# Tokens copied from blink/static/index.html (:root and :root[data-
# theme="light"]).

DARK = {
    "sunken": "#050607",
    "surface": "#0b0d0f",
    "bg": "#0f1113",
    "panel": "#17191b",
    "edge_soft": "#1f2326",
    "edge": "#2c3236",
    "edge_strong": "#2f3338",
    "text_bright": "#ffffff",
    "text": "#e6eef3",
    "text_alt": "#e8ecf1",
    "text_dim": "#b9c3cc",
    "text_mute": "#8892a6",
    "accent": "#0b84ff",
    "danger": "#ff4d4d",
    "ok": "#4cff00",
    "inset": "rgba(0,0,0,0.2)",
}
LIGHT = {
    "sunken": "#e4e8ee",
    "surface": "#eef1f5",
    "bg": "#f5f7fa",
    "panel": "#ffffff",
    "edge_soft": "#e2e7ec",
    "edge": "#d4dae1",
    "edge_strong": "#c3cad3",
    "text_bright": "#000000",
    "text": "#16191d",
    "text_alt": "#1b1f24",
    "text_dim": "#3d454e",
    "text_mute": "#5c6673",
    "accent": "#0b84ff",
    "danger": "#c62828",
    "ok": "#2e7d32",
    "inset": "rgba(0,0,0,0.05)",
}

# Blink's curve palette (03-state.js PALETTE). Neon reads well on the dark
# plot; on the light plot white and pure yellow vanish, so the light theme uses
# deeper equivalents in the same order. A curve and its map marker always share
# one colour - the colour is picked when the spectrum is read.
CURVES_DARK = [
    "#00d9ff",
    "#ff3366",
    "#ffd700",
    "#00ff00",
    "#ff8800",
    "#b203ed",
    "#ff00ff",
    "#00ffff",
    "#ffff00",
    "#ffffff",
]
CURVES_LIGHT = [
    "#0097a7",
    "#e0245e",
    "#b8860b",
    "#2e9e2e",
    "#e07000",
    "#8e24aa",
    "#c2185b",
    "#0277bd",
    "#827717",
    "#37474f",
]

_DARK_UI_THEMES = ("night", "dark", "blend of gray")


def is_dark():
    """True when QGIS is currently showing a dark UI."""
    try:
        from qgis.core import QgsSettings

        name = str(QgsSettings().value("UI/UITheme", "default") or "").lower()
        if any(k in name for k in _DARK_UI_THEMES):
            return True
    except Exception as e:
        log(f"could not read the QGIS UI theme: {e}")
    app = QApplication.instance()
    pal = app.palette() if app else QPalette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


def tokens(dark):
    return DARK if dark else LIGHT


def curve_palette(dark):
    return CURVES_DARK if dark else CURVES_LIGHT


def stylesheet(t):
    hover = QColor(t["accent"]).lighter(120).name()
    return f"""
    QWidget#spectraRoot, QScrollArea#spectraScroll {{
        background: {t['edge_soft']};
    }}
    QScrollArea#spectraScroll {{ border: none; }}
    QLabel {{ background: transparent; }}
    QLabel#status {{ color: {t['text_mute']}; font-size: 10px; }}
    QLabel#status[level="error"] {{ color: {t['danger']}; }}
    QPushButton {{
        border: none; border-radius: 6px; padding: 6px; font-size: 10px;
        font-weight: 700; min-height: 14px;
    }}
    QPushButton[kind="primary"] {{
        background: {t['accent']}; color: #ffffff;
    }}
    QPushButton[kind="primary"]:hover {{ background: {hover}; }}
    QPushButton[kind="ghost"] {{
        background: {t['edge_strong']}; color: {t['text']};
    }}
    QPushButton[kind="ghost"]:hover {{ background: {t['edge']}; }}
    QPushButton[kind="danger"] {{
        background: {t['edge_strong']}; color: {t['danger']};
        border: 1px solid {t['danger']};
    }}
    QPushButton[kind="tool"] {{ padding: 4px; }}
    QFrame#inset {{
        background: {t['inset']}; border: 1px solid {t['edge']};
        border-radius: 4px;
    }}
    QFrame#limits {{
        background: {t['panel']}; border: 1px solid {t['edge']};
        border-radius: 4px;
    }}
    QFrame#limits QLabel {{ color: {t['text']}; font-size: 11px; }}
    QFrame#limits QLabel[muted="1"] {{ color: {t['text_mute']}; }}
    QFrame#limits QLineEdit {{
        background: {t['bg']}; border: 1px solid {t['edge']};
        color: {t['text_alt']}; padding: 2px; font-size: 10px;
    }}
    QFrame#plotWrap {{ background: {t['sunken']}; border-radius: 6px; }}
    QWidget#legend {{ background: transparent; }}
    """


# ---------------------------------------------------------------------------
# Icons, drawn rather than shipped, so they take the theme's colour.
# ---------------------------------------------------------------------------
def _arrow(p, tip, direction, s=2.6):
    dx, dy = direction
    a = QPointF(tip.x() - dx * s + dy * s, tip.y() - dy * s + dx * s)
    b = QPointF(tip.x() - dx * s - dy * s, tip.y() - dy * s - dx * s)
    p.drawLine(a, tip)
    p.drawLine(b, tip)


def icon(kind, color, size=14):
    scale = 3
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size * scale / 16.0, size * scale / 16.0)
    col = QColor(color)
    pen = QPen(col, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if kind == "pan":
        p.drawLine(QPointF(8, 1.5), QPointF(8, 14.5))
        p.drawLine(QPointF(1.5, 8), QPointF(14.5, 8))
        _arrow(p, QPointF(8, 1.5), (0, -1))
        _arrow(p, QPointF(8, 14.5), (0, 1))
        _arrow(p, QPointF(1.5, 8), (-1, 0))
        _arrow(p, QPointF(14.5, 8), (1, 0))
    elif kind == "zoom":
        dash = QPen(col, 1.4)
        dash.setDashPattern([2, 1.6])
        p.setPen(dash)
        p.drawRect(QRectF(2, 2, 10, 10))
        p.setPen(pen)
        p.drawLine(QPointF(11, 11), QPointF(14.5, 14.5))
    elif kind == "limits":
        for y, kx in ((3.5, 5), (8, 11), (12.5, 7)):
            p.drawLine(QPointF(1.5, y), QPointF(14.5, y))
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(kx, y), 1.9, 1.9)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "swapx":
        p.drawLine(QPointF(1.5, 8), QPointF(14.5, 8))
        _arrow(p, QPointF(1.5, 8), (-1, 0), 3.2)
        _arrow(p, QPointF(14.5, 8), (1, 0), 3.2)
    elif kind == "home":
        path = QPainterPath(QPointF(1.5, 8))
        path.lineTo(8, 2)
        path.lineTo(14.5, 8)
        p.drawPath(path)
        p.drawRect(QRectF(3.8, 7.2, 8.4, 7))
        p.drawLine(QPointF(8, 14.2), QPointF(8, 10.5))
    elif kind == "trash":
        p.drawLine(QPointF(2.5, 4), QPointF(13.5, 4))
        path = QPainterPath(QPointF(6, 4))
        path.lineTo(6, 2)
        path.lineTo(10, 2)
        path.lineTo(10, 4)
        p.drawPath(path)
        body = QPainterPath(QPointF(4, 4))
        body.lineTo(5, 14.5)
        body.lineTo(11, 14.5)
        body.lineTo(12, 4)
        p.drawPath(body)
        p.drawLine(QPointF(7, 7), QPointF(7, 12))
        p.drawLine(QPointF(9, 7), QPointF(9, 12))
    elif kind == "expand":
        for x, y, dx, dy in (
            (2, 2, 1, 1),
            (14, 2, -1, 1),
            (2, 14, 1, -1),
            (14, 14, -1, -1),
        ):
            p.drawLine(QPointF(x, y), QPointF(x + 4 * dx, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + 4 * dy))
    elif kind == "crosshair":
        p.drawEllipse(QPointF(8, 8), 4.5, 4.5)
        for a, b in (
            ((8, 1), (8, 5)),
            ((8, 11), (8, 15)),
            ((1, 8), (5, 8)),
            ((11, 8), (15, 8)),
        ):
            p.drawLine(QPointF(*a), QPointF(*b))
    elif kind == "square":
        p.drawRoundedRect(QRectF(2.5, 2.5, 11, 11), 1.5, 1.5)
    p.end()
    return QIcon(pm)
