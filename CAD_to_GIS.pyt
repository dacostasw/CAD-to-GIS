# -*- coding: utf-8 -*-
"""
CAD to GIS (v0.1.10) - ArcGIS Pro Python toolbox.
Date: 2026-10-12
-- Test phase --
Designed for ArcGIS Pro 3.0+ (Python 3.9+). See README.md for installation and usage.

Checks the CAD drawings in a folder against a reference layer that is known to
be in the right place (for example, borings). Reports which coordinate system
each drawing is in. In "Check and fix" mode, writes the matching .prj so the
drawing lands in the right place.

Drawings are never edited. Only .prj side files are written. See README.md.
Runs in ArcGIS Pro's default Python environment (arcpy + standard library).
"""
import datetime
import getpass
import html
import math
import os
import shutil
import statistics
import time

import arcpy

VERSION = "0.1.10"
CAD_EXTENSIONS = [".dwg", ".dxf", ".dgn"]
OUTPUT_SUBFOLDER = "_GIS_ready"

# Coordinate systems tried in addition to the reference layer's own system and
# the drawing's existing .prj. Only 6597 has been checked with arcpy so far.
# Codes arcpy does not recognize are skipped with a warning.
CANDIDATE_WKIDS = [
    6597,  # NAD83(2011) State Plane WA North, US ft (checked)
    6596,  # NAD83(2011) State Plane WA North, m      (to verify)
    6599,  # NAD83(2011) State Plane WA South, US ft  (to verify)
    6339,  # NAD83(2011) UTM zone 10N, m              (to verify)
    6318,  # NAD83(2011) geographic, degrees          (to verify)
]

# The reference box grows by this fraction of its longer side.
MARGIN_FRACTION = 0.5
# Flag stray objects when the drawing's full width is this many times the width
# of its core (the 5th to 95th percentile of feature positions).
STRAY_RATIO = 20.0

MODE_CHECK = "Check only"
MODE_FIX = "Check and fix"
WRITE_COPIES = "Copies in _GIS_ready subfolder"
WRITE_ORIGINALS = "Original folders (adds a .prj next to each drawing)"
CHANGE_LOG = "_CAD_to_GIS_changes.txt"
DONE_OPEN_REPORT = "Generate report and open in browser"
DONE_ADD_TO_MAP = "Add ready drawings to the current map"


# --------------------------------------------------------------------------
# Toolbox and tool
# --------------------------------------------------------------------------
class Toolbox:
    def __init__(self):
        self.label = "CAD to GIS"
        self.alias = "cadtogis"
        self.tools = [CheckCADCoordinates]


