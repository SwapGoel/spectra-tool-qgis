import math
import os.path
import re

import numpy as np
from qgis.PyQt.QtCore import QCoreApplication, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon

try:  # Qt6 / QGIS 4: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3.x
    from qgis.PyQt.QtWidgets import QAction

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRaster,
    QgsRasterLayer,
    QgsRectangle,
    QgsWkbTypes,
)
from .spectra_theme import log
from qgis.gui import (
    QgsMapTool,
    QgsMapToolEmitPoint,
    QgsRubberBand,
    QgsVertexMarker,
)

# ---------------------------------------------------------------------------
# Enums QGIS moved between versions. Newest spelling first, oldest as the
# fallback, so the same file runs on QGIS 3.16 ... 3.x LTR ... QGIS 4 (Qt6).
# ---------------------------------------------------------------------------
try:
    from qgis.core import Qgis
except ImportError:  # pragma: no cover
    Qgis = None


def _enum(*paths):
    for owner, names in paths:
        obj = owner
        try:
            for n in names.split("."):
                obj = getattr(obj, n)
            return obj
        except AttributeError:
            continue
    raise AttributeError(paths)


POLYGON = _enum(
    (Qgis, "GeometryType.Polygon"), (QgsWkbTypes, "PolygonGeometry")
)
ICON_CIRCLE = _enum(
    (QgsVertexMarker, "IconType.ICON_CIRCLE"), (QgsVertexMarker, "ICON_CIRCLE")
)
# The engine's area read is capped; bigger boxes are averaged on a subsampled
# grid (the "~" in the label), the same honesty Blink's region_spectrum gives.
AREA_MAX_SIDE = 512


def _identify_format():
    return _enum(
        (Qgis, "RasterIdentifyFormat.Value"),
        (QgsRaster, "IdentifyFormatValue"),
    )


def _matplotlib_missing():
    try:
        import matplotlib  # noqa: F401

        return None
    except ImportError as e:
        return str(e)


class SpectraRectTool(QgsMapTool):
    """Drag a rectangle on the map; emits it in canvas CRS."""

    rectangleDrawn = pyqtSignal(QgsRectangle)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.start = None
        self.rb = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _band(self):
        if self.rb is None:
            self.rb = QgsRubberBand(self.canvas, POLYGON)
            self.rb.setStrokeColor(QColor("#0b84ff"))
            self.rb.setFillColor(QColor(11, 132, 255, 40))
            self.rb.setWidth(1)
            self.rb.setLineStyle(Qt.PenStyle.DashLine)
        return self.rb

    def canvasPressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.start = e.mapPoint()
        self._band().reset(POLYGON)

    def canvasMoveEvent(self, e):
        if self.start is None:
            return
        rect = QgsRectangle(self.start, e.mapPoint())
        self._band().setToGeometry(
            QgsGeometry.fromRect(rect),
            self.canvas.mapSettings().destinationCrs(),
        )

    def canvasReleaseEvent(self, e):
        if self.start is None:
            return
        rect = QgsRectangle(self.start, e.mapPoint())
        self.start = None
        self._band().reset(POLYGON)
        if rect.width() > 0 and rect.height() > 0:
            self.rectangleDrawn.emit(rect)

    def deactivate(self):
        self.start = None
        if self.rb is not None:
            self.rb.reset(POLYGON)
        super().deactivate()


