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
