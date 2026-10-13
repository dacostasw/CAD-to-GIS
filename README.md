# CAD to GIS
 
An ArcGIS Pro tool that checks a folder of CAD drawings against a project's reference layer, for example borings. It reports which drawings are in the wrong or an unknown coordinate system and, if you ask it to, fixes them so they land in the right place on the map.
 
> **Status:** Pilot, v0.1.10 - In testing in ArcGIS Pro

> **Maintainer:** Daniel Acosta Reyes

---

## Purpose
 
Bringing CAD drawings into ArcGIS Pro often puts them in the wrong place. The usual cause is that a drawing has no coordinate system assigned, or the wrong one. Fixing it by hand ("assign coordinate system" → "validate position") takes about **20 minutes per file**, and project folders can hold dozens of drawings whose coordinate system are unknown. This tool  This tool works out which `.prj` each drawing `.dwg` needs by checking where it lands relative to known project dataa, a process 30x times faster than by hand.

## Workflow

1. Open the ArcGIS Pro project that contains the reference layer (for example, borings).
2. Open **CAD to GIS → Check CAD Coordinates** and fill in the form (see [Parameters](#parameters)).
3. Run it in **Check only** mode. Nothing is changed. You get a short report.
4. Read the report: which files are OK, which need a fix, which need a person to look.
5. To apply the fixes, run it again in **Check and fix** mode. That second run is the confirmation step.
6. The tool runs in the background, so you can keep working in Pro.
7. When it finishes, it confirms that Pro reads each fixed file in the intended coordinate system and says where the ready files are. Tick **Add ready drawings to the current map** to have them loaded for you, or drag in the ones you need.

---
 
## Requirements
 
- ArcGIS Pro 3.7.0
- Pro's default Python environment (`arcgispro-py3`).
- Read access to the CAD folder. If you write fixes to the originals, you also need write access there.

## Install
 
1. Download `CAD_to_GIS.pyt` from this repository.
2. In ArcGIS Pro: **Catalog** pane → right-click **Toolboxes** → **Add Toolbox** → select `CAD_to_GIS.pyt`.
3. The tool appears under the toolbox. You only need to do this once per project, or add it to your favorites to use it everywhere.

---

## Parameters
 
| Parameter | Description | Default |
|---|---|---|
| CAD folder | Folder holding the drawings. Subfolders are included, except `_GIS_ready`. After you pick a folder, the form shows how many CAD files it found (for example "Found 8 CAD file(s): 8 .dwg, in 2 folder(s)"), and blocks the run if there are none. The note appears with Pro's yellow warning icon; it's information, not a problem. | (none, required) |
| Reference layer | A layer in the current map that is known to be in the right place, such as borings. If features are selected, only the selected ones are used. | (none, required) |
| Mode | `Check only` or `Check and fix` | `Check only` |
| Write fixes to | `Copies in _GIS_ready subfolder` or `Originals`. Only active in fix mode. | Copies |
| Check each CAD layer | Also checks each CAD layer separately. This finds parts of a combined drawing that land somewhere else. | On |
| File types | `.dwg`, `.dxf`, `.dgn` | All three |
| Open the report in my browser when done | Opens the HTML report automatically at the end of the run | On |
| Also save the results as CSV | Saves CSV copies of the results next to the HTML report, for Excel | Off |
| Extra coordinate systems | Other systems to try, such as the project ground system when the reference layer is in State Plane. Pick them in Pro's coordinate system picker, which can also import one from a layer or a `.prj`. | None |
 
---

## Report

Each run produces:

- **An HTML report** (`CAD_to_GIS_report_<timestamp>.html`), saved in the **Pro project's home folder** and opened in your browser automatically. It's a single file that works offline and can be emailed. It contains:
  - count tiles (OK / FIX / REVIEW / SKIP),
  - **next steps**: how many drawings are ready to import, the **next action** to take first, and action items, each with a **Solution**,
  - a results table, one row per drawing,
  - collapsible details per file: the `.prj` comparison and a table of CAD layers showing which land on site,
  - the reference layer's coordinate system.

  To reopen it, click the link under *Report* in the geoprocessing history.
- **A short log** in the geoprocessing messages (below).

Nothing is written to the CAD folder in Check only mode.

The Results table has a **Path** column showing where each drawing is: `(top folder)`, the subfolder name, or `_GIS_ready\<subfolder>` for copies.

---

## Safety rules

- **Check only is the default.** Nothing changes unless you run it again in Check and fix mode.
- **Check only writes nothing to the CAD folder.** Reports go to the Pro project's home folder.
- **Drawings are never opened for writing or edited.** Fixes are `.prj` side files only, written with Pro's own **Define Projection** tool (the same step the GIS team does by hand). The `.dwg` content, metadata and modified date stay the same.
- **No `.prj` is ever lost.** A wrong `.prj` is renamed to `.prj.bak` (or `.bak2`, `.bak3`…) before the new one is written. If writing fails, the original is put back.
- **Every change is recorded** in `_CAD_to_GIS_changes.txt` in the CAD folder.
- **An existing copy in `_GIS_ready` is never replaced** (Copies mode).
- **Every fix is checked afterwards:** the tool confirms Pro reads the intended coordinate system.

## Limitations

- **Extent matching finds coarse errors:** the wrong system, a false-origin shift, the wrong units, the wrong zone. It **cannot** tell apart:
  - different versions of NAD83 (around 1 m),
  - US survey feet vs international feet (around 2 ft at State Plane coordinate sizes),
  - ground vs grid coordinates on a project whose ground system has no false shift.

  Those need the project's survey basis or control points.
- **Drawings in arbitrary local coordinates** come out as `REVIEW`. They need control points, which are not supported in v0.1.
- **External references (xrefs):** only the main drawing is copied to `_GIS_ready`.
- **The project's ground coordinate system definition should be confirmed by the project surveyor** before the results are used for design.

---

## Repository layout

```
CAD_to_GIS/
├── README.md          this file
├── CAD_to_GIS.pyt     the ArcGIS Pro toolbox (single file)
├── docs/              screenshots and how-to notes
└── examples/          sample reports (no project data)