class SpectralTool:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.toolbar = None
        self.dockwidget = None
        self.point_tool = None
        self.area_tool = None
        self.markers = []  # canvas items to remove on Clear
        self.color_idx = 0
        self._wv_cache = {}

    def tr(self, message):
        return QCoreApplication.translate("SpectralTool", message)

    # ------------------------------------------------------------ plugin glue
    def initGui(self):
        # One action, used in two places: its own toolbar and directly in the
        # Plugins menu. (addPluginToMenu would nest it in a submenu of the same
        # name - the old "Spectra Plotter > Spectra Plotter" double click.)
        self.action = QAction(
            QIcon(os.path.join(self.plugin_dir, "icon.png")),
            self.tr("Spectral Tool"),
            self.iface.mainWindow(),
        )
        self.action.setObjectName("SpectralToolAction")
        self.action.setToolTip(
            self.tr("Spectral Tool - plot pixel spectra from the map")
        )
        self.action.setCheckable(True)
        self.action.triggered.connect(self.run)

        # The Plugins toolbar: where QGIS users expect a plugin's button, shown
        # by default.
        self.iface.addToolBarIcon(self.action)
        self.iface.pluginMenu().addAction(self.action)
        self._ensure_toolbar_visible_once()

    def _ensure_toolbar_visible_once(self):
        """On first install, switch the Plugins toolbar on so the button is
        actually seen.

        Done once per profile: if the user later hides the toolbar themselves,
        that choice
        is respected on every following start.
        """
        from qgis.core import QgsSettings

        settings = QgsSettings()
        key = "SpectralTool/toolbarShown"
        if settings.value(key, False, type=bool):
            return
        try:
            tb = self.iface.pluginToolBar()
            if tb is not None:
                tb.setVisible(True)
            settings.setValue(key, True)
        except Exception as e:
            log(f"could not show the Plugins toolbar: {e}")

    def unload(self):
        self.reset_all()
        if self.action:
            self.iface.pluginMenu().removeAction(self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.dockwidget:
            self.dockwidget.close()
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.deleteLater()
            self.dockwidget = None
        for tool in (self.point_tool, self.area_tool):
            if tool and self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)

    def run(self, *_):
        # The button toggles the panel. If the panel is open but another map
        # tool (pan, identify...) took over, the first click just hands the map
        # back to us.
        if self.dockwidget and self.dockwidget.isVisible():
            if self.canvas.mapTool() in (self.point_tool, self.area_tool):
                self.dockwidget.close()
                return
            self._sync_action()
            self.activate_tool()
            return

        if not self.dockwidget:
            missing = _matplotlib_missing()
            if missing:
                self.action.setChecked(False)
                self.iface.messageBar().pushCritical(
                    "Spectral Tool",
                    "needs the matplotlib Python package, which this QGIS "
                    "installation does not have ("
                    + missing
                    + "). Install it with your QGIS "
                    "Python (OSGeo4W Shell on Windows: "
                    '"python -m pip install matplotlib").',
                )
                return
            from .spectra_dockwidget import SpectraDockWidget

            self.dockwidget = SpectraDockWidget(self.iface.mainWindow())
            self.dockwidget.closingPlugin.connect(self.on_close_dock)
            self.dockwidget.clear_requested.connect(self.reset_all)
            self.dockwidget.mode_changed.connect(lambda _m: self.reset_all())
            self.dockwidget.shape_changed.connect(
                lambda _s: self.activate_tool()
            )
            self.dockwidget.visibilityChanged.connect(
                lambda _v: self._sync_action()
            )
            self.iface.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea, self.dockwidget
            )

        if not self.point_tool:
            self.point_tool = QgsMapToolEmitPoint(self.canvas)
            self.point_tool.canvasClicked.connect(self.handle_click)
            self.area_tool = SpectraRectTool(self.canvas)
            self.area_tool.rectangleDrawn.connect(self.handle_area)

        self.dockwidget.show()
        self.dockwidget.raise_()
        self._sync_action()
        self.activate_tool()

    def _sync_action(self):
        if self.action:
            self.action.setChecked(
                bool(self.dockwidget and self.dockwidget.isVisible())
            )

    def activate_tool(self):
        if not self.dockwidget:
            return
        tool = (
            self.area_tool
            if self.dockwidget.shape == "area"
            else self.point_tool
        )
        self.canvas.setMapTool(tool)

    def on_close_dock(self):
        self.reset_all()
        for tool in (self.point_tool, self.area_tool):
            if tool and self.canvas.mapTool() is tool:
                self.canvas.unsetMapTool(tool)

    # ---------------------------------------------------------------- layers
    def _visible_rasters(self):
        root = QgsProject.instance().layerTreeRoot()
        out = []
        for node in root.findLayers():
            lyr = node.layer()
            if isinstance(lyr, QgsRasterLayer) and node.isVisible():
                out.append(lyr)
        return out

    def _layers_to_read(self):
        visible = self._visible_rasters()
        if self.dockwidget.mode == "layer":
            return visible
        active = self.iface.activeLayer()
        if isinstance(active, QgsRasterLayer):
            return [active]
        return visible[:1]

    def _next_color(self):
        pal = self.dockwidget.curve_palette()
        c = pal[self.color_idx % len(pal)]
        self.color_idx += 1
        return c

    def _to_layer(self, layer):
        return QgsCoordinateTransform(
            self.canvas.mapSettings().destinationCrs(),
            layer.crs(),
            QgsProject.instance(),
        )

    # ----------------------------------------------------------- wavelengths
    def get_wavelengths(self, layer):
        """Per-band wavelength in nm (None where unknown), or None if the layer
        has none.

        Works for any raster: it looks for wavelengths in the places different
        formats
        keep them, and if none of them say anything the plot uses band index
        instead.
          1. dataset-level lists (ENVI 'wavelength = {...}', NetCDF/HDF
             attributes...)
          2. per-band metadata in any domain ('wavelength',
             'central_wavelength_um',
             GDAL's IMAGERY domain, Sentinel-2 'WAVELENGTH' +
             'WAVELENGTH_UNIT'...)
          3. band names / descriptions ('665 nm', 'B4 (0.665 um)',
             'wavelength=665',
             or every band named by a bare number like '442.5')
        """
        key = (layer.id(), layer.source())
        if key in self._wv_cache:
            return self._wv_cache[key]
        n = layer.bandCount()
        wv = [None] * n
        if layer.providerType() == "gdal":
            try:
                self._wavelengths_from_gdal(layer, n, wv)
            except Exception as e:
                log(f"no GDAL wavelength metadata for {layer.name()}: {e}")
        self._wavelengths_from_names(layer, n, wv)

        wv = [
            v if (v is not None and math.isfinite(v) and v > 0) else None
            for v in wv
        ]
        found = sum(v is not None for v in wv)
        result = wv if (found >= 2 or (n == 1 and found == 1)) else None
        self._wv_cache[key] = result
        return result

    _NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
    _UNIT = r"(nm|nanomet(?:er|re)s?|um|µm|μm|microns?|micromet(?:er|re)s?)"

    @staticmethod
    def _is_wv_key(k):
        k = k.lower().split("#")[-1]
        return (
            "wavelength" in k or k in ("lambda", "center_wl", "central_wl")
        ) and not any(
            bad in k
            for bad in ("unit", "fwhm", "width", "min", "max", "range")
        )

    @staticmethod
    def _units_in(md, key=""):
        """Units from a companion key (wavelength_units, WAVELENGTH_UNIT...) or
        the key itself."""
        k = key.lower()
        if k.endswith("_um") or "micro" in k:
            return "um"
        if k.endswith("_nm") or "nano" in k:
            return "nm"
        for mk, mv in md.items():
            if "wavelength" in mk.lower() and "unit" in mk.lower():
                return str(mv).lower()
        return ""

    def _wavelengths_from_gdal(self, layer, n, wv):
        import warnings
        from osgeo import gdal

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = gdal.Open(layer.source().split("|")[0])
        if ds is None:
            return
        # 1. A list of n numbers at dataset level, in any metadata domain.
        for dom in (ds.GetMetadataDomainList() or []) + [""]:
            md = ds.GetMetadata(dom) or {}
            if not isinstance(md, dict):
                continue
            for k, v in md.items():
                if not self._is_wv_key(k):
                    continue
                vals = [float(x) for x in re.findall(self._NUM, str(v))]
                if len(vals) >= n > 1 or (n == 1 and len(vals) == 1):
                    units = self._units_in(md, k)
                    for i in range(n):
                        wv[i] = self._nm(vals[i], units)
                    return
        # 2. Per-band metadata, any domain.
        for i in range(n):
            b = ds.GetRasterBand(i + 1)
            doms = (b.GetMetadataDomainList() or []) + [""]
            for dom in doms:
                md = b.GetMetadata(dom) or {}
                if not isinstance(md, dict):
                    continue
                for k, v in md.items():
                    if not self._is_wv_key(k):
                        continue
                    m = re.search(
                        self._NUM + r"\s*" + self._UNIT + "?", str(v), re.I
                    )
                    if m:
                        num = float(re.match(self._NUM, m.group(0)).group(0))
                        wv[i] = self._nm(
                            num, (m.group(1) or self._units_in(md, k)).lower()
                        )
                        break
                if wv[i] is not None:
                    break

    def _wavelengths_from_names(self, layer, n, wv):
        names = [(layer.bandName(i + 1) or "") for i in range(n)]
        # GDAL descriptions arrive as "Band 01: <description>"; keep the
        # description.
        descs = [
            (
                nm.split(":", 1)[1].strip()
                if re.match(r"^band\s*\d+\s*:", nm, re.I)
                else nm
            )
            for nm in names
        ]
        for i, d in enumerate(descs):
            if wv[i] is not None:
                continue
            m = re.search(
                r"(" + self._NUM + r")\s*" + self._UNIT + r"(?![a-z])", d, re.I
            )
            if not m:
                m2 = re.search(
                    r"(?:wavelength|lambda|wl)\s*[=:_ ]\s*(" + self._NUM + ")",
                    d,
                    re.I,
                )
                if m2:
                    wv[i] = self._nm(float(m2.group(1)))
                continue
            wv[i] = self._nm(float(m.group(1)), m.group(2).lower())
        # Every band named by a bare number (e.g. "442.5", "Band 3: 0.665") is
        # a wavelength list in disguise - but only when ALL of them are, so
        # "1", "2", "3" band ids that happen to be numeric are not mistaken for
        # one unless they look like a spectrum.
        if all(v is None for v in wv) and n > 1:
            bare = [re.fullmatch(self._NUM, d.strip()) for d in descs]
            if all(bare):
                vals = [float(b.group(0)) for b in bare]
                looks_nm = all(250 <= v <= 20000 for v in vals)
                looks_um = all(0.25 <= v <= 20 for v in vals) and any(
                    v != int(v) for v in vals
                )
                if looks_nm or looks_um:
                    for i, v in enumerate(vals):
                        wv[i] = self._nm(v, "um" if looks_um else "nm")

    @staticmethod
    def _nm(value, units=""):
        u = (units or "").lower()
        if "nano" in u or u == "nm":
            return value
        if "micro" in u or u in ("um", "µm", "μm", "micron", "microns"):
            return value * 1000.0
        return (
            value * 1000.0 if value < 30 else value
        )  # no units: guess from magnitude

    # ------------------------------------------------------------ point read
    def handle_click(self, point, button=Qt.MouseButton.LeftButton):
        if not self.dockwidget or button != Qt.MouseButton.LeftButton:
            return
        layers = self._layers_to_read()
        if not layers:
            self.dockwidget.set_status(
                "Select or show a raster layer first.", "error"
            )
            return
        layer_mode = self.dockwidget.mode == "layer"
        if layer_mode:
            self.reset_all()
        point_color = self._next_color()
        pal = self.dockwidget.curve_palette()
        plotted = 0

        for i, layer in enumerate(layers):
            read = self._read_pixel(layer, point)
            if read is None:
                self.dockwidget.set_status(
                    f"⚠ no data here in {layer.name()} - outside the "
                    "scene, so no spectrum is plotted",
                    "error",
                )
                if not layer_mode and self.color_idx > 0:
                    self.color_idx -= (
                        1  # the colour goes to the next point instead
                    )
                continue
            values, row, col, pixel_rect = read
            color = (
                pal[(self.color_idx - 1 + i) % len(pal)]
                if layer_mode
                else point_color
            )

            px = QgsRubberBand(self.canvas, POLYGON)
            c = QColor(color)
            px.setStrokeColor(c)
            px.setFillColor(QColor(c.red(), c.green(), c.blue(), 90))
            px.setWidth(2)
            px.setToGeometry(QgsGeometry.fromRect(pixel_rect), layer.crs())
            self.markers.append(px)

            dot = QgsVertexMarker(self.canvas)
            dot.setIconType(ICON_CIRCLE)
            dot.setColor(QColor("#000000"))
            dot.setFillColor(c)
            dot.setIconSize(7)
            dot.setPenWidth(1)
            dot.setCenter(point)
            self.markers.append(dot)

            wv = self.get_wavelengths(layer)
            data = [
                {
                    "band": b + 1,
                    "value": values[b],
                    "wv": wv[b] if wv else None,
                }
                for b in range(len(values))
            ]
            self.dockwidget.add_trace(
                f"{layer.name()} [R:{row}, C:{col}]", color, data
            )
            plotted += 1

        if plotted:
            self.dockwidget.set_status("")
            if layer_mode:
                self.color_idx = max(self.color_idx, plotted)

    def _read_pixel(self, layer, canvas_point):
        try:
            p = self._to_layer(layer).transform(canvas_point)
        except Exception:
            return None
        ext = layer.extent()
        xres, yres = layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY()
        if xres <= 0 or yres <= 0:
            return None
        col = int(math.floor((p.x() - ext.xMinimum()) / xres))
        row = int(math.floor((ext.yMaximum() - p.y()) / yres))
        if col < 0 or col >= layer.width() or row < 0 or row >= layer.height():
            return None
        x0 = ext.xMinimum() + col * xres
        y1 = ext.yMaximum() - row * yres
        rect = QgsRectangle(x0, y1 - yres, x0 + xres, y1)

        res = layer.dataProvider().identify(rect.center(), _identify_format())
        if not res.isValid():
            return None
        raw = res.results()
        values = []
        for b in range(1, layer.bandCount() + 1):
            v = raw.get(b)
            try:
                v = float(v) if v is not None else None
            except (TypeError, ValueError):
                v = None
            if v is not None and not math.isfinite(v):
                v = None
            values.append(v)
        if all(v is None for v in values):
            return None
        return values, row, col, rect

    # ------------------------------------------------------------- area read
    def handle_area(self, canvas_rect):
        if not self.dockwidget:
            return
        layers = self._layers_to_read()
        if not layers:
            self.dockwidget.set_status(
                "Select or show a raster layer first.", "error"
            )
            return
        layer_mode = self.dockwidget.mode == "layer"
        if layer_mode:
            self.reset_all()
        point_color = self._next_color()
        pal = self.dockwidget.curve_palette()

        box = QgsRubberBand(self.canvas, POLYGON)
        c = QColor(point_color)
        box.setStrokeColor(c)
        box.setFillColor(QColor(c.red(), c.green(), c.blue(), 38))
        box.setWidth(2)
        box.setToGeometry(
            QgsGeometry.fromRect(canvas_rect),
            self.canvas.mapSettings().destinationCrs(),
        )
        self.markers.append(box)

        n = len(layers)
        self.dockwidget.set_status(
            "⏳ Averaging "
            + (f"{n} layers over the box..." if n > 1 else "the box...")
        )
        QCoreApplication.processEvents()

        for i, layer in enumerate(layers):
            try:
                out = self._read_area(layer, canvas_rect)
            except Exception as e:
                self.dockwidget.set_status(
                    f"❌ Region spectrum failed: {e}", "error"
                )
                continue
            if out is None:
                self.dockwidget.set_status(
                    f"⚠ the box does not cover {layer.name()}", "error"
                )
                continue
            means, w, h, counted, sampled = out
            color = (
                pal[(self.color_idx - 1 + i) % len(pal)]
                if layer_mode
                else point_color
            )
            wv = self.get_wavelengths(layer)
            data = [
                {"band": b + 1, "value": means[b], "wv": wv[b] if wv else None}
                for b in range(len(means))
            ]
            label = f"{layer.name()} [AVG {w}x{h}{' ~' if sampled else ''}]"
            self.dockwidget.add_trace(label, color, data)
            self.dockwidget.set_status(
                f"✅ {counted:,} pixels averaged "
                f"({'sampled' if sampled else 'exact'})"
            )
        if layer_mode:
            self.color_idx = max(self.color_idx, n)

    def _read_area(self, layer, canvas_rect):
        r = self._to_layer(layer).transformBoundingBox(canvas_rect)
        ext = layer.extent()
        r = r.intersect(ext)
        if r.isEmpty():
            return None
        xres, yres = layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY()
        # Snap to whole pixels so the block lines up with the raster grid.
        c0 = max(0, int(math.floor((r.xMinimum() - ext.xMinimum()) / xres)))
        c1 = min(
            layer.width(),
            int(math.ceil((r.xMaximum() - ext.xMinimum()) / xres)),
        )
        r0 = max(0, int(math.floor((ext.yMaximum() - r.yMaximum()) / yres)))
        r1 = min(
            layer.height(),
            int(math.ceil((ext.yMaximum() - r.yMinimum()) / yres)),
        )
        w, h = max(1, c1 - c0), max(1, r1 - r0)
        snapped = QgsRectangle(
            ext.xMinimum() + c0 * xres,
            ext.yMaximum() - (r0 + h) * yres,
            ext.xMinimum() + (c0 + w) * xres,
            ext.yMaximum() - r0 * yres,
        )
        scale = max(w, h) / float(AREA_MAX_SIDE)
        sampled = scale > 1
        bw = max(1, int(round(w / scale))) if sampled else w
        bh = max(1, int(round(h / scale))) if sampled else h

        prov = layer.dataProvider()
        means, counted = [], 0
        for b in range(1, layer.bandCount() + 1):
            block = prov.block(b, snapped, bw, bh)
            arr = self._block_array(block, bw, bh)
            ok = np.isfinite(arr)
            cnt = int(ok.sum())
            counted = max(counted, cnt)
            means.append(float(arr[ok].mean()) if cnt else None)
        if all(m is None for m in means):
            return None
        return means, w, h, counted, sampled

    @staticmethod
    def _block_array(block, bw, bh):
        if hasattr(block, "as_numpy"):
            a = block.as_numpy(use_masking=True)
            if np.ma.isMaskedArray(a):
                return a.astype(float).filled(np.nan)
            return np.asarray(a, float)
        out = np.full((bh, bw), np.nan)
        for y in range(bh):
            for x in range(bw):
                if not block.isNoData(y, x):
                    out[y, x] = block.value(y, x)
        return out

    # ------------------------------------------------------------------ reset
    def reset_all(self):
        scene = self.canvas.scene()
        for m in self.markers:
            if m.scene() is scene:
                scene.removeItem(m)
        self.markers = []
        self.color_idx = 0
        if self.dockwidget:
            self.dockwidget.clear_traces()
            self.dockwidget.set_status("")


# Backwards-compatible name.
SpectraPlotter = SpectralTool
