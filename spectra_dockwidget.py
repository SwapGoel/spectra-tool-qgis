import os
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import pyqtSignal

# Matplotlib imports
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

class SpectraDockWidget(QtWidgets.QDockWidget):
    closingPlugin = pyqtSignal()
    resetTriggered = pyqtSignal() # NEW: Signal to tell the main app to reset

    def __init__(self, parent=None):
        """Constructor."""
        super(SpectraDockWidget, self).__init__(parent)
        
        self.traces = []  
        self.chart_title = "Spectral Profile"

        self.main_widget = QtWidgets.QWidget()
        self.setWidget(self.main_widget)
        
        self.layout = QtWidgets.QVBoxLayout()
        self.main_widget.setLayout(self.layout)
        
        # --- CONTROL PANEL ---
        self.controls_layout = QtWidgets.QHBoxLayout()
        
        # 1. Wavelength Checkbox
        self.chk_wavelength = QtWidgets.QCheckBox("Plot Wavelengths (nm)")
        self.chk_wavelength.setChecked(True)
        self.chk_wavelength.setEnabled(False)
        self.chk_wavelength.toggled.connect(self.refresh_plot)
        
        # 2. Multi-Select Checkbox
        self.chk_multi = QtWidgets.QCheckBox("Multi-Select Mode")
        self.chk_multi.setChecked(False)

        # 3. Reset Button (NEW)
        self.btn_reset = QtWidgets.QPushButton("Reset")
        self.btn_reset.clicked.connect(self.on_reset_click)

        self.controls_layout.addWidget(self.chk_wavelength)
        self.controls_layout.addWidget(self.chk_multi)
        self.controls_layout.addStretch() # Spacer
        self.controls_layout.addWidget(self.btn_reset) # Add Reset button to right
        
        self.layout.addLayout(self.controls_layout)
        
        # --- PLOT AREA ---
        self.figure = Figure(figsize=(5, 4), dpi=100) 
        self.canvas = FigureCanvas(self.figure)
        self.layout.addWidget(self.canvas)
        
        self.ax = self.figure.add_subplot(111)
        
        # Interactivity elements
        self.annot = None
        self.vline = None
        self.hline = None

        self.canvas.mpl_connect("motion_notify_event", self.on_hover)
        
        self.reset_plot_area()

    def on_reset_click(self):
        """Emits signal so the main plugin can clear markers too."""
        self.resetTriggered.emit()

    def reset_plot_area(self):
        self.ax.clear()
        self.ax.set_title("Ready to Plot")
        self.ax.set_xlabel("Band Number")
        self.ax.set_ylabel("Pixel Value")
        self.ax.grid(True, alpha=0.3)
        self.canvas.draw()

    def clear_all_traces(self):
        """Clears internal data and resets the graph."""
        self.traces = []
        self.chk_wavelength.setEnabled(False)
        self.reset_plot_area() # Wipes the visual graph

    def add_trace(self, x_data, y_data, color, label, layer_name):
        self.traces.append({
            'x': x_data,
            'y': y_data,
            'color': color,
            'label': label
        })
        self.chart_title = layer_name
        
        if x_data is not None:
            self.chk_wavelength.setEnabled(True)
            self.chk_wavelength.setChecked(True)
            
        self.refresh_plot()

    def refresh_plot(self):
        """Redraws all stored traces."""
        self.ax.clear()
        
        if not self.traces:
            self.reset_plot_area()
            return

        has_wavelengths = (self.traces[0]['x'] is not None)
        use_wavelengths = self.chk_wavelength.isChecked() and has_wavelengths
        
        if use_wavelengths:
            x_label = "Wavelength (nm)"
        else:
            x_label = "Band Number"

        all_x = []
        all_y = []

        # 1. Plot Data
        for trace in self.traces:
            y_vals = trace['y']
            
            if use_wavelengths:
                x_vals = trace['x']
            else:
                x_vals = list(range(1, len(y_vals) + 1))
            
            all_x.extend(x_vals)
            all_y.extend(y_vals)

            # Create line object first, then set picker properties
            line, = self.ax.plot(x_vals, y_vals, color=trace['color'], marker='o', 
                         markersize=4, label=trace['label'])
            line.set_picker(True)
            line.set_pickradius(5)

        # 2. Add Wavelength Reference Lines
        if use_wavelengths:
            ref_lines = [(485.9, 'red', 'R'), (560.0, 'green', 'G'), 
                         (657.1, 'blue', 'B'), (866.0, 'black', 'NIR')]
            text_y = 1.0
            for wl, c, lbl in ref_lines:
                self.ax.axvline(x=wl, color=c, linestyle='--', linewidth=1, alpha=0.5)
                self.ax.text(wl, text_y, f" {lbl}", color=c, fontsize=8, rotation=90, verticalalignment='top')

        # 3. Styling
        self.ax.set_title(f"{self.chart_title} ({len(self.traces)} Points)")
        self.ax.set_xlabel(x_label)
        self.ax.set_ylabel("Pixel Value")
        self.ax.grid(True, alpha=0.3)
        self.ax.legend()
        
        # 4. LIMITS
        # Y-Axis: Fixed 0-1
        self.ax.set_ylim(0, 1)

        # X-Axis: Dynamic
        if all_x:
            x_min, x_max = min(all_x), max(all_x)
            x_pad = (x_max - x_min) * 0.02 if x_max != x_min else 1.0
            self.ax.set_xlim(x_min - x_pad, x_max + x_pad)

        # 5. Re-init Hover Elements
        center_x = (min(all_x) + max(all_x)) / 2 if all_x else 0
        
        self.annot = self.ax.annotate("", xy=(center_x, 0), xytext=(15,15),
                            textcoords="offset points",
                            bbox=dict(boxstyle="round", fc="white", ec="gray", alpha=0.9),
                            arrowprops=dict(arrowstyle="->"))
        self.annot.set_visible(False)
        
        self.vline = self.ax.axvline(x=center_x, color='gray', linestyle=':', linewidth=1)
        self.hline = self.ax.axhline(y=0, color='gray', linestyle=':', linewidth=1)
        self.vline.set_visible(False)
        self.hline.set_visible(False)

        self.canvas.draw()

    def on_hover(self, event):
        if event.inaxes != self.ax or not self.traces:
            if self.annot and self.annot.get_visible():
                self.annot.set_visible(False)
                self.vline.set_visible(False)
                self.hline.set_visible(False)
                self.canvas.draw_idle()
            return

        mouse_x, mouse_y = event.xdata, event.ydata
        best_dist = float('inf')
        best_point = None
        
        use_wavelengths = self.chk_wavelength.isChecked() and (self.traces[0]['x'] is not None)

        for trace in self.traces:
            y_vals = trace['y']
            x_vals = trace['x'] if use_wavelengths else list(range(1, len(y_vals) + 1))
            
            for i, x in enumerate(x_vals):
                y = y_vals[i]
                dist = (x - mouse_x)**2 + (y - mouse_y)**2
                
                if dist < best_dist:
                    best_dist = dist
                    best_point = (x, y, trace['color'])

        if best_point:
            snap_x, snap_y, color = best_point
            
            self.vline.set_xdata([snap_x, snap_x])
            self.hline.set_ydata([snap_y, snap_y])
            self.vline.set_visible(True)
            self.hline.set_visible(True)
            
            self.annot.xy = (snap_x, snap_y)
            self.annot.set_text(f"X: {snap_x:.1f}\nY: {snap_y:.4f}")
            self.annot.get_bbox_patch().set_edgecolor(color)
            self.annot.set_visible(True)
            
            self.canvas.draw_idle()

    def closeEvent(self, event):
        self.closingPlugin.emit()
        event.accept()