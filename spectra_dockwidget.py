"""The Spectral Tool panel - a port of Blink's spectra panel to a QGIS dock.

What it mirrors from Blink (static/js/19-charts.js, 20-boot.js):
  * Multi-Point / Multi-Layer and Point / Area toggles.
  * Pan, Box Zoom, Set Limits, Toggle X-Axis, Reset Zoom, Clear Graph.
  * Drag pans (Alt/Option-drag box-zooms), the wheel zooms; in Box Zoom mode it
    is the
    other way round.
  * The axes fit the data - both x AND y follow the spectra's own min and max.
    Once you
    pan, zoom or type a limit, the view is HELD: adding spectra no longer re-
    frames it
    until Reset is pressed.
  * A VIBGYOR band behind 400-700 nm on the wavelength axis.
  * A legend grouped by layer; click to hide a curve, shift-click to show only
    that one,
    click a layer name to hide or show all of its curves.
  * Light and dark themes that follow QGIS / the OS automatically.
"""

import math

import numpy as np
from qgis.PyQt.QtCore import (
    Qt,
    pyqtSignal,
    QTimer,
    QEvent,
    QRect,
    QSize,
    QPoint,
)
from qgis.PyQt.QtGui import QColor, QDoubleValidator
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDockWidget,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:  # matplotlib >= 3.5 picks Qt5 or Qt6 to match QGIS
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg as FigureCanvas,
    )
except ImportError:  # older matplotlib shipped with older QGIS 3.x
    from matplotlib.backends.backend_qt5agg import (
        FigureCanvasQTAgg as FigureCanvas,
    )
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator

from . import spectra_theme as theme

HIT_RADIUS_PX = 8  # Chart.js: point radius 3 + hitRadius 5
WHEEL_STEP = 0.1  # chartjs-plugin-zoom default wheel speed


def fmt_value(v):
    """Blink's fmtPlotValue: precision follows magnitude."""
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return str(v)
    a = abs(v)
    if a >= 1000:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def fmt_x(v):
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")


def nice_range(lo, hi):
    """Chart.js linear-scale bounds ('ticks'): data min/max widened to nice
    tick values."""
    if lo == hi:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    ticks = MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]).tick_values(lo, hi)
    below = [t for t in ticks if t <= lo + 1e-12 * abs(lo)]
    above = [t for t in ticks if t >= hi - 1e-12 * abs(hi)]
    return (below[-1] if below else lo), (above[0] if above else hi)


def hex_alpha(color, alpha):
    c = QColor(color)
    return (c.redF(), c.greenF(), c.blueF(), alpha)


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------
class FlowLayout(QLayout):
    """Wraps its items like CSS flex-wrap - for the legend rows."""

    def __init__(self, parent=None, hspacing=8, vspacing=3):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _layout(self, rect, test):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + self._h
            if nx - self._h > rect.right() and line_h > 0:
                x, y = rect.x(), y + line_h + self._v
                nx = x + hint.width() + self._h
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class Clickable(QWidget):
    clicked = pyqtSignal(object)  # Qt.KeyboardModifiers

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(e.modifiers())
        super().mousePressEvent(e)


