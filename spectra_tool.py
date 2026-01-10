from qgis.gui import QgsMapToolEmitPoint
from qgis.core import QgsPointXY
from PyQt5.QtCore import pyqtSignal

class SpectraPointTool(QgsMapToolEmitPoint):
    # This acts like a radio transmitter. 
    # It will broadcast a "signal" containing coordinates whenever we click.
    canvas_clicked = pyqtSignal(QgsPointXY)

    def __init__(self, canvas):
        # We must initialize the parent class with the canvas
        self.canvas = canvas
        super().__init__(self.canvas)

    def canvasReleaseEvent(self, event):
        # This function triggers every time the user LETS GO of the mouse button.
        
        # 1. Get the click coordinates from the event
        point = self.toMapCoordinates(event.pos())
        
        # 2. Emit our custom signal with these coordinates
        self.canvas_clicked.emit(point)