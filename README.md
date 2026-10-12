# CAD to GIS
 
An ArcGIS Pro tool that checks a folder of CAD drawings against a project's reference layer, for example borings. It reports which drawings are in the wrong or an unknown coordinate system and, if you ask it to, fixes them so they land in the right place on the map.
 
> **Status:** Pilot, v0.1 (design). Not yet implemented. This README describes the intended workflow and records changes as it gets built.
>
> **Maintainer:** Daniel Acosta Reyes

---

## Purpose
 
Bringing CAD drawings into ArcGIS Pro often puts them in the wrong place. The usual cause is that a drawing has no coordinate system assigned, or the wrong one. Fixing it by hand ("assign coordinate system" → "validate position") takes about **20 minutes per file**, and project folders can hold dozens of drawings whose coordinate system nobody knows.

## Workflow
 
1. Open the ArcGIS Pro project that contains the reference layer (for example, borings).
2. Open **CAD to GIS** from the toolbox and fill in the form (see [Parameters](#parameters)).
3. Run it in **Check only** mode. Nothing is changed. You get a short report.
4. Read the report: which files are OK, which need a fix, which need a person to look.
5. To apply the fixes, run it again in **Check and fix** mode. That second run is the confirmation step.
6. The tool runs in the background, so you can keep working in Pro.
7. When it finishes, it checks every fixed file against the reference layer and reports where the ready files are. Drag the ones you need into your map.

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