# ---------------------------------------------------------------------------
# The dock
# ---------------------------------------------------------------------------
class SpectraDockWidget(QDockWidget):
    closingPlugin = pyqtSignal()
    clear_requested = pyqtSignal()
    mode_changed = pyqtSignal(str)  # 'point' | 'layer'
    shape_changed = pyqtSignal(str)  # 'point' | 'area'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SpectraDockWidget")
        self.setWindowTitle("Spectral Tool")

        self.traces = (
            []
        )  # {'label', 'color', 'data': [{band, value, wv}], 'hidden'}
        self.mode = "point"
        self.shape = "point"
        self.x_type = "band"
        self.x_auto = True  # until the user presses Toggle X-Axis
        self.interaction = "pan"
        self.held = False  # Blink's plotViewHeld['spectra']
        self.lim = {"xmin": None, "xmax": None, "ymin": None, "ymax": None}
        self._lines = []  # (trace, Line2D)
        self._drag = None
        self._hover_key = None
        self._fullscreen = False
        self._dark = theme.is_dark()
        self.t = theme.tokens(self._dark)

        self._build_ui()
        self._apply_theme()

        # Follow QGIS / the OS. Palette signals cover the normal case; the slow
        # poll is a safety net for platforms that change appearance without
        # telling Qt widgets.
        app = QApplication.instance()
        if hasattr(app, "paletteChanged"):  # Qt5; Qt6 relies on changeEvent
            app.paletteChanged.connect(
                lambda *_: QTimer.singleShot(0, self.check_theme)
            )
        self._theme_timer = QTimer(self)
        self._theme_timer.setInterval(2000)
        self._theme_timer.timeout.connect(self.check_theme)
        self._theme_timer.start()

        self.redraw()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setObjectName("spectraScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root = QWidget()
        root.setObjectName("spectraRoot")
        scroll.setWidget(root)
        self.setWidget(scroll)
        self._root = scroll

        v = QVBoxLayout(root)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        # Multi-Point / Multi-Layer
        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_mode_point = self._button("Multi-Point", "Multi-Point Mode")
        self.btn_mode_layer = self._button("Multi-Layer", "Multi-Layer Mode")
        self.btn_mode_point.clicked.connect(lambda: self.set_mode("point"))
        self.btn_mode_layer.clicked.connect(lambda: self.set_mode("layer"))
        row.addWidget(self.btn_mode_point)
        row.addWidget(self.btn_mode_layer)
        v.addLayout(row)

        # Point / Area
        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_shape_point = self._button(
            " Point", "Read one pixel", "crosshair"
        )
        self.btn_shape_area = self._button(
            " Area", "Drag a rectangle and average every pixel in it", "square"
        )
        self.btn_shape_point.clicked.connect(lambda: self.set_shape("point"))
        self.btn_shape_area.clicked.connect(lambda: self.set_shape("area"))
        row.addWidget(self.btn_shape_point)
        row.addWidget(self.btn_shape_area)
        v.addLayout(row)

        # Plot toolbar
        bar = QFrame()
        bar.setObjectName("inset")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(4, 4, 4, 4)
        bl.setSpacing(4)
        self.btn_pan = self._button(
            "", "Pan Mode (Alt-drag to box zoom)", "pan", tool=True
        )
        self.btn_zoom = self._button(
            "", "Box Zoom (Alt-drag to pan)", "zoom", tool=True
        )
        self.btn_limits = self._button("", "Set Limits", "limits", tool=True)
        self.btn_togglex = self._button(
            "", "Toggle X-Axis", "swapx", tool=True
        )
        self.btn_reset = self._button("", "Reset Zoom", "home", tool=True)
        self.btn_clear = self._button("", "Clear Graph", "trash", tool=True)
        self.btn_full = self._button("", "Fullscreen", "expand", tool=True)
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        self.btn_pan.clicked.connect(lambda: self.set_interaction("pan"))
        self.btn_zoom.clicked.connect(lambda: self.set_interaction("zoom"))
        self.btn_limits.clicked.connect(
            lambda: self.limits_frame.setVisible(
                not self.limits_frame.isVisible()
            )
        )
        self.btn_togglex.clicked.connect(self.toggle_x)
        self.btn_reset.clicked.connect(self.reset_zoom)
        self.btn_clear.clicked.connect(self.clear_requested.emit)
        for b in (
            self.btn_pan,
            self.btn_zoom,
            self.btn_limits,
            self.btn_togglex,
            self.btn_reset,
            self.btn_full,
            self.btn_clear,
        ):
            bl.addWidget(b)
        v.addWidget(bar)

        # Limits
        self.limits_frame = QFrame()
        self.limits_frame.setObjectName("limits")
        ll = QVBoxLayout(self.limits_frame)
        ll.setContentsMargins(8, 8, 8, 8)
        ll.setSpacing(6)
        self.inp = {}
        for axis in ("x", "y"):
            r = QHBoxLayout()
            r.setSpacing(8)
            lab = QLabel(axis.upper() + ":")
            lab.setFixedWidth(15)
            r.addWidget(lab)
            for i, end in enumerate(("min", "max")):
                if i:
                    to = QLabel("to")
                    to.setProperty("muted", "1")
                    r.addWidget(to)
                e = QLineEdit()
                e.setPlaceholderText(end)
                e.setValidator(QDoubleValidator())
                e.setFixedWidth(70)
                e.returnPressed.connect(self.apply_limits)
                self.inp[axis + end] = e
                r.addWidget(e)
            r.addStretch(1)
            if axis == "y":
                self.btn_apply = self._button("Apply", "Apply limits")
                self.btn_apply.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
                )
                self.btn_apply.clicked.connect(self.apply_limits)
                r.addWidget(self.btn_apply)
            ll.addLayout(r)
        self.limits_frame.setVisible(False)
        v.addWidget(self.limits_frame)

        # Plot + legend
        self.wrap = QFrame()
        self.wrap.setObjectName("plotWrap")
        wl = QVBoxLayout(self.wrap)
        wl.setContentsMargins(8, 8, 8, 8)
        wl.setSpacing(10)

        try:  # matplotlib >= 3.6
            self.figure = Figure(
                figsize=(4, 2.4), dpi=100, layout="constrained"
            )
            self.figure.get_layout_engine().set(w_pad=4 / 72, h_pad=4 / 72)
        except (TypeError, AttributeError):  # older matplotlib
            self.figure = Figure(
                figsize=(4, 2.4), dpi=100, constrained_layout=True
            )
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setMinimumHeight(200)
        self.canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.canvas.setMouseTracking(True)
        self.ax = self.figure.add_subplot(111)
        wl.addWidget(self.canvas, 1)

        self.legend = QWidget()
        self.legend.setObjectName("legend")
        self.legend_layout = QVBoxLayout(self.legend)
        self.legend_layout.setContentsMargins(0, 0, 0, 0)
        self.legend_layout.setSpacing(6)
        wl.addWidget(self.legend)
        v.addWidget(self.wrap, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("status")
        self.lbl_status.setWordWrap(True)
        v.addWidget(self.lbl_status)

        # Tooltip - Chart.js style, drawn as a floating label over the canvas.
        self.tip = QLabel(self.canvas)
        self.tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tip.setTextFormat(Qt.TextFormat.RichText)
        self.tip.setStyleSheet(
            "background: rgba(0,0,0,0.8); color: #ffffff; border-radius: 6px;"
            " padding: 6px; font-size: 11px;"
        )
        self.tip.hide()

        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect(
            "figure_leave_event", lambda e: self._hide_tip()
        )

    def _button(self, text, tip, icon_kind=None, tool=False):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setProperty("icon_kind", icon_kind)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return b

    def _set_kind(self, btn, kind):
        btn.setProperty("kind", kind)
        st = btn.style()
        st.unpolish(btn)
        st.polish(btn)
        ik = btn.property("icon_kind")
        if ik:
            col = {"primary": "#ffffff", "danger": self.t["danger"]}.get(
                kind, self.t["text"]
            )
            btn.setIcon(theme.icon(ik, col))

    def _sync_buttons(self):
        def sel(on):
            return "primary" if on else "ghost"

        self._set_kind(self.btn_mode_point, sel(self.mode == "point"))
        self._set_kind(self.btn_mode_layer, sel(self.mode == "layer"))
        self._set_kind(self.btn_shape_point, sel(self.shape == "point"))
        self._set_kind(self.btn_shape_area, sel(self.shape == "area"))
        self._set_kind(self.btn_pan, sel(self.interaction == "pan"))
        self._set_kind(self.btn_zoom, sel(self.interaction == "zoom"))
        for b in (
            self.btn_limits,
            self.btn_togglex,
            self.btn_reset,
            self.btn_full,
        ):
            self._set_kind(b, "ghost")
        self._set_kind(self.btn_clear, "danger")
        self._set_kind(self.btn_apply, "primary")

    # --------------------------------------------------------------- theme
    def check_theme(self):
        dark = theme.is_dark()
        if dark != self._dark:
            self._dark = dark
            self._apply_theme()
            self.redraw()

    def is_dark(self):
        return self._dark

    def _apply_theme(self):
        self.t = theme.tokens(self._dark)
        self._root.setStyleSheet(theme.stylesheet(self.t))
        self._sync_buttons()

    def changeEvent(self, e):
        if e.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            QTimer.singleShot(0, self.check_theme)
        super().changeEvent(e)

    # ------------------------------------------------------------- toggles
    def set_mode(self, mode):
        self.mode = mode
        self._sync_buttons()
        self.mode_changed.emit(mode)

    def set_shape(self, shape):
        self.shape = shape
        self._sync_buttons()
        self.shape_changed.emit(shape)

    def set_interaction(self, mode):
        self.interaction = mode
        self._sync_buttons()

    def set_status(self, text, level="info"):
        self.lbl_status.setProperty("level", level)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)
        self.lbl_status.setText(text)

    def curve_palette(self):
        return theme.curve_palette(self._dark)

    def toggle_fullscreen(self):
        if not self._fullscreen:
            self._fullscreen = True
            self.setFloating(True)
            self.showMaximized()
        else:
            self._fullscreen = False
            self.showNormal()
            self.setFloating(False)

    # ---------------------------------------------------------------- data
    def add_trace(self, label, color, data):
        """data: list of {'band': int, 'value': float|None, 'wv':
        float|None}."""
        self.traces.append(
            {"label": label, "color": color, "data": data, "hidden": False}
        )
        self._auto_x()
        self.redraw()

    def clear_traces(self):
        self.traces = []
        self.x_auto = True
        self.redraw()

    @staticmethod
    def _has_wavelengths(trace):
        return any(d.get("wv") for d in trace["data"])

    def _auto_x(self):
        """Wavelength when every plotted spectrum knows its wavelengths, band
        index otherwise.

        A layer with no wavelength information would have nothing to draw on a
        wavelength
        axis, so one such spectrum on the plot is enough to fall back to band
        index.
        """
        if not self.x_auto:
            return
        new = (
            "wavelength"
            if (
                self.traces
                and all(self._has_wavelengths(t) for t in self.traces)
            )
            else "band"
        )
        if new != self.x_type:
            self.x_type = new
            self.lim["xmin"] = self.lim["xmax"] = None
            self.inp["xmin"].clear()
            self.inp["xmax"].clear()

    def _xy(self, trace):
        xs, ys = [], []
        for d in trace["data"]:
            if self.x_type == "wavelength":
                if not d.get("wv"):
                    continue  # no wavelength -> not on a wavelength axis
                xs.append(d["wv"])
            else:
                xs.append(d["band"])
            v = d.get("value")
            ys.append(np.nan if v is None else float(v))
        return np.asarray(xs, float), np.asarray(ys, float)

    def set_hidden(self, indices, hidden):
        for i in indices:
            if 0 <= i < len(self.traces):
                self.traces[i]["hidden"] = bool(hidden)
        self.redraw()

    # ---------------------------------------------------------------- view
    def toggle_x(self):
        if self.x_type == "band" and not any(
            self._has_wavelengths(t) for t in self.traces
        ):
            self.set_status(
                "No wavelength information in these layers - "
                "plotting by band index."
            )
            return
        self.x_auto = False
        self.x_type = "band" if self.x_type == "wavelength" else "wavelength"
        # The units change, so typed x limits mean nothing now. y limits and
        # the hold stay.
        self.lim["xmin"] = self.lim["xmax"] = None
        self.inp["xmin"].clear()
        self.inp["xmax"].clear()
        self.redraw()

    def apply_limits(self):
        for k, e in self.inp.items():
            txt = e.text().strip().replace(",", ".")
            try:
                self.lim[k] = float(txt) if txt else None
            except ValueError:
                self.lim[k] = None
        self.held = any(v is not None for v in self.lim.values())
        self.redraw()

    def reset_zoom(self):
        self.held = False
        self.lim = dict.fromkeys(self.lim)
        for e in self.inp.values():
            e.clear()
        self.redraw()

    def _hold_current(self):
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self.lim = {"xmin": x0, "xmax": x1, "ymin": y0, "ymax": y1}
        self.held = True

    def _auto_limits(self):
        xs_all, ys_vis = [], []
        for tr in self.traces:
            xs, ys = self._xy(tr)
            xs_all.extend(xs.tolist())
            if not tr["hidden"]:
                ys_vis.extend(ys[np.isfinite(ys)].tolist())
        if xs_all:
            lo, hi = min(xs_all), max(xs_all)
            buf = (hi - lo) * 0.05 or 10
            xlim = (lo - buf, hi + buf)
        else:
            xlim = (400, 1000) if self.x_type == "wavelength" else (0, 10)
        ylim = nice_range(min(ys_vis), max(ys_vis)) if ys_vis else (0, 1)
        return xlim, ylim

    def _limits(self):
        (ax0, ax1), (ay0, ay1) = self._auto_limits()
        if not self.held:
            return (ax0, ax1), (ay0, ay1)

        def pick(k, d):
            return d if self.lim[k] is None else self.lim[k]

        return (
            (pick("xmin", ax0), pick("xmax", ax1)),
            (pick("ymin", ay0), pick("ymax", ay1)),
        )

    # ---------------------------------------------------------------- draw
    def redraw(self):
        t = self.t
        ax = self.ax
        ax.clear()
        self._lines = []
        self._hover_key = None
        self.tip.hide()
        self.figure.set_facecolor(t["sunken"])
        ax.set_facecolor(t["sunken"])
        for s in ax.spines.values():
            s.set_color(t["edge"])
        ax.tick_params(colors=t["text_mute"], labelsize=8, length=0, pad=6)
        ax.grid(True, color=t["edge"], linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_xlabel(
            "Wavelength (nm)" if self.x_type == "wavelength" else "Band Index",
            color=t["text_alt"],
            fontsize=9,
        )
        ax.set_ylabel("Pixel value", color=t["text_alt"], fontsize=9)

        (x0, x1), (y0, y1) = self._limits()

        if self.x_type == "wavelength":
            self._draw_vibgyor()

        for tr in self.traces:
            if tr["hidden"]:
                continue
            xs, ys = self._xy(tr)
            if not len(xs):
                continue
            (line,) = ax.plot(
                xs,
                ys,
                color=tr["color"],
                linewidth=1.5,
                marker="o",
                markersize=4.5,
                markerfacecolor=hex_alpha(tr["color"], 0.25),
                markeredgecolor=tr["color"],
                markeredgewidth=1,
                zorder=3,
            )
            self._lines.append((tr, line))

        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.yaxis.set_major_locator(
            MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10])
        )
        ax.xaxis.set_major_locator(
            MaxNLocator(nbins=7, steps=[1, 2, 2.5, 5, 10])
        )

        if not self.traces:
            ax.text(
                0.5,
                0.5,
                "Click on the map to plot a spectrum",
                transform=ax.transAxes,
                ha="center",
                va="center",
                color=t["text_mute"],
                fontsize=9,
            )

        (self._hover_artist,) = ax.plot(
            [],
            [],
            marker="o",
            markersize=7,
            linestyle="none",
            zorder=5,
            visible=False,
        )
        self._zoom_rect = Rectangle(
            (0, 0),
            0,
            0,
            facecolor=(11 / 255, 132 / 255, 1, 0.3),
            edgecolor=t["accent"],
            linewidth=1,
            visible=False,
            zorder=6,
        )
        ax.add_patch(self._zoom_rect)
        self.canvas.draw_idle()
        self._rebuild_legend()

    def _draw_vibgyor(self):
        stops = [
            (0.0, (148, 0, 211)),
            (0.16, (75, 0, 130)),
            (0.33, (0, 0, 255)),
            (0.5, (0, 255, 0)),
            (0.66, (255, 255, 0)),
            (0.83, (255, 127, 0)),
            (1.0, (255, 0, 0)),
        ]
        grad = np.zeros((1, 256, 4))
        for i in range(256):
            f = i / 255.0
            for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
                if p0 <= f <= p1:
                    k = (f - p0) / (p1 - p0)
                    grad[0, i, :3] = [
                        (c0[j] + (c1[j] - c0[j]) * k) / 255.0 for j in range(3)
                    ]
                    break
            grad[0, i, 3] = 0.15
        self.ax.imshow(
            grad,
            extent=(400, 700, 0, 1),
            aspect="auto",
            interpolation="bilinear",
            transform=self.ax.get_xaxis_transform(),
            zorder=0,
        )

    # -------------------------------------------------------------- legend
    def _rebuild_legend(self):
        lay = self.legend_layout
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                # Detach now - a widget only waiting on deleteLater is still
                # painted.
                w.hide()
                w.setParent(None)
                w.deleteLater()
        groups, order = {}, []
        for i, tr in enumerate(self.traces):
            name, suffix = tr["label"], ""
            b = name.rfind(" [")
            if b != -1 and name.endswith("]"):
                suffix = name[b + 2:len(name) - 1]
                name = name[:b]
            if name not in groups:
                groups[name] = []
                order.append(name)
            groups[name].append((i, tr, suffix))

        t = self.t
        for name in order:
            items = groups[name]
            all_hidden = all(tr["hidden"] for _, tr, _ in items)
            g = QWidget()
            gl = QVBoxLayout(g)
            gl.setContentsMargins(0, 0, 0, 0)
            gl.setSpacing(2)

            title = Clickable()
            tl = QHBoxLayout(title)
            tl.setContentsMargins(0, 0, 0, 0)
            tlab = QLabel(name)
            f = tlab.font()
            f.setBold(True)
            f.setPixelSize(11)
            f.setStrikeOut(all_hidden)
            tlab.setFont(f)
            tlab.setStyleSheet(
                f"color: {t['text_mute'] if all_hidden else t['text']};"
            )
            tlab.setWordWrap(True)
            tl.addWidget(tlab)
            title.setCursor(Qt.CursorShape.PointingHandCursor)
            title.setToolTip(
                ("Show" if all_hidden else "Hide")
                + " every curve from this layer"
            )
            idx = [i for i, _, _ in items]
            title.clicked.connect(
                lambda _m, idx=idx, h=all_hidden: self.set_hidden(idx, not h)
            )
            gl.addWidget(title)

            row = QWidget()
            fl = FlowLayout(row)
            for i, tr, suffix in items:
                it = Clickable()
                il = QHBoxLayout(it)
                il.setContentsMargins(0, 0, 0, 0)
                il.setSpacing(4)
                sw = QFrame()
                sw.setFixedSize(8, 8)
                fill = "transparent" if tr["hidden"] else tr["color"]
                sw.setStyleSheet(
                    f"border: 1px solid {tr['color']}; border-radius: 2px;"
                    f" background: {fill};"
                )
                il.addWidget(sw)
                if suffix:
                    lab = QLabel(suffix)
                    lf = lab.font()
                    lf.setPixelSize(10)
                    lf.setStrikeOut(tr["hidden"])
                    lab.setFont(lf)
                    lab.setStyleSheet(f"color: {t['text_mute']};")
                    il.addWidget(lab)
                if tr["hidden"]:
                    eff = QGraphicsOpacityEffect(it)
                    eff.setOpacity(0.45)
                    it.setGraphicsEffect(eff)
                it.setCursor(Qt.CursorShape.PointingHandCursor)
                it.setToolTip(
                    ("Show " if tr["hidden"] else "Hide ")
                    + (suffix or "this curve")
                    + ". Shift-click to show only this one."
                )
                it.clicked.connect(
                    lambda m, i=i: self._legend_item_clicked(i, m)
                )
                fl.addWidget(it)
            gl.addWidget(row)
            lay.addWidget(g)

    def _legend_item_clicked(self, i, mods):
        if mods & Qt.KeyboardModifier.ShiftModifier:
            for k, tr in enumerate(self.traces):
                tr["hidden"] = k != i
            self.redraw()
        else:
            self.set_hidden([i], not self.traces[i]["hidden"])

    # -------------------------------------------------------- interactions
    def _dpr(self):
        try:
            return self.canvas.device_pixel_ratio
        except AttributeError:
            return self.canvas.devicePixelRatioF()

    def _effective_drag_mode(self):
        alt = bool(
            QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier
        )
        if alt:
            return "zoom" if self.interaction == "pan" else "pan"
        return self.interaction

    def _on_press(self, e):
        if e.button != 1 or e.inaxes != self.ax or not self.traces:
            return
        self._hide_tip()
        mode = self._effective_drag_mode()
        self._drag = {
            "mode": mode,
            "px": (e.x, e.y),
            "data": (e.xdata, e.ydata),
            "xlim": self.ax.get_xlim(),
            "ylim": self.ax.get_ylim(),
            "inv": self.ax.transData.inverted().frozen(),
            "moved": False,
        }
        if mode == "pan":
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _on_motion(self, e):
        d = self._drag
        if d:
            if abs(e.x - d["px"][0]) + abs(e.y - d["px"][1]) > 3:
                d["moved"] = True
            if d["mode"] == "pan":
                x0, y0 = d["inv"].transform(d["px"])
                x1, y1 = d["inv"].transform((e.x, e.y))
                dx, dy = x0 - x1, y0 - y1
                self.ax.set_xlim(d["xlim"][0] + dx, d["xlim"][1] + dx)
                self.ax.set_ylim(d["ylim"][0] + dy, d["ylim"][1] + dy)
            else:
                x1, y1 = d["inv"].transform((e.x, e.y))
                x0, y0 = d["data"]
                self._zoom_rect.set_bounds(
                    min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
                )
                self._zoom_rect.set_visible(True)
            self.canvas.draw_idle()
            return
        self._hover(e)

    def _on_release(self, e):
        d = self._drag
        self._drag = None
        self.canvas.unsetCursor()
        if not d:
            return
        if d["mode"] == "pan":
            if d["moved"]:
                self._hold_current()
        else:
            self._zoom_rect.set_visible(False)
            if (
                d["moved"]
                and abs(e.x - d["px"][0]) > 5
                and abs(e.y - d["px"][1]) > 5
            ):
                x1, y1 = d["inv"].transform((e.x, e.y))
                x0, y0 = d["data"]
                self.ax.set_xlim(min(x0, x1), max(x0, x1))
                self.ax.set_ylim(min(y0, y1), max(y0, y1))
                self._hold_current()
            self.canvas.draw_idle()

    def _on_scroll(self, e):
        if e.inaxes != self.ax or not self.traces:
            return
        factor = (1 - WHEEL_STEP) if e.button == "up" else 1 / (1 - WHEEL_STEP)
        cx, cy = e.xdata, e.ydata
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self.ax.set_xlim(cx - (cx - x0) * factor, cx + (x1 - cx) * factor)
        self.ax.set_ylim(cy - (cy - y0) * factor, cy + (y1 - cy) * factor)
        self._hold_current()
        self._hide_tip()
        self.canvas.draw_idle()

    def _hide_tip(self):
        self.tip.hide()
        if self._hover_key is not None:
            self._hover_key = None
            self._hover_artist.set_visible(False)
            self.canvas.draw_idle()

    def _hover(self, e):
        if e.inaxes != self.ax or not self._lines:
            self._hide_tip()
            return
        best = None
        for n, (tr, line) in enumerate(self._lines):
            xs, ys = line.get_data()
            xs = np.asarray(xs, float)
            ys = np.asarray(ys, float)
            ok = np.isfinite(ys)
            if not ok.any():
                continue
            pts = self.ax.transData.transform(
                np.column_stack([xs, np.where(ok, ys, 0)])
            )
            dist = np.hypot(pts[:, 0] - e.x, pts[:, 1] - e.y)
            dist[~ok] = np.inf
            i = int(np.argmin(dist))
            if best is None or dist[i] < best[0]:
                best = (dist[i], n, i, pts[i])
        if best is None or best[0] > HIT_RADIUS_PX * self._dpr():
            self._hide_tip()
            return
        _, n, i, (px, py) = best
        tr, line = self._lines[n]
        xs, ys = line.get_data()
        key = (n, i)
        if key != self._hover_key:
            self._hover_key = key
            self._hover_artist.set_data([xs[i]], [ys[i]])
            self._hover_artist.set_color(tr["color"])
            self._hover_artist.set_markerfacecolor(tr["color"])
            self._hover_artist.set_visible(True)
            self.canvas.draw_idle()
            self.tip.setText(
                f"<b>{fmt_x(float(xs[i]))}</b><br>"
                f"<span style='color:{tr['color']}; font-size:13px;'>"
                "&#9632;</span>"
                f"&nbsp;{fmt_value(float(ys[i]))}"
            )
            self.tip.adjustSize()
        dpr = self._dpr()
        qx = px / dpr
        qy = self.canvas.height() - py / dpr
        tw, th = self.tip.width(), self.tip.height()
        x = qx + 10 if qx + 10 + tw < self.canvas.width() else qx - 10 - tw
        y = min(max(qy - th / 2, 0), self.canvas.height() - th)
        self.tip.move(int(x), int(y))
        self.tip.show()
        self.tip.raise_()

    # --------------------------------------------------------------- close
    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()
