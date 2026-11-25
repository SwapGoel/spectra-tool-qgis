# Spectral Tool (QGIS plugin)

Interactive spectral profiler for any multi-band raster in QGIS 3.x (including LTR) and QGIS 4.

- Click the map to plot every band of a pixel, or switch to **Area** and drag a box to plot its average spectrum.
- **Multi-Point**: compare many pixels of the active layer. **Multi-Layer**: one location across every visible raster.
- The x-axis is wavelength when the data provides it (ENVI headers, GDAL / NetCDF / HDF metadata, band names such as `665 nm`), otherwise band index. Toggle with the ↔ button.
- Axes fit the data's own min and max. Pan (drag), box zoom (Alt/Option-drag), wheel zoom and typed limits hold the view until **Reset**.
- Legend: click to hide a curve, Shift-click to show only that one, click a layer name to toggle all its curves.
- Light and dark themes follow QGIS and the system appearance automatically.

Requires the `matplotlib` Python package (bundled with the standard QGIS installers).

Issues: https://github.com/SwapGoel/spectra-tool-qgis/issues
