# -*- coding: utf-8 -*-
"""Elevation Area Extractor - Windows desktop application."""
from __future__ import annotations
import queue, threading, traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
import rasterio
from rasterio.mask import mask as raster_mask
from rasterio.features import shapes as raster_shapes
from rasterio.windows import from_bounds, Window
from scipy import ndimage
import fiona
from fiona.transform import transform_geom
from pyproj import CRS
from shapely.geometry import shape as shp_shape
from shapely.ops import unary_union

APP_TITLE = "استخراج نواحی ارتفاعی"

def area_km2(geom, crs):
    g = shp_shape(geom)
    crs = CRS.from_user_input(crs)
    if crs.is_projected:
        ax = crs.axis_info[0] if crs.axis_info else None
        factor = float(ax.unit_conversion_factor) if ax and ax.unit_conversion_factor else 1.0
        return abs(g.area) * factor * factor / 1_000_000.0
    a, _ = crs.get_geod().geometry_area_perimeter(g)
    return abs(a) / 1_000_000.0

def remove_old_shp(path):
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix"):
        Path(str(Path(path).with_suffix("")) + ext).unlink(missing_ok=True)

def run_analysis(area_path, dem_path, out_path, mode, min_z, max_z, max_cells, log):
    area_path, dem_path, out_path = Path(area_path), Path(dem_path), Path(out_path)
    if out_path.suffix.lower() != ".shp":
        raise ValueError("مسیر خروجی باید به .shp ختم شود.")
    if not area_path.exists() or not dem_path.exists():
        raise FileNotFoundError("فایل محدوده یا DEM پیدا نشد.")
    if mode == "between" and (min_z is None or max_z is None or min_z > max_z):
        raise ValueError("بازه حداقل/حداکثر ارتفاع معتبر نیست.")
    if mode == "above" and min_z is None:
        raise ValueError("حداقل ارتفاع را وارد کنید.")
    if mode == "below" and max_z is None:
        raise ValueError("حداکثر ارتفاع را وارد کنید.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log("خواندن محدوده پلیگونی…")
    geoms = []
    with fiona.open(area_path) as area_src:
        if not area_src.crs_wkt and not area_src.crs:
            raise ValueError("سیستم مختصات محدوده تعریف نشده است.")
        area_crs = CRS.from_user_input(area_src.crs_wkt or area_src.crs)
        for feat in area_src:
            if not feat.get("geometry"):
                continue
            g = shp_shape(feat["geometry"])
            if g.is_empty:
                continue
            if g.geom_type not in ("Polygon", "MultiPolygon"):
                raise ValueError("ورودی محدوده باید پلیگون یا مولتی‌پلیگون باشد.")
            geoms.append((feat["geometry"], area_crs))
    if not geoms:
        raise ValueError("هیچ پلیگون معتبری در لایه وجود ندارد.")
    with rasterio.open(dem_path) as src:
        if src.crs is None:
            raise ValueError("سیستم مختصات DEM تعریف نشده است.")
        ras_crs = CRS.from_user_input(src.crs)
        raster_geoms = []
        for geom, gcrs in geoms:
            if gcrs != ras_crs:
                geom = transform_geom(gcrs.to_string(), ras_crs.to_string(), geom)
            raster_geoms.append(geom)
        union_geom = unary_union([shp_shape(g) for g in raster_geoms])
        win = from_bounds(*union_geom.bounds, transform=src.transform).round_offsets().round_lengths()
        try:
            win = win.intersection(Window(0, 0, src.width, src.height))
        except Exception as exc:
            raise ValueError("محدوده با گستره DEM همپوشانی ندارد.") from exc
        estimate = max(0, int(win.width)) * max(0, int(win.height))
        log(f"تخمین سلول‌های محدوده: {estimate:,}")
        if estimate < 1:
            raise ValueError("محدوده با گستره DEM همپوشانی ندارد.")
        if estimate > max_cells:
            raise MemoryError(
                f"محدوده حدود {estimate:,} سلول دارد؛ بیش از سقف ایمنی {max_cells:,}. "
                "محدوده را کوچک کنید یا فقط در صورت داشتن RAM کافی سقف را افزایش دهید."
            )
        log("برش DEM به محدوده…")
        try:
            clipped, transform = raster_mask(
                src, raster_geoms, crop=True, filled=False, indexes=1, all_touched=False
            )
        except ValueError as exc:
            raise ValueError("محدوده با DEM همپوشانی ندارد.") from exc
        arr = np.asarray(clipped.data if np.ma.isMaskedArray(clipped) else clipped)
        mask = np.ma.getmaskarray(clipped)
        valid = (~mask) & np.isfinite(arr)
        if src.nodata is not None:
            valid &= arr != src.nodata
        log("انتخاب سلول‌های ارتفاعی…")
        if mode == "between":
            selected = valid & (arr >= min_z) & (arr <= max_z)
            range_text = f"{min_z:g} تا {max_z:g}"
        elif mode == "above":
            selected = valid & (arr > min_z)
            range_text = f"بیشتر از {min_z:g}"
        else:
            selected = valid & (arr < max_z)
            range_text = f"کمتر از {max_z:g}"
        selected_count = int(np.count_nonzero(selected))
        if not selected_count:
            raise ValueError(f"برای ارتفاع {range_text} هیچ سلولی پیدا نشد.")
        log(f"سلول‌های انتخاب‌شده: {selected_count:,}")
        log("شناسایی نواحی پیوسته…")
        structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
        labels, nregions = ndimage.label(selected, structure=structure)
        if nregions > 1_000_000:
            raise MemoryError(f"تعداد نواحی جداگانه زیاد است ({nregions:,}). پردازش متوقف شد.")
        if nregions == 0:
            raise ValueError("ناحیه‌ای برای ساخت پلیگون پیدا نشد.")
        ids = np.arange(1, nregions + 1, dtype=np.int32)
        counts = np.bincount(labels.ravel(), minlength=nregions + 1)[1:]
        mins = ndimage.minimum(arr, labels=labels, index=ids)
        maxs = ndimage.maximum(arr, labels=labels, index=ids)
        means = ndimage.mean(arr, labels=labels, index=ids)
        crs_wkt = src.crs.to_wkt()
        log(f"تعداد نواحی پیوسته: {nregions:,}")
        schema = {
            "geometry": "Polygon",
            "properties": {
                "REGION_ID": "int:10", "MIN_ELEV": "float:18.4",
                "MAX_ELEV": "float:18.4", "MEAN_ELEV": "float:18.4",
                "AREA_KM2": "float:18.6", "CELL_CNT": "int:10"
            }
        }
        log("تبدیل نواحی به پلیگون و نوشتن SHP…")
        remove_old_shp(out_path)
        written = 0
        try:
            with fiona.open(
                out_path, "w", driver="ESRI Shapefile", schema=schema,
                crs_wkt=crs_wkt, encoding="UTF-8"
            ) as sink:
                for geo, rid_raw in raster_shapes(
                    labels, mask=labels > 0, transform=transform, connectivity=4
                ):
                    rid = int(rid_raw)
                    sink.write({
                        "geometry": geo,
                        "properties": {
                            "REGION_ID": rid,
                            "MIN_ELEV": float(mins[rid - 1]),
                            "MAX_ELEV": float(maxs[rid - 1]),
                            "MEAN_ELEV": float(means[rid - 1]),
                            "AREA_KM2": float(area_km2(geo, ras_crs)),
                            "CELL_CNT": int(counts[rid - 1])
                        }
                    })
                    written += 1
                    if written % 1000 == 0:
                        log(f"پلیگون‌های نوشته‌شده: {written:,}")
        except Exception:
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                try:
                    Path(str(out_path.with_suffix("")) + ext).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
    log(f"انجام شد؛ تعداد پلیگون‌ها: {written:,}")
    return {"output": str(out_path), "regions": written, "selected": selected_count, "range": range_text}

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("900x700")
        root.minsize(780, 620)
        self.q = queue.Queue()
        self.running = False
        self.area = tk.StringVar()
        self.dem = tk.StringVar()
        self.out = tk.StringVar()
        self.mode = tk.StringVar(value="بین حداقل و حداکثر")
        self.minv = tk.StringVar(value="30")
        self.maxv = tk.StringVar(value="100")
        self.cells = tk.StringVar(value="15000000")
        self.status = tk.StringVar(value="آماده")
        self.build()
        root.after(150, self.poll)

    def build(self):
        f = ttk.Frame(self.root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(11, weight=1)
        ttk.Label(f, text="استخراج نواحی ارتفاعی از DEM", font=("Tahoma", 16, "bold"), anchor="e").grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0,4))
        ttk.Label(f, text="محدوده پلیگونی + DEM (TIFF) ← شیپ‌فایل نواحی پیوسته با آمار ارتفاع", anchor="e").grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0,12))
        self.file_row(f, 2, "محدوده مطالعه (Polygon)", self.area, self.browse_area)
        self.file_row(f, 3, "فایل DEM (TIFF)", self.dem, self.browse_dem)
        self.file_row(f, 4, "خروجی شیپ‌فایل (.shp)", self.out, self.browse_out)
        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(f, text="حالت ارتفاع", anchor="e").grid(row=6, column=0, sticky="e", padx=8, pady=4)
        cb = ttk.Combobox(f, textvariable=self.mode, values=["بین حداقل و حداکثر", "بیشتر از حداقل", "کمتر از حداکثر"], state="readonly", width=27)
        cb.grid(row=6, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_fields())
        ttk.Label(f, text="حداقل ارتفاع (واحد DEM)", anchor="e").grid(row=7, column=0, sticky="e", padx=8, pady=4)
        self.minentry = ttk.Entry(f, textvariable=self.minv, width=24, justify="right")
        self.minentry.grid(row=7, column=1, sticky="w")
        ttk.Label(f, text="حداکثر ارتفاع (واحد DEM)", anchor="e").grid(row=8, column=0, sticky="e", padx=8, pady=4)
        self.maxentry = ttk.Entry(f, textvariable=self.maxv, width=24, justify="right")
        self.maxentry.grid(row=8, column=1, sticky="w")
        ttk.Label(f, text="حداکثر تعداد سلول (سقف ایمنی)", anchor="e").grid(row=9, column=0, sticky="e", padx=8, pady=4)
        ttk.Entry(f, textvariable=self.cells, width=24, justify="right").grid(row=9, column=1, sticky="w")
        ttk.Label(f, text="برای جلوگیری از مصرف بیش‌ازحد حافظه، محدوده‌های بزرگ‌تر از سقف متوقف می‌شوند.", anchor="e", wraplength=650).grid(row=10, column=0, columnspan=3, sticky="ew", pady=4)
        bar = ttk.Frame(f)
        bar.grid(row=11, column=0, columnspan=3, sticky="nsew")
        bar.columnconfigure(0, weight=1)
        self.logbox = tk.Text(bar, height=12, wrap="word", state="disabled", font=("Consolas", 10))
        self.logbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(bar, command=self.logbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.logbox.configure(yscrollcommand=sb.set)
        btnbar = ttk.Frame(f)
        btnbar.grid(row=12, column=0, columnspan=3, sticky="ew", pady=8)
        self.runbtn = ttk.Button(btnbar, text="شروع استخراج", command=self.start)
        self.runbtn.pack(side="left")
        self.progress = ttk.Progressbar(btnbar, mode="indeterminate", length=220)
        self.progress.pack(side="left", padx=12)
        ttk.Label(f, textvariable=self.status, anchor="e").grid(row=13, column=0, columnspan=3, sticky="ew")
        self.update_fields()

    def file_row(self, p, r, label, var, fn):
        ttk.Label(p, text=label, anchor="e").grid(row=r, column=0, sticky="e", padx=8, pady=4)
        ttk.Entry(p, textvariable=var).grid(row=r, column=1, sticky="ew")
        ttk.Button(p, text="انتخاب…", command=fn).grid(row=r, column=2, padx=6)

    def browse_area(self):
        p = filedialog.askopenfilename(title="انتخاب محدوده پلیگونی", filetypes=[("Vector", "*.shp *.gpkg *.geojson"), ("All files", "*.*")])
        if p:
            self.area.set(p)
            if not self.out.get():
                self.out.set(str(Path(p).with_name(Path(p).stem + "_ElevationZones.shp")))

    def browse_dem(self):
        p = filedialog.askopenfilename(title="انتخاب DEM", filetypes=[("GeoTIFF", "*.tif *.tiff"), ("All files", "*.*")])
        if p:
            self.dem.set(p)

    def browse_out(self):
        p = filedialog.asksaveasfilename(title="ذخیره خروجی", defaultextension=".shp", initialfile="ElevationZones.shp", filetypes=[("Shapefile", "*.shp")])
        if p:
            self.out.set(p)

    def update_fields(self):
        self.minentry.configure(state="disabled" if self.mode.get() == "کمتر از حداکثر" else "normal")
        self.maxentry.configure(state="disabled" if self.mode.get() == "بیشتر از حداقل" else "normal")

    def log(self, text):
        self.q.put(("log", text))

    def start(self):
        if self.running:
            return
        if not self.area.get().strip() or not self.dem.get().strip() or not self.out.get().strip():
            messagebox.showerror("ورودی ناقص", "محدوده، DEM و مسیر خروجی را انتخاب کنید.")
            return
        try:
            cells = int(self.cells.get().replace(",", ""))
            mode = {"بین حداقل و حداکثر": "between", "بیشتر از حداقل": "above", "کمتر از حداکثر": "below"}[self.mode.get()]
            minv = float(self.minv.get()) if mode in ("between", "above") else None
            maxv = float(self.maxv.get()) if mode in ("between", "below") else None
            if cells < 1 or (mode == "between" and minv > maxv):
                raise ValueError
        except Exception:
            messagebox.showerror("پارامتر نامعتبر", "ارتفاع‌ها یا حداکثر تعداد سلول را درست وارد کنید.")
            return
        area_path, dem_path, out_path = self.area.get(), self.dem.get(), self.out.get()
        self.running = True
        self.runbtn.configure(state="disabled")
        self.progress.start(10)
        self.status.set("در حال پردازش…")
        self.log("شروع پردازش")

        def work():
            try:
                result = run_analysis(area_path, dem_path, out_path, mode, minv, maxv, cells, self.log)
                self.q.put(("ok", result))
            except Exception as exc:
                self.q.put(("err", (str(exc), traceback.format_exc())))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", value + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "ok":
                    self.running = False
                    self.runbtn.configure(state="normal")
                    self.progress.stop()
                    self.status.set("پردازش موفق بود.")
                    messagebox.showinfo("پایان", f"خروجی: {value['output']}\nپلیگون‌ها: {value['regions']:,}\nسلول‌های منتخب: {value['selected']:,}")
                elif kind == "err":
                    self.running = False
                    self.runbtn.configure(state="normal")
                    self.progress.stop()
                    self.status.set("پردازش با خطا پایان یافت.")
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", "خطا: " + value[0] + "\n" + value[1] + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                    messagebox.showerror("خطا", value[0])
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

def main():
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