class CheckCADCoordinates:
    def __init__(self):
        self.label = "Check CAD Coordinates"
        self.description = (
            "Checks CAD drawings against a reference layer, reports which "
            "coordinate system each drawing is in and, in 'Check and fix' "
            "mode, writes the matching .prj file. Drawings are never edited."
        )

    def getParameterInfo(self):
        folder = arcpy.Parameter(
            displayName="CAD folder (subfolders included)",
            name="cad_folder", datatype="DEFolder",
            parameterType="Required", direction="Input")

        ref = arcpy.Parameter(
            displayName="Reference layer (known to be in the right place, e.g. borings)",
            name="reference_layer", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")

        mode = arcpy.Parameter(
            displayName="Mode", name="mode", datatype="GPString",
            parameterType="Required", direction="Input")
        mode.filter.type = "ValueList"
        mode.filter.list = [MODE_CHECK, MODE_FIX]
        mode.value = MODE_CHECK

        write_to = arcpy.Parameter(
            displayName="Write fixes to", name="write_to", datatype="GPString",
            parameterType="Optional", direction="Input")
        write_to.filter.type = "ValueList"
        write_to.filter.list = [WRITE_ORIGINALS, WRITE_COPIES]
        write_to.value = WRITE_ORIGINALS
        write_to.enabled = False

        layers = arcpy.Parameter(
            displayName="Check each CAD layer (finds parts of combined drawings)",
            name="check_layers", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        layers.value = True

        types = arcpy.Parameter(
            displayName="File types", name="file_types", datatype="GPString",
            parameterType="Optional", direction="Input", multiValue=True)
        types.filter.type = "ValueList"
        types.filter.list = CAD_EXTENSIONS
        types.values = CAD_EXTENSIONS

        extra = arcpy.Parameter(
            displayName="Extra coordinate systems to try (e.g. the project ground system)",
            name="extra_systems", datatype="GPSpatialReference",
            parameterType="Optional", direction="Input", multiValue=True)

        when_done = arcpy.Parameter(
            displayName="Output options", name="when_done", datatype="GPString",
            parameterType="Optional", direction="Input", multiValue=True)
        when_done.filter.type = "ValueList"
        when_done.filter.list = [DONE_OPEN_REPORT, DONE_ADD_TO_MAP]
        when_done.values = [DONE_OPEN_REPORT]

        report = arcpy.Parameter(
            displayName="Report", name="report", datatype="DEFile",
            parameterType="Derived", direction="Output")

        return [folder, ref, mode, write_to, layers, types, extra, when_done, report]

    def updateParameters(self, parameters):
        parameters[3].enabled = parameters[2].valueAsText == MODE_FIX
        return

    def updateMessages(self, parameters):
        # Block the run only when the folder holds no CAD files at all
        folder_p = parameters[0]
        if folder_p.value and not folder_p.hasError():
            folder = folder_p.valueAsText
            exts = [e.lower() for e in (parameters[5].values or CAD_EXTENSIONS)]
            if os.path.isdir(folder):
                counts, _, _ = count_cad_files(folder, exts)
                if sum(counts.values()) == 0:
                    folder_p.setErrorMessage(
                        f"No {', '.join(exts)} files in this folder or its subfolders.")
        return

    def execute(self, parameters, messages):
        t0 = time.time()
        cad_folder = parameters[0].valueAsText
        ref_layer = parameters[1].valueAsText
        mode = parameters[2].valueAsText
        write_to = parameters[3].valueAsText if mode == MODE_FIX else None
        check_layers = bool(parameters[4].value)
        exts = [e.lower() for e in (parameters[5].values or CAD_EXTENSIONS)]

        ref_sr = arcpy.Describe(ref_layer).spatialReference
        if ref_sr is None or ref_sr.name == "Unknown":
            arcpy.AddError("The reference layer has no coordinate system defined. "
                           "Use a layer whose coordinate system is known.")
            return
        files = find_cad_files(cad_folder, exts)
        if not files:
            arcpy.AddError(f"No {', '.join(exts)} files found in {os.path.basename(cad_folder)}.")
            return

        n_ref = int(arcpy.management.GetCount(ref_layer)[0])
        candidates = build_candidates(ref_sr, parse_extra_systems(parameters[6]))
        print_header(mode, cad_folder, files, ref_layer, n_ref, ref_sr, candidates)
        boxes = BoxCache(ref_layer)
        if boxes.get(ref_sr) is None:
            arcpy.AddError(f"Could not use the reference layer: {boxes.why(ref_sr)}.")
            return

        # Pass 1: read every drawing once
        arcpy.SetProgressor("step", "Reading CAD files...", 0, len(files), 1)
        reads = []
        for path in files:
            arcpy.SetProgressorLabel(f"Reading {os.path.basename(path)}")
            reads.append(read_file(path, cad_folder, check_layers))
            arcpy.SetProgressorPosition()

        # Drawings whose own .prj lands on site teach us their system, which
        # often applies to drawings in the same folder that have no .prj
        learned = learn_systems(reads, candidates, boxes)
        if learned:
            arcpy.AddMessage("Learned from drawings that check OK")
            for label, _ in learned:
                arcpy.AddMessage(f"  - {label}")
        candidates = candidates + learned

        # Pass 2: classify
        results, layer_rows = [], []
        for res, loc in reads:
            if loc is not None:
                layer_rows.extend(classify(res, loc, candidates, boxes, check_layers))
            results.append(res)

        if mode == MODE_CHECK or write_to == WRITE_COPIES:
            for res in results:
                mark_existing_fix(res, cad_folder)

        if mode == MODE_FIX:
            arcpy.SetProgressorLabel("Applying fixes...")
            for res in results:
                apply_fix(res, cad_folder, write_to)
            write_change_log(results, cad_folder)

        when_done = list(parameters[7].values or [])
        folder_name = os.path.basename(cad_folder.rstrip("\\/"))
        n_ready = sum(1 for r in results if r["status"] in ("OK", "FIXED"))
        # The map is filled after the report, so the report doesn't wait for it
        adding = ({"count": n_ready, "group": f"CAD to GIS - {folder_name}"}
                  if DONE_ADD_TO_MAP in when_done and n_ready else None)

        seconds = time.time() - t0
        context = {"mode": mode, "write_to": write_to, "added": adding,
                   "ref_name": os.path.basename(ref_layer),
                   "n_ref": n_ref, "ref_sr": ref_sr, "candidates": candidates,
                   "cad_folder": os.path.basename(cad_folder.rstrip("\\/")), "seconds": seconds}
        report_path = write_html_report(results, layer_rows, context)
        print_summary(results, mode, write_to, report_path, seconds, adding)
        parameters[8].value = report_path
        if DONE_OPEN_REPORT in when_done:
            open_in_browser(report_path)

        if adding:
            t1 = time.time()
            arcpy.SetProgressorLabel("Adding ready drawings to the map...")
            arcpy.AddMessage("")
            arcpy.AddMessage(f"Adding {n_ready} ready drawing(s) to the map. The report is already open.")
            add_ready_to_map(results, folder_name)
            arcpy.AddMessage(f"Map time: {time.time() - t1:.1f} s")
        arcpy.ResetProgressor()
        return


# --------------------------------------------------------------------------
# Coordinate system helpers
# --------------------------------------------------------------------------
def sr_wkt(sr):
    """WKT of a spatial reference, without arcpy's precision suffix."""
    return sr.exportToString().split(";")[0]


def same_sr(a, b):
    if a.factoryCode and b.factoryCode:
        return a.factoryCode == b.factoryCode
    return a.name == b.name


# Coordinate system properties compared between the reference layer and each
# drawing's .prj. Read with getattr, so a property a system doesn't have
# (e.g. false easting on lat/long) is simply None.
SR_PROPS = [
    ("projection", "projectionName"),
    ("datum", "datumName"),
    ("units", "linearUnitName"),
    ("false easting", "falseEasting"),
    ("false northing", "falseNorthing"),
    ("scale factor", "scaleFactor"),
    ("central meridian", "centralMeridian"),
    ("latitude of origin", "latitudeOfOrigin"),
    ("standard parallel 1", "standardParallel1"),
    ("standard parallel 2", "standardParallel2"),
]


def sr_props(sr):
    out = {}
    for label, attr in SR_PROPS:
        try:
            if attr == "datumName":
                # Projected systems keep the datum on their geographic part
                v = getattr(getattr(sr, "GCS", None), "datumName", None) or getattr(sr, "datumName", None)
            else:
                v = getattr(sr, attr)
        except Exception:
            v = None
        if attr == "scaleFactor" and v == 0:
            v = 1.0  # arcpy reports 0 when a projection has no scale factor; treat as 1
        out[label] = v if v not in ("", None) else None
    return out


def fmt(v):
    return f"{v:.10g}" if isinstance(v, float) else str(v)


def compare_sr(cad_sr, ref_sr):
    """Compare a drawing's declared coordinate system with the reference's.
    Returns (short label, full description)."""
    a, b = sr_props(cad_sr), sr_props(ref_sr)
    diffs = []
    for k in a:
        va, vb = a[k], b[k]
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if not math.isclose(va, vb, rel_tol=1e-9, abs_tol=1e-9):
                diffs.append(f"{k} {fmt(va)} vs {fmt(vb)}")
        elif va != vb:
            diffs.append(f"{k} {fmt(va)} vs {fmt(vb)}")
    if not diffs:
        if cad_sr.name == ref_sr.name:
            return "same", "same as reference"
        return "same", f"same parameters as reference, different name ({cad_sr.name})"
    return "different", "different from reference: " + "; ".join(diffs)


def same_grid(a, b):
    """True when two systems differ at most in datum (e.g. NAD83 vs NAD83(2011))."""
    pa, pb = sr_props(a), sr_props(b)
    for k in pa:
        if k == "datum":
            continue
        va, vb = pa[k], pb[k]
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if not math.isclose(va, vb, rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif va != vb:
            return False
    return True


def describe_sr(sr):
    """Two short lines summarizing a coordinate system, for the log."""
    p = sr_props(sr)
    main = " | ".join(f"{k} {fmt(p[k])}" for k in ("projection", "datum", "units") if p[k] is not None)
    params = " | ".join(f"{k} {fmt(v)}" for k, v in p.items()
                        if v is not None and k not in ("projection", "datum", "units"))
    return main, params


def parse_extra_systems(param):
    """Spatial references from the optional 'extra coordinate systems' parameter."""
    out = []
    for v in (param.values or []):
        if isinstance(v, arcpy.SpatialReference):
            out.append(v)
            continue
        # Pro can pass the value as quoted text with a precision suffix:
        # 'PROJCS[...];-20037700 ...;IsHighPrecision'. Try the full text, then the WKT alone.
        text = str(v).strip().strip("'\"")
        for attempt in (text, text.split(";")[0]):
            try:
                sr = arcpy.SpatialReference()
                sr.loadFromString(attempt)
                out.append(sr)
                break
            except Exception:
                continue
        else:
            arcpy.AddWarning(f"Skipping an extra coordinate system that could not be read: {text[:60]}...")
    return out


def build_candidates(ref_sr, extra_srs=()):
    cands = [(f"Project system ({ref_sr.name})", ref_sr)]
    for sr in extra_srs:
        if not any(same_sr(sr, c) for _, c in cands):
            cands.append((f"{sr.name} (extra)", sr))
    for wkid in CANDIDATE_WKIDS:
        try:
            sr = arcpy.SpatialReference(wkid)
        except Exception:
            arcpy.AddWarning(f"Skipping candidate EPSG {wkid}: not recognized by arcpy.")
            continue
        if not any(same_sr(sr, c) for _, c in cands):
            cands.append((f"{sr.name} (EPSG {wkid})", sr))
    return cands


class BoxCache:
    """Reference layer's bounding box, converted into each coordinate system once."""

    def __init__(self, ref_layer):
        self.ref_layer = ref_layer
        self._boxes = {}

    def get(self, sr):
        key = sr_wkt(sr)
        if key not in self._boxes:
            self._boxes[key] = reference_box(self.ref_layer, sr)
        return self._boxes[key][0]

    def why(self, sr):
        """Reason the box for `sr` could not be built ('' if it was)."""
        self.get(sr)
        return self._boxes[sr_wkt(sr)][1]


def reference_box(ref_layer, sr):
    """(box, reason). The box covers the full extent of every reference
    feature (points, lines or polygons) in `sr`, plus a margin. Works with a
    single feature, such as one alignment line."""
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    n = 0
    try:
        with arcpy.da.SearchCursor(ref_layer, ["SHAPE@"], spatial_reference=sr) as cur:
            for row in cur:
                geom = row[0]
                if geom is None:
                    continue
                e = geom.extent
                if e is None or any(v is None or math.isnan(v)
                                    for v in (e.XMin, e.YMin, e.XMax, e.YMax)):
                    continue  # empty geometry
                xmin, ymin = min(xmin, e.XMin), min(ymin, e.YMin)
                xmax, ymax = max(xmax, e.XMax), max(ymax, e.YMax)
                n += 1
    except Exception as e:
        return None, f"could not convert it to {sr.name} ({e})"
    if n == 0:
        return None, "it has no features with geometry (check for a selection or definition query)"
    size = max(xmax - xmin, ymax - ymin)
    if size == 0:
        return None, "all its features sit on a single point; use a layer that covers the site"
    m = size * MARGIN_FRACTION
    return (xmin - m, ymin - m, xmax + m, ymax + m), ""


def inside(box, x, y):
    return box is not None and box[0] <= x <= box[2] and box[1] <= y <= box[3]


# --------------------------------------------------------------------------
# Reading CAD files
# --------------------------------------------------------------------------
def prj_path(path):
    return os.path.splitext(path)[0] + ".prj"


def find_cad_files(folder, exts):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != OUTPUT_SUBFOLDER]  # skip our own output
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                found.append(os.path.join(root, f))
    return sorted(found)


_FOLDER_COUNTS = {}  # cache, so the form stays fast while you edit it


def count_cad_files(folder, exts, limit=20000):
    """({ext: count}, folders with CAD files, truncated?) for the form's folder note."""
    key = (folder, tuple(exts))
    if key not in _FOLDER_COUNTS:
        counts = {e: 0 for e in exts}
        folders, seen, truncated = set(), 0, False
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d != OUTPUT_SUBFOLDER]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in counts:
                    counts[ext] += 1
                    folders.add(root)
            seen += len(files)
            if seen > limit:
                truncated = True
                break
        _FOLDER_COUNTS[key] = (counts, len(folders), truncated)
    return _FOLDER_COUNTS[key]


def read_cad_points(path, by_layer):
    """{cad_layer: [(x, y), ...]} in the drawing's own coordinates."""
    points = {}
    for child in arcpy.Describe(path).children:
        fc = os.path.join(path, child.name)
        try:
            names = {f.name.lower(): f.name for f in arcpy.ListFields(fc)}
            fields = ["SHAPE@XY"]
            if by_layer and "layer" in names:
                fields.append(names["layer"])
            with arcpy.da.SearchCursor(fc, fields) as cur:
                for row in cur:
                    xy = row[0]
                    if xy is None or xy[0] is None:
                        continue
                    layer = (row[1] or "(no layer)") if len(row) > 1 else "(all)"
                    points.setdefault(layer, []).append(xy)
        except Exception:
            continue  # e.g. a feature class without geometry
    return points


def percentile(sorted_vals, q):
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


# --------------------------------------------------------------------------
# Check one file: read it once, then classify it
# --------------------------------------------------------------------------
# Coordinates below this (in both X and Y) are treated as likely local or
# sheet coordinates. Real State Plane eastings in Washington are > 1,000,000 ft.
LOCAL_COORD_LIMIT = 100000


def read_file(path, root, check_layers):
    """Read a drawing once. Returns (res, loc); loc is None when the file is skipped."""
    res = {"file": os.path.relpath(path, root), "path": path, "status": "",
           "detail": "", "diagnosis": "", "center": "", "existing_prj": "",
           "crs_check": "", "crs_vs_reference": "", "suggested": "",
           "suggested_sr": None, "features": 0, "cad_layers": "",
           "offsite_layers": "", "review_kind": "", "likely_local": False,
           "stray": False, "already": False, "fix": "", "output": "",
           "location": os.path.dirname(os.path.relpath(path, root)) or "(top folder)",
           "backup": ""}
    try:
        existing_sr = arcpy.Describe(path).spatialReference
        has_prj = os.path.exists(prj_path(path))
        by_layer = read_cad_points(path, check_layers)
    except Exception as e:
        res.update(status="SKIP", detail=f"Could not read file: {e}")
        return res, None

    all_pts = [p for pts in by_layer.values() for p in pts]
    if not all_pts:
        res.update(status="SKIP", detail="No features with geometry")
        return res, None
    res["features"] = len(all_pts)

    # The median position is not thrown off by stray objects far from the site
    x = statistics.median(p[0] for p in all_pts)
    y = statistics.median(p[1] for p in all_pts)
    xs = sorted(p[0] for p in all_pts)
    core = percentile(xs, 0.95) - percentile(xs, 0.05)
    res["stray"] = core > 0 and (xs[-1] - xs[0]) > STRAY_RATIO * core
    res["center"] = f"X = {x:,.1f}, Y = {y:,.1f} (drawing units)"
    res["likely_local"] = abs(x) < LOCAL_COORD_LIMIT and abs(y) < LOCAL_COORD_LIMIT

    layers = {layer: (statistics.median(p[0] for p in pts),
                      statistics.median(p[1] for p in pts), len(pts))
              for layer, pts in by_layer.items()}
    loc = {"x": x, "y": y, "layers": layers, "existing_sr": existing_sr,
           "has_prj": has_prj,
           "existing_known": has_prj and existing_sr.name != "Unknown"}
    return res, loc


def lands_with_existing_prj(loc, boxes):
    return loc["existing_known"] and inside(boxes.get(loc["existing_sr"]), loc["x"], loc["y"])


def diagnose(x, y, likely_local, candidates, boxes):
    """Why a drawing matched nothing: the closest system and how far off it is."""
    if likely_local:
        return ("Coordinates are small numbers, typical of local or sheet coordinates "
                "rather than a mapping system.")
    # Offset from the site centre, read in the reference layer's own system.
    # A large, round shift in both directions usually means a ground system.
    ref_sr = candidates[0][1]
    box = boxes.get(ref_sr)
    if box is None:
        return ""
    dx = x - (box[0] + box[2]) / 2
    dy = y - (box[1] + box[3]) / 2
    unit = getattr(ref_sr, "linearUnitName", "") or "units"
    ew = f"{abs(dx):,.0f} {unit} {'east' if dx >= 0 else 'west'}"
    ns = f"{abs(dy):,.0f} {unit} {'north' if dy >= 0 else 'south'}"
    return (f"Read in the reference system, the drawing sits about {ew} and {ns} of the site. "
            f"Probably a system not on the list, such as a project ground system.")


def classify(res, loc, candidates, boxes, check_layers):
    """Set status and details on `res`. Returns the per-CAD-layer rows."""
    ref_sr = candidates[0][1]  # the reference layer's own system is always first
    x, y = loc["x"], loc["y"]
    existing_sr, has_prj = loc["existing_sr"], loc["has_prj"]
    warnings = ["stray objects far from the drawing"] if res["stray"] else []

    if loc["existing_known"]:
        res["existing_prj"] = existing_sr.name
        res["crs_check"], res["crs_vs_reference"] = compare_sr(existing_sr, ref_sr)
    else:
        res["existing_prj"] = "unreadable .prj" if has_prj else "none"
        res["crs_check"] = res["crs_vs_reference"] = res["existing_prj"]

    chosen = None
    if lands_with_existing_prj(loc, boxes):
        detail = "Existing .prj lands on the reference layer"
        if res["crs_check"] == "different":
            detail += " (its system differs from the reference; Pro aligns it on the fly)"
        res.update(status="OK", detail=detail)
        chosen = existing_sr
    else:
        fits, seen = [], set()
        for label, sr in candidates:
            if sr_wkt(sr) not in seen and inside(boxes.get(sr), x, y):
                fits.append((label, sr))
                seen.add(sr_wkt(sr))
        if len(fits) == 1:
            label, sr = fits[0]
            chosen = sr
            res.update(status="FIX", suggested=label, suggested_sr=sr)
            if has_prj and res["crs_check"] == "same":
                res["detail"] = (f"Existing .prj matches the reference's system, but the drawing's "
                                 f"coordinates are not in it; fits {label}")
            elif has_prj:
                res["detail"] = f"Existing .prj ({res['existing_prj']}) lands off-site; fits {label}"
            else:
                res["detail"] = f"No .prj; fits {label}"
        elif len(fits) > 1 and all(same_grid(fits[0][1], sr) for _, sr in fits[1:]):
            # Same grid, different NAD83 version (about 1 m apart). This check
            # can't tell them apart, so use the first one: the reference's
            # system when it fits.
            label, sr = fits[0]
            chosen = sr
            others = "; ".join(l for l, _ in fits[1:])
            prefix = f"Existing .prj ({res['existing_prj']}) lands off-site; fits" if has_prj else "No .prj; fits"
            res.update(status="FIX", suggested=label, suggested_sr=sr, review_kind="datum",
                       detail=f"{prefix} {label}",
                       diagnosis=(f"Also fits {others}: same grid, different NAD83 version "
                                  f"(about 1 m apart), which this check can't tell apart."))
        elif len(fits) > 1:
            res.update(status="REVIEW", review_kind="ambiguous",
                       detail="Fits more than one system: " + "; ".join(l for l, _ in fits))
        else:
            res.update(status="REVIEW", review_kind="nomatch",
                       detail="Matches no known coordinate system",
                       diagnosis=diagnose(x, y, res["likely_local"], candidates, boxes))

    layer_rows = []
    if check_layers and chosen is not None:
        box = boxes.get(chosen)
        offsite = 0
        for layer, (lx, ly, n) in sorted(loc["layers"].items()):
            ok = inside(box, lx, ly)
            offsite += 0 if ok else 1
            layer_rows.append({"file": res["file"], "cad_layer": layer,
                               "features": n, "lands_on_site": ok})
        res["cad_layers"] = len(loc["layers"])
        res["offsite_layers"] = offsite
        if offsite:
            warnings.append(f"{offsite} of {len(loc['layers'])} CAD layers land elsewhere")

    if warnings:
        res["detail"] += " | WARNING: " + "; ".join(warnings)
    return layer_rows


def learn_systems(reads, candidates, boxes):
    """Coordinate systems of drawings whose own .prj lands on site. Other
    drawings in the same folder are often in the same system but lack a .prj."""
    learned = []
    for res, loc in reads:
        if loc is None or not lands_with_existing_prj(loc, boxes):
            continue
        sr = loc["existing_sr"]
        if not any(same_sr(sr, c) for _, c in candidates + learned):
            learned.append((f"{sr.name} (learned from {res['file']})", sr))
    return learned


def action_items(results, mode, write_to, added=None):
    """(headline, next action, [(issue, solution), ...]), shared by the log and
    the HTML report. The next action is the one thing to do first."""
    n = len(results)
    count = lambda pred: sum(1 for r in results if pred(r))
    applied = count(lambda r: r["status"] == "FIXED" and not r["already"])
    already = count(lambda r: r["already"])
    ready = count(lambda r: r["status"] == "OK") + applied + already
    head = f"{ready} of {n} drawing(s) ready to import."

    fix = count(lambda r: r["status"] == "FIX")
    not_applied = fix if mode == MODE_FIX else 0  # still FIX after a fix run
    nomatch_other = count(lambda r: r["review_kind"] == "nomatch" and not r["likely_local"])
    nomatch_local = count(lambda r: r["review_kind"] == "nomatch" and r["likely_local"])
    amb = count(lambda r: r["review_kind"] == "ambiguous")
    datum = count(lambda r: r["review_kind"] == "datum")
    stray = count(lambda r: r["stray"])
    offsite = count(lambda r: isinstance(r["offsite_layers"], int) and r["offsite_layers"] > 0)
    skip = count(lambda r: r["status"] == "SKIP")
    where = f"the '{OUTPUT_SUBFOLDER}' subfolder" if write_to == WRITE_COPIES else "their original folders"

    # The one thing to do first
    if fix and mode == MODE_CHECK:
        nxt = (f"Run again with 'Check and fix' to write the correct .prj for {fix} drawing(s) "
               f"(on copies in the '{OUTPUT_SUBFOLDER}' subfolder).")
    elif applied or already:
        copies = f"the '{OUTPUT_SUBFOLDER}' subfolder" if already and not applied else where
        lead = ("Fixes were already applied in an earlier run. " if already and not applied else "")
        verb = "from" if copies != "their original folders" else "in"
        nxt = f"{lead}Drag the {applied + already} fixed drawing(s) {verb} {copies} into your map."
    elif nomatch_other:
        nxt = "Add the drawings' coordinate system under 'Extra coordinate systems' and run again."
    elif amb:
        nxt = "Confirm the coordinate system of the drawings that fit more than one."
    elif ready == n:
        nxt = "Drag the drawings into your map."
    else:
        nxt = "See the action items below."

    if added:
        head += f" Adding {added['count']} to your map now (group '{added['group']}')."
        if nxt.startswith(("Drag", "Fixes were already applied")):
            nxt = (("Fixes were already applied in an earlier run. " if already and not applied else "")
                   + "The ready drawings are being added to your map; they appear in the group "
                   f"'{added['group']}' when the tool finishes.")

    items = []
    stale = count(lambda r: "(.prj file checked)" in r["fix"])
    if stale:
        items.append((f"{stale} fixed through the .prj file only (Pro had already read them in this session).",
                      "The .prj files are correct. If those drawings land in the wrong place in this "
                      "session, remove them from the map and restart ArcGIS Pro."))
    if already:
        items.append((f"{already} were already fixed in an earlier run.",
                      f"Nothing to redo: the fixed copies are in the '{OUTPUT_SUBFOLDER}' subfolder. "
                      f"To fix them again, delete those copies and run again."))
    if not_applied:
        items.append((f"{not_applied} could not be fixed or verified.",
                      "See the Fix column for the reason, then fix and run again."))
    if datum:
        items.append((f"{datum} fit two versions of NAD83 on the same grid (about 1 m apart).",
                      "The tool uses the reference's version. If the CAD author used the other "
                      "one, positions may be off by about 1 m; fine for display, check before "
                      "measuring."))
    if nomatch_other:
        items.append((f"{nomatch_other} match no known coordinate system.",
                      "Add their system (for example the project ground system) under 'Extra "
                      "coordinate systems' and run again. The picker can import it from a drawing "
                      "that checks OK or from the project's .prj. Otherwise, ask the CAD author "
                      "which system the drawing uses."))
    if nomatch_local:
        items.append((f"{nomatch_local} appear to be in local or sheet coordinates.",
                      "Ask the CAD author for a version in project coordinates, or georeference "
                      "with control points (not supported by this tool yet)."))
    if amb:
        items.append((f"{amb} fit more than one coordinate system.",
                      "Confirm which one with the CAD author or the project surveyor, then add it "
                      "under 'Extra coordinate systems' and run again."))
    if stray:
        items.append((f"{stray} have stray objects far from the drawing.",
                      "They import fine, but the extent is too large. Ask the CAD author to "
                      "remove the objects, or ignore it."))
    if offsite:
        items.append((f"{offsite} have CAD layers that land elsewhere.",
                      "Check which layers in the per-file details, and leave them out when "
                      "importing."))
    if skip:
        items.append((f"{skip} could not be read.",
                      "Check that the file opens in CAD and is not locked, then run again."))
    return head, nxt, items


# --------------------------------------------------------------------------
# Fix
# --------------------------------------------------------------------------
def copy_path(res, root):
    """Where Copies mode puts the fixed copy of a drawing."""
    return os.path.join(root, OUTPUT_SUBFOLDER, os.path.dirname(res["file"]),
                        os.path.basename(res["path"]))


def mark_existing_fix(res, root):
    """A FIX drawing whose copy in _GIS_ready already has the right .prj was
    fixed in an earlier run: report it as FIXED instead of fixing it again."""
    if res["status"] != "FIX":
        return
    target = copy_path(res, root)
    if not (os.path.exists(target) and os.path.exists(prj_path(target))):
        return
    try:
        got = arcpy.Describe(target).spatialReference
    except Exception:
        return
    if same_sr(got, res["suggested_sr"]):
        res.update(status="FIXED", already=True, output=target,
                   location=os.path.relpath(os.path.dirname(target), root),
                   fix=f"Already fixed in an earlier run (copy in {OUTPUT_SUBFOLDER})")


def write_prj(target, sr):
    """Write the drawing's .prj with Pro's Define Projection tool, the same step
    the GIS team does by hand, so Pro also updates what it remembers about the
    drawing. Falls back to writing the .prj text directly."""
    try:
        arcpy.management.DefineProjection(target, sr)
        if os.path.exists(prj_path(target)):
            return
    except Exception:
        pass
    with open(prj_path(target), "w", encoding="ascii") as f:
        f.write(sr_wkt(sr))


def verify_prj(target, sr):
    """Check the fix: first what Pro reads for the drawing, then the .prj file
    itself (Pro can keep an old answer for a drawing it read earlier in the run)."""
    try:
        if same_sr(arcpy.Describe(target).spatialReference, sr):
            return "Applied and verified"
    except Exception:
        pass
    try:
        if same_sr(arcpy.SpatialReference(prj_path(target)), sr):
            return "Applied and verified (.prj file checked)"
    except Exception as e:
        return f"Applied, could not verify: {e}"
    return "Applied, but the .prj does not read back as intended: check manually"


def next_backup_name(prj):
    """drawing.prj.bak, or .prj.bak2, .bak3... so no backup is ever overwritten."""
    candidate, i = prj + ".bak", 2
    while os.path.exists(candidate):
        candidate, i = f"{prj}.bak{i}", i + 1
    return candidate


def write_change_log(results, root):
    """Append one block per run to _CAD_to_GIS_changes.txt in the CAD folder."""
    changed = [r for r in results if r["fix"].startswith("Applied")]
    if not changed:
        return
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    lines = [f"{datetime.datetime.now():%Y-%m-%d %H:%M} | User: {user} | CAD_to_GIS.pyt v{VERSION}"]
    for r in changed:
        system = r["suggested_sr"].name if r["suggested_sr"] is not None else "?"
        text = ""
        if r["backup"]:
            text += f"Original .prj set to {r['backup']}. "
        if r["output"] and os.path.normcase(r["output"]) != os.path.normcase(r["path"]):
            text += f"Copy written to {os.path.relpath(r['output'], root)}. "
        text += (f'New .prj processed through CAD_to_GIS.pyt on "{system}" '
                 f"coordinate reference for this project.")
        lines.append(f"- {r['file']}: {text}")
    log = os.path.join(root, CHANGE_LOG)
    try:
        new_file = not os.path.exists(log)
        with open(log, "a", encoding="utf-8") as f:
            if new_file:
                f.write("CAD to GIS change log\n=====================\n")
            f.write("\n" + "\n".join(lines) + "\n")
        arcpy.AddMessage(f"Change log updated: {CHANGE_LOG} (in the CAD folder)")
    except Exception as e:
        arcpy.AddWarning(f"Could not write the change log: {e}")


def add_ready_to_map(results, folder_name):
    """Add OK and FIXED drawings to the active map, in one group. Returns
    (number added, group name), or None when nothing could be added."""
    ready = [r for r in results if r["status"] in ("OK", "FIXED")]
    if not ready:
        arcpy.AddMessage("No ready drawings to add to the map.")
        return None
    try:
        m = arcpy.mp.ArcGISProject("CURRENT").activeMap
    except Exception as e:
        arcpy.AddWarning(f"Could not reach the current map: {e}")
        return None
    if m is None:
        arcpy.AddWarning("No map is active (a layout may be open). Click a map view and run again "
                         "to add the drawings.")
        return None
    group_name = f"CAD to GIS - {folder_name}"
    try:
        group = m.createGroupLayer(group_name)
    except Exception:
        group = None  # older Pro: add the drawings without a group
    added = 0
    for r in ready:
        try:
            lyr = m.addDataFromPath(r["output"] or r["path"])
            if group is not None:
                m.addLayerToGroup(group, lyr)
                m.removeLayer(lyr)
            added += 1
        except Exception as e:
            arcpy.AddWarning(f"Could not add {r['file']} to the map: {e}")
    if added:
        where = f"group '{group_name}'" if group is not None else "the map"
        arcpy.AddMessage(f"Added {added} ready drawing(s) to {where} in map '{m.name}'.")
    return (added, group_name if group is not None else m.name) if added else None


def apply_fix(res, root, write_to):
    if res["status"] != "FIX":
        return
    sr, src = res["suggested_sr"], res["path"]

    if write_to == WRITE_ORIGINALS:
        target = src
        old_prj = prj_path(src)
        if os.path.exists(old_prj):
            backup = next_backup_name(old_prj)
            res["backup_path"] = backup
            try:
                os.rename(old_prj, backup)
            except Exception as e:
                res["fix"] = f"Not applied: could not back up the existing .prj ({e})"
                return
            res["backup"] = os.path.basename(backup)
    else:
        out_dir = os.path.join(root, OUTPUT_SUBFOLDER, os.path.dirname(res["file"]))
        target = os.path.join(out_dir, os.path.basename(src))
        if os.path.exists(target) or os.path.exists(prj_path(target)):
            res["fix"] = (f"Not applied: a copy with a different coordinate system already exists "
                          f"in {OUTPUT_SUBFOLDER}. Delete it to redo.")
            return
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy2(src, target)  # the .prj is not copied; a new one is written
        res["location"] = os.path.relpath(out_dir, root)

    try:
        write_prj(target, sr)
    except Exception as e:
        res["fix"] = f"Not applied: could not write the .prj ({e})"
        if res.get("backup_path"):  # put the original .prj back
            try:
                os.rename(res["backup_path"], prj_path(target))
                res["backup"] = ""
            except Exception:
                pass
        return

    res["fix"] = verify_prj(target, sr)
    res["output"] = target
    if res["fix"].startswith("Applied and verified"):
        res["status"] = "FIXED"


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def report_folder():
    try:
        return arcpy.mp.ArcGISProject("CURRENT").homeFolder
    except Exception:
        return arcpy.env.scratchFolder


def open_in_browser(path):
    try:
        os.startfile(path)  # Windows: opens in the default browser
    except Exception:
        try:
            import webbrowser
            webbrowser.open("file:///" + path.replace("\\", "/"))
        except Exception as e:
            arcpy.AddWarning(f"Could not open the report automatically: {e}")


# --------------------------------------------------------------------------
# HTML report (single self-contained file, no internet needed)
# --------------------------------------------------------------------------
# Status colors are always paired with an icon and a text label.
STATUS_STYLE = {
    "OK":     ("#0ca30c", "&#10003;", "OK"),
    "FIXED":  ("#0ca30c", "&#10003;", "FIXED"),
    "FIX":    ("#fab219", "!", "FIX"),
    "REVIEW": ("#d03b3b", "?", "REVIEW"),
    "SKIP":   ("#898781", "&ndash;", "SKIP"),
}

REPORT_CSS = """
:root { --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
        --muted:#898781; --rule:#e4e3df; }
@media (prefers-color-scheme: dark) {
  :root { --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7;
          --muted:#898781; --rule:#2e2e2c; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--ink);
       font:14px/1.45 "Segoe UI", system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:15px; margin:28px 0 8px; }
.sub { color:var(--ink2); margin:0 0 20px; }
.tiles { display:flex; flex-wrap:wrap; gap:12px; }
.tile { background:var(--surface); border:1px solid var(--rule); border-radius:8px;
        padding:12px 16px; min-width:110px; }
.tile .n { font-size:26px; font-weight:600; }
.tile .l { color:var(--ink2); }
.tile.zero .n { color:var(--muted); }
.card { background:var(--surface); border:1px solid var(--rule); border-radius:8px;
        padding:12px 16px; }
.kv { display:grid; grid-template-columns:110px 1fr; gap:4px 12px; }
.kv dt { color:var(--ink2); } .kv dd { margin:0; }
.wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; background:var(--surface);
        border:1px solid var(--rule); border-radius:8px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--rule);
         vertical-align:top; }
th { color:var(--ink2); font-weight:600; font-size:13px; }
tr:last-child td { border-bottom:0; }
.badge { display:inline-flex; align-items:center; gap:6px; font-weight:600; white-space:nowrap; }
.dot { width:18px; height:18px; border-radius:50%; color:#fff; font-size:12px;
       display:inline-flex; align-items:center; justify-content:center; }
.warn { color:var(--ink); }
.warn::before { content:"\\26A0  "; }
details { background:var(--surface); border:1px solid var(--rule); border-radius:8px;
          padding:8px 12px; margin:8px 0; }
summary { cursor:pointer; font-weight:600; }
.muted { color:var(--muted); }
.next { margin:8px 0 0; padding:8px 12px; border-left:4px solid #2a78d6;
        background:rgba(42,120,214,0.08); border-radius:4px; }
.actions { margin:12px 0 0; padding-left:20px; }
.actions li { margin:8px 0; }
.sol { color:var(--ink2); }
footer { margin-top:28px; color:var(--ink2); font-size:13px; }
code { font-family:Consolas, monospace; font-size:12.5px; }
"""


def _e(v):
    return html.escape("" if v is None else str(v))


def _badge(status):
    color, icon, label = STATUS_STYLE.get(status, ("#898781", "", status))
    dot_ink = "#0b0b0b" if status == "FIX" else "#ffffff"  # dark icon on yellow
    return (f'<span class="badge"><span class="dot" style="background:{color};color:{dot_ink}">'
            f'{icon}</span>{_e(label)}</span>')


def write_html_report(results, layer_rows, ctx):
    stamp = datetime.datetime.now()
    path = os.path.join(report_folder(), f"CAD_to_GIS_report_{stamp:%Y%m%d_%H%M%S}.html")
    counts = {s: sum(1 for r in results if r["status"] == s) for s in STATUS_STYLE}
    main_sr, params_sr = describe_sr(ctx["ref_sr"])
    by_file = {}
    for row in layer_rows:
        by_file.setdefault(row["file"], []).append(row)

    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f'<title>CAD to GIS report</title><style>{REPORT_CSS}</style></head><body><main>',
         '<h1>CAD to GIS report</h1>',
         f'<p class="sub">{_e(ctx["mode"])} &middot; folder <b>{_e(ctx["cad_folder"])}</b> &middot; '
         f'{stamp:%Y-%m-%d %H:%M} &middot; {ctx["seconds"]:.1f} s &middot; v{VERSION}</p>']

    # Count tiles
    h.append('<div class="tiles">')
    h.append(f'<div class="tile"><div class="n">{len(results)}</div><div class="l">Files checked</div></div>')
    shown = (("OK", "FIXED", "FIX", "REVIEW", "SKIP") if ctx["mode"] == MODE_FIX or counts["FIXED"]
             else ("OK", "FIX", "REVIEW", "SKIP"))
    for s in shown:
        zero = " zero" if counts[s] == 0 else ""
        h.append(f'<div class="tile{zero}"><div class="n">{counts[s]}</div>'
                 f'<div class="l">{_badge(s)}</div></div>')
    h.append('</div>')

    # Next steps: headline plus action items
    head, nxt, items = action_items(results, ctx["mode"], ctx["write_to"], ctx.get("added"))
    changed = " Nothing was changed (Check only)." if ctx["mode"] == MODE_CHECK else ""
    h.append(f'<h2>Next steps</h2><div class="card"><b>{_e(head)}</b>{_e(changed)}'
             f'<p class="next"><b>Next:</b> {_e(nxt)}</p>')
    if items:
        h.append('<ul class="actions">' + "".join(
            f'<li>{_e(issue)}<br><span class="sol"><b>Solution:</b> {_e(sol)}</span></li>'
            for issue, sol in items) + '</ul>')
    h.append('</div>')

    # Results table
    h.append('<h2>Results</h2><div class="wrap"><table><thead><tr>'
             '<th>Status</th><th>File</th><th>Path</th><th>.prj vs reference</th><th>What was found</th>'
             '<th>CAD layers</th><th>Fix</th></tr></thead><tbody>')
    for r in results:
        detail, _, warning = r["detail"].partition(" | WARNING: ")
        cell = _e(detail)
        if r["diagnosis"]:
            cell += f'<br><span class="muted">{_e(r["diagnosis"])}</span>'
        if warning:
            cell += f'<br><span class="warn">{_e(warning)}</span>'
        if r["cad_layers"] == "":
            layers = '<span class="muted">&ndash;</span>'
        elif r["offsite_layers"]:
            layers = f'{r["cad_layers"]}<br><span class="warn">{r["offsite_layers"]} elsewhere</span>'
        else:
            layers = f'{r["cad_layers"]}, all on site'
        h.append(f'<tr><td>{_badge(r["status"])}</td><td>{_e(os.path.basename(r["file"]))}</td>'
                 f'<td>{_e(r["location"])}</td>'
                 f'<td>{_e(r["crs_check"] or "n/a")}</td><td>{cell}</td>'
                 f'<td>{layers}</td><td>{_e(r["fix"]) or "<span class=muted>&ndash;</span>"}</td></tr>')
    h.append('</tbody></table></div>')

    # Per-file details (collapsed)
    h.append('<h2>Details per file</h2>')
    for r in results:
        h.append(f'<details><summary>{_e(r["file"])}</summary><dl class="kv">'
                 f'<dt>Existing .prj</dt><dd>{_e(r["existing_prj"])}</dd>'
                 f'<dt>vs reference</dt><dd>{_e(r["crs_vs_reference"])}</dd>'
                 f'<dt>Suggested</dt><dd>{_e(r["suggested"]) or "&ndash;"}</dd>'
                 f'<dt>Features read</dt><dd>{_e(r["features"])}</dd>'
                 + (f'<dt>Drawing centre</dt><dd>{_e(r["center"])}</dd>' if r["center"] else "")
                 + (f'<dt>Diagnosis</dt><dd>{_e(r["diagnosis"])}</dd>' if r["diagnosis"] else "")
                 + (f'<dt>Output</dt><dd>{_e(os.path.basename(r["output"]))}</dd>' if r["output"] else "")
                 + '</dl>')
        rows = by_file.get(r["file"], [])
        if rows:
            h.append('<div class="wrap"><table><thead><tr><th>CAD layer</th><th>Features</th>'
                     '<th>Lands on site</th></tr></thead><tbody>')
            for lr in rows:
                on = "yes" if lr["lands_on_site"] else '<span class="warn">no</span>'
                h.append(f'<tr><td>{_e(lr["cad_layer"])}</td><td>{lr["features"]}</td><td>{on}</td></tr>')
            h.append('</tbody></table></div>')
        h.append('</details>')

    # Reference
    h.append('<h2>Reference</h2><div class="card"><dl class="kv">'
             f'<dt>Layer</dt><dd>{_e(ctx["ref_name"])} ({ctx["n_ref"]} feature(s))</dd>'
             f'<dt>System</dt><dd>{_e(ctx["ref_sr"].name)}<br><span class="muted">{_e(main_sr)}'
             f'<br>{_e(params_sr)}</span></dd>'
             '<dt>Systems tried</dt><dd>' + "<br>".join(_e(l) for l, _ in ctx["candidates"])
             + '</dd></dl></div>')

    h.append('<footer>Coordinate systems are assigned by matching each drawing\'s location '
             'against the reference layer. This finds wrong systems, shifts, units and zones, '
             'but not differences between NAD83 versions (about 1 m) or US vs international feet. '
             'Confirm the project coordinate system with the project surveyor before using results '
             f'for design. &middot; CAD to GIS v{VERSION}</footer>')
    h.append('</main></body></html>')

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))
    return path


