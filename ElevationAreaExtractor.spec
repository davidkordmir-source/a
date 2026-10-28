# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

APP_DIR = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ("numpy", "scipy", "rasterio", "fiona", "shapely", "pyproj"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
hiddenimports += [
    "rasterio._base", "rasterio._io", "rasterio._features",
    "rasterio._env", "fiona._env", "fiona.ogrext", "shapely.lib",
    "pyproj._network", "scipy.ndimage", "scipy.ndimage._nd_image",
    "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
]
a = Analysis(
    [str(APP_DIR / "elevation_area_extractor.py")],
    pathex=[str(APP_DIR)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=["matplotlib", "pytest", "IPython", "notebook"],
    noarchive=False, optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ElevationAreaExtractor",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[],
               name="ElevationAreaExtractor")
