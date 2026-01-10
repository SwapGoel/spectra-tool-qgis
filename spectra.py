# -*- coding: utf-8 -*-
import re
import os.path
import random

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsPointXY, QgsGeometry, QgsRectangle, QgsWkbTypes, QgsRaster
from qgis.gui import QgsRubberBand

from .spectra_tool import SpectraPointTool
from .spectra_dockwidget import SpectraDockWidget
from .resources import *


class SpectraPlotter:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """Constructor."""
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        self.active_markers = [] 
        
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'SpectraPlotter_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr(u'&Spectra')
        self.toolbar = self.iface.addToolBar(u'SpectraPlotter')
        self.toolbar.setObjectName(u'SpectraPlotter')

        self.pluginIsActive = False
        self.dockwidget = None
        
        canvas = self.iface.mapCanvas()
        self.tool = SpectraPointTool(canvas)
        self.tool.canvas_clicked.connect(self.handle_click)


    def tr(self, message):
        return QCoreApplication.translate('SpectraPlotter', message)


    def add_action(self, icon_path, text, callback, enabled_flag=True, add_to_menu=True, add_to_toolbar=True, status_tip=None, whats_this=None, parent=None):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action


    def initGui(self):
        icon_path = ':/plugins/spectra/icon.png'
        self.add_action(
            icon_path,
            text=self.tr(u'Spectra Tool'),
            callback=self.run,
            parent=self.iface.mainWindow())


    def onClosePlugin(self):
        self.dockwidget.closingPlugin.disconnect(self.onClosePlugin)
        self.pluginIsActive = False
        self.reset_all() # Cleanup markers on close


    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&Spectra'), action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar


    def get_wavelengths(self, layer):
        count = layer.bandCount()
        wls = []
        found_any = False
        for i in range(1, count + 1):
            desc = layer.bandName(i) 
            val = 0.0 
            if desc:
                specific_match = re.search(r"WV_(\d+\.?\d*)", desc)
                if specific_match:
                    val = float(specific_match.group(1))
                    found_any = True
            wls.append(val)
        if not found_any:
            return None
        return wls

    def get_next_color(self):
        """Generates a random vibrant color."""
        r = random.randint(0, 220)
        g = random.randint(0, 220)
        b = random.randint(0, 220)
        return '#{:02x}{:02x}{:02x}'.format(r, g, b)

    def reset_all(self):
        """Cleans up map markers and resets internal lists."""
        # 1. Clear Map Canvas
        for m_set in self.active_markers:
            try:
                self.iface.mapCanvas().scene().removeItem(m_set['box'])
                self.iface.mapCanvas().scene().removeItem(m_set['cross'])
            except:
                pass
        self.active_markers = []
        
        # 2. Clear Graph (if dockwidget is open)
        if self.dockwidget:
            self.dockwidget.clear_all_traces()

    def handle_click(self, point):
        layer = self.iface.activeLayer()
        if not layer or layer.type() != layer.RasterLayer:
            return
            
        # Check Multi-Select Mode
        if self.dockwidget and self.dockwidget.chk_multi.isChecked():
            is_multi = True
        else:
            is_multi = False
            
        if not is_multi:
            # Reuse our new reset function for single-point mode
            self.reset_all()

        # Calculate Geometry
        extent = layer.extent()
        x_res = layer.rasterUnitsPerPixelX()
        y_res = layer.rasterUnitsPerPixelY()
        col = int((point.x() - extent.xMinimum()) / x_res)
        row = int((extent.yMaximum() - point.y()) / y_res)

        if col < 0 or col >= layer.width() or row < 0 or row >= layer.height():
            print("Clicked outside")
            return

        pixel_x_min = extent.xMinimum() + (col * x_res)
        pixel_y_max = extent.yMaximum() - (row * y_res)
        true_rect = QgsRectangle(pixel_x_min, pixel_y_max - y_res, pixel_x_min + x_res, pixel_y_max)

        # Generate Color
        hex_color = self.get_next_color()
        q_color = QColor(hex_color)

        # 1. BOX MARKER
        box = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PolygonGeometry)
        box.setColor(QColor(q_color.red(), q_color.green(), q_color.blue(), 40)) 
        box.setStrokeColor(q_color) 
        box.setWidth(2) 
        box.setToGeometry(QgsGeometry.fromRect(true_rect), layer.crs())
        
        # 2. CROSSHAIR MARKER
        cross = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PointGeometry)
        cross.setIcon(QgsRubberBand.ICON_CROSS)
        cross.setColor(q_color)
        cross.setWidth(2) # 2 pixel width (tiny!)
        cross.setToGeometry(QgsGeometry.fromPointXY(true_rect.center()), layer.crs())
        
        self.active_markers.append({'box': box, 'cross': cross})

        # Extract Data
        provider = layer.dataProvider()
        results = provider.identify(point, QgsRaster.IdentifyFormatValue)
        
        if results.isValid():
            values = results.results()
            y_values = []
            for band_num in range(1, layer.bandCount() + 1):
                val = values.get(band_num, 0)
                y_values.append(val)

            x_values = self.get_wavelengths(layer)

            if self.dockwidget:
                label = f"Pt {len(self.active_markers)}"
                self.dockwidget.add_trace(x_values, y_values, hex_color, label, layer.name())
                
            self.iface.messageBar().pushMessage("Spectra", f"Added Point {len(self.active_markers)}", level=0)

    def run(self):
        if self.dockwidget is None:
            self.dockwidget = SpectraDockWidget()
            self.dockwidget.closingPlugin.connect(self.onClosePlugin)
            # NEW: Connect the reset signal
            self.dockwidget.resetTriggered.connect(self.reset_all)
        
        if not self.dockwidget.isVisible():
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dockwidget)
            self.dockwidget.show()
        
        self.iface.mapCanvas().setMapTool(self.tool)
        self.pluginIsActive = True