LINE_HEAVY = "=" * 60
LINE_LIGHT = "-" * 60


def print_header(mode, cad_folder, files, ref_layer, n_ref, ref_sr, candidates):
    """Run header. Shows names only, not full paths."""
    main, params = describe_sr(ref_sr)
    by_ext = {}
    for f in files:
        e = os.path.splitext(f)[1].lower()
        by_ext[e] = by_ext.get(e, 0) + 1
    n_dirs = len({os.path.dirname(f) for f in files})
    folder_name = os.path.basename(cad_folder.rstrip("\\/"))
    arcpy.AddMessage(LINE_HEAVY)
    arcpy.AddMessage(f" CAD to GIS v{VERSION}  |  {mode}")
    arcpy.AddMessage(LINE_HEAVY)
    arcpy.AddMessage("CAD folder")
    arcpy.AddMessage(f"  {folder_name}: {len(files)} file(s) ("
                     + ", ".join(f"{n} {e}" for e, n in sorted(by_ext.items()))
                     + f") in {n_dirs} folder(s)")
    arcpy.AddMessage("Reference")
    arcpy.AddMessage(f"  Layer:   {os.path.basename(ref_layer)}  ({n_ref} feature(s))")
    arcpy.AddMessage(f"  System:  {ref_sr.name}")
    arcpy.AddMessage(f"           {main}")
    if params:
        arcpy.AddMessage(f"           {params}")
    arcpy.AddMessage(f"Coordinate systems tried ({len(candidates)})")
    for label, _ in candidates:
        arcpy.AddMessage(f"  - {label}")


