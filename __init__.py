"""Spectral Tool - interactive spectral profiler for raster layers (QGIS 3.x
and 4)."""


def classFactory(iface):  # pylint: disable=invalid-name
    """Called by QGIS to load the plugin."""
    from .spectra import SpectralTool

    return SpectralTool(iface)
