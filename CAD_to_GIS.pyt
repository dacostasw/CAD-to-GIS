# -*- coding: utf-8 -*-
"""
CAD to GIS (v0.1) - ArcGIS Pro Python toolbox.

Checks the CAD drawings in a folder against a reference layer that is known to
be in the right place (for example, borings). Reports which coordinate system
each drawing is in. In "Check and fix" mode, writes the matching .prj so the
drawing lands in the right place.

Drawings are never edited. Only .prj side files are written. See README.md.
Runs in ArcGIS Pro's default Python environment (arcpy + standard library).
"""
import csv
import datetime
import os
import shutil
import statistics
import time

import arcpy

VERSION = "0.1"
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
WRITE_ORIGINALS = "Originals"


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
        write_to.filter.list = [WRITE_COPIES, WRITE_ORIGINALS]
        write_to.value = WRITE_COPIES
        write_to.enabled = False

        layers = arcpy.Parameter(
            displayName="Check each CAD layer (finds parts of combined drawings that land elsewhere)",
            name="check_layers", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        layers.value = True

        types = arcpy.Parameter(
            displayName="File types", name="file_types", datatype="GPString",
            parameterType="Optional", direction="Input", multiValue=True)
        types.filter.type = "ValueList"
        types.filter.list = CAD_EXTENSIONS
        types.values = CAD_EXTENSIONS

        report = arcpy.Parameter(
            displayName="Report", name="report", datatype="DEFile",
            parameterType="Derived", direction="Output")

        return [folder, ref, mode, write_to, layers, types, report]

    def updateParameters(self, parameters):
        parameters[3].enabled = parameters[2].valueAsText == MODE_FIX
        return

    def updateMessages(self, parameters):
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
        candidates = build_candidates(ref_sr)
        boxes = BoxCache(ref_layer)
        if boxes.get(ref_sr) is None:
            arcpy.AddError("Could not read the reference layer. It needs at least "
                           "two features with geometry, spread apart.")
            return

        files = find_cad_files(cad_folder, exts)
        if not files:
            arcpy.AddWarning(f"No {', '.join(exts)} files found in {cad_folder}.")
            return

        arcpy.SetProgressor("step", "Checking CAD files...", 0, len(files), 1)
        results, layer_rows = [], []
        for path in files:
            arcpy.SetProgressorLabel(f"Checking {os.path.basename(path)}")
            res, rows = check_file(path, cad_folder, candidates, boxes, check_layers)
            results.append(res)
            layer_rows.extend(rows)
            arcpy.SetProgressorPosition()

        if mode == MODE_FIX:
            arcpy.SetProgressorLabel("Applying fixes...")
            for res in results:
                apply_fix(res, cad_folder, write_to)

        report_path = write_reports(results, layer_rows, check_layers)
        print_summary(results, mode, write_to, ref_layer, ref_sr,
                      report_path, time.time() - t0)
        parameters[6].value = report_path
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


def build_candidates(ref_sr):
    cands = [(f"Project system ({ref_sr.name})", ref_sr)]
    for wkid in CANDIDATE_WKIDS:
        try:
            sr = arcpy.SpatialReference(wkid)
        except Exception:
            arcpy.AddWarning(f"Skipping candidate EPSG {wkid}: not recognized by arcpy.")
            continue
        if not same_sr(sr, ref_sr):
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
        return self._boxes[key]


def reference_box(ref_layer, sr):
    """Box around the reference features in `sr`, with a margin. None on failure."""
    xs, ys = [], []
    try:
        with arcpy.da.SearchCursor(ref_layer, ["SHAPE@XY"], spatial_reference=sr) as cur:
            for row in cur:
                xy = row[0]
                if xy is None or xy[0] is None:
                    continue
                xs.append(xy[0])
                ys.append(xy[1])
    except Exception as e:
        arcpy.AddWarning(f"Could not convert reference layer to {sr.name}: {e}")
        return None
    if len(xs) < 2:
        return None
    margin = max(max(xs) - min(xs), max(ys) - min(ys)) * MARGIN_FRACTION
    if margin == 0:
        return None
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


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
# Check one file
# --------------------------------------------------------------------------
def check_file(path, root, candidates, boxes, check_layers):
    res = {"file": os.path.relpath(path, root), "path": path, "status": "",
           "detail": "", "existing_prj": "", "suggested": "",
           "suggested_sr": None, "features": 0, "cad_layers": "",
           "offsite_layers": "", "fix": "", "output": ""}
    try:
        existing_sr = arcpy.Describe(path).spatialReference
        has_prj = os.path.exists(prj_path(path))
        by_layer = read_cad_points(path, check_layers)
    except Exception as e:
        res.update(status="SKIP", detail=f"Could not read file: {e}")
        return res, []

    all_pts = [p for pts in by_layer.values() for p in pts]
    if not all_pts:
        res.update(status="SKIP", detail="No features with geometry")
        return res, []
    res["features"] = len(all_pts)

    # The median position is not thrown off by stray objects far from the site
    x = statistics.median(p[0] for p in all_pts)
    y = statistics.median(p[1] for p in all_pts)

    warnings = []
    xs = sorted(p[0] for p in all_pts)
    core = percentile(xs, 0.95) - percentile(xs, 0.05)
    if core > 0 and (xs[-1] - xs[0]) > STRAY_RATIO * core:
        warnings.append("stray objects far from the drawing")

    existing_known = has_prj and existing_sr.name != "Unknown"
    if existing_known:
        res["existing_prj"] = existing_sr.name
    else:
        res["existing_prj"] = "unreadable .prj" if has_prj else "none"

    chosen = None
    if existing_known and inside(boxes.get(existing_sr), x, y):
        res.update(status="OK", detail="Existing .prj lands on the reference layer")
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
            if has_prj:
                res["detail"] = f"Existing .prj ({res['existing_prj']}) lands off-site; fits {label}"
            else:
                res["detail"] = f"No .prj; fits {label}"
        elif len(fits) > 1:
            res.update(status="REVIEW",
                       detail="Fits more than one system: " + "; ".join(l for l, _ in fits))
        else:
            res.update(status="REVIEW",
                       detail="Matches no known system (local coordinates? needs control points)")

    layer_rows = []
    if check_layers and chosen is not None:
        box = boxes.get(chosen)
        offsite = 0
        for layer, pts in sorted(by_layer.items()):
            lx = statistics.median(p[0] for p in pts)
            ly = statistics.median(p[1] for p in pts)
            ok = inside(box, lx, ly)
            offsite += 0 if ok else 1
            layer_rows.append({"file": res["file"], "cad_layer": layer,
                               "features": len(pts), "lands_on_site": ok})
        res["cad_layers"] = len(by_layer)
        res["offsite_layers"] = offsite
        if offsite:
            warnings.append(f"{offsite} of {len(by_layer)} CAD layers land elsewhere")

    if warnings:
        res["detail"] += " | WARNING: " + "; ".join(warnings)
    return res, layer_rows


# --------------------------------------------------------------------------
# Fix
# --------------------------------------------------------------------------
def apply_fix(res, root, write_to):
    if res["status"] != "FIX":
        return
    sr, src = res["suggested_sr"], res["path"]

    if write_to == WRITE_ORIGINALS:
        if os.path.exists(prj_path(src)):
            res["fix"] = "Not applied: original already has a .prj (never overwritten). Use Copies mode."
            return
        target = src
    else:
        out_dir = os.path.join(root, OUTPUT_SUBFOLDER, os.path.dirname(res["file"]))
        target = os.path.join(out_dir, os.path.basename(src))
        if os.path.exists(target) or os.path.exists(prj_path(target)):
            res["fix"] = f"Not applied: a copy already exists in {OUTPUT_SUBFOLDER}. Delete it to redo."
            return
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy2(src, target)  # the .prj is not copied; a new one is written

    with open(prj_path(target), "w", encoding="ascii") as f:
        f.write(sr_wkt(sr))

    # Confirm Pro now reads the intended coordinate system
    try:
        arcpy.management.ClearWorkspaceCache()
        got = arcpy.Describe(target).spatialReference
        res["fix"] = ("Applied and verified" if same_sr(got, sr)
                      else f"Applied, but Pro reads '{got.name}': check manually")
    except Exception as e:
        res["fix"] = f"Applied, could not verify: {e}"
    res["output"] = target


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def report_folder():
    try:
        return arcpy.mp.ArcGISProject("CURRENT").homeFolder
    except Exception:
        return arcpy.env.scratchFolder


def write_reports(results, layer_rows, check_layers):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = report_folder()
    report = os.path.join(folder, f"CAD_to_GIS_report_{stamp}.csv")
    cols = ["file", "status", "detail", "existing_prj", "suggested",
            "features", "cad_layers", "offsite_layers", "fix", "output"]
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    if check_layers and layer_rows:
        layers_csv = os.path.join(folder, f"CAD_to_GIS_layers_{stamp}.csv")
        with open(layers_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["file", "cad_layer", "features", "lands_on_site"])
            w.writeheader()
            w.writerows(layer_rows)
    return report


def print_summary(results, mode, write_to, ref_layer, ref_sr, report_path, seconds):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    arcpy.AddMessage(f"CAD to GIS v{VERSION} | {mode} | {len(results)} files | "
                     f"reference: {ref_layer} ({ref_sr.name})")
    for status in ("OK", "FIX", "REVIEW", "SKIP"):
        if counts.get(status):
            arcpy.AddMessage(f"  {counts[status]:>3}  {status}")
    arcpy.AddMessage("")
    for r in results:
        line = f"  {r['status']:<7}{r['file']}  {r['detail']}"
        if r["fix"]:
            line += f"  -> {r['fix']}"
        if r["status"] == "OK" and "WARNING" not in r["detail"]:
            arcpy.AddMessage(line)
        else:
            arcpy.AddWarning(line)
    arcpy.AddMessage("")
    if mode == MODE_CHECK:
        arcpy.AddMessage("Check only: nothing was changed. Run again with "
                         "'Check and fix' to apply the FIX items.")
    else:
        applied = sum(1 for r in results if r["fix"].startswith("Applied"))
        where = (os.path.join("<CAD folder>", OUTPUT_SUBFOLDER)
                 if write_to == WRITE_COPIES else "next to the originals")
        arcpy.AddMessage(f"Fixed {applied} file(s), {where}. Drag them into your map.")
    arcpy.AddMessage(f"Details: {report_path}")
    arcpy.AddMessage(f"Elapsed: {seconds:.1f} s")