def print_summary(results, mode, write_to, report_path, seconds, added=None):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    tally = "  |  ".join(f"{counts[s]} {s}" for s in ("OK", "FIXED", "FIX", "REVIEW", "SKIP") if counts.get(s))
    arcpy.AddMessage(LINE_LIGHT)
    arcpy.AddMessage(f"Results: {len(results)} file(s)  |  {tally}")
    arcpy.AddMessage(LINE_LIGHT)

    for r in results:
        detail, _, warning = r["detail"].partition(" | WARNING: ")
        lines = [f"[{r['status']}]  {r['file']}",
                 f"      .prj vs reference: {r['crs_check'] or 'n/a'}",
                 f"      {detail}"]
        if r["diagnosis"]:
            lines.append(f"      {r['diagnosis']}")
        if r["cad_layers"] != "":
            off = r["offsite_layers"]
            lines.append(f"      CAD layers: {r['cad_layers']}, "
                         + ("all on site" if not off else f"{off} land elsewhere"))
        if warning:
            lines.append(f"      WARNING: {warning}")
        if r["fix"]:
            lines.append(f"      Fix: {r['fix']}")
        text = "\n".join(lines)
        if r["status"] in ("OK", "FIXED") and not warning:
            arcpy.AddMessage(text)
        else:
            arcpy.AddWarning(text)
        arcpy.AddMessage("")

    arcpy.AddMessage(LINE_LIGHT)
    head, nxt, items = action_items(results, mode, write_to, added)
    arcpy.AddMessage(head + ("  Nothing was changed (Check only)." if mode == MODE_CHECK else ""))
    arcpy.AddMessage(f"NEXT: {nxt}")
    if items:
        arcpy.AddMessage("")
        arcpy.AddMessage("Action items:")
        for issue, solution in items:
            arcpy.AddMessage(f"  - {issue}")
            arcpy.AddMessage(f"      Solution: {solution}")
    arcpy.AddMessage("")
    arcpy.AddMessage(f"Report:  {os.path.basename(report_path)}  (in the Pro project folder)")
    arcpy.AddMessage("         Click the link above, under 'Report', to open it again.")
    arcpy.AddMessage(f"Time:    {seconds:.1f} s")
    arcpy.AddMessage(LINE_HEAVY)
