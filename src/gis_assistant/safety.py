"""Safety validation: algorithm allowlists, path confinement, plan limits."""

from __future__ import annotations

import os
from pathlib import Path

# -- Plan Limits -------------------------------------------------------------

MAX_PLAN_STEPS = 20
MAX_SCAN_FILES = 200
MAX_FEATURE_LIMIT = 100

# -- Algorithm Allowlist -----------------------------------------------------

ALLOWED_ALGORITHMS: set[str] = {
    # -- native: vector geometry --
    "native:buffer",
    "native:multiringconstantbuffer",
    "native:singlesidedbuffer",
    "native:offsetline",
    "native:simplifygeometries",
    "native:smoothgeometry",
    "native:centroids",
    "native:convexhull",
    "native:minimumboundinggeometry",
    "native:boundingboxes",
    "native:densifygeometries",
    "native:densifygeometriesgivenaninterval",
    "native:pointsalonggeometry",
    "native:reverselinedirection",
    "native:snappointstogrid",
    "native:swapxy",
    "native:transect",
    "native:polygonstolines",
    "native:linestopolygons",
    "native:multiparttosingleparts",
    "native:promotetomulti",
    "native:collect",
    "native:fixgeometries",
    # -- native: vector overlay --
    "native:clip",
    "native:intersection",
    "native:union",
    "native:difference",
    "native:symmetricaldifference",
    # -- native: vector general --
    "native:dissolve",
    "native:mergevectorlayers",
    "native:reprojectlayer",
    "native:createspatialindex",
    "native:joinattributesbylocation",
    "native:joinattributestable",
    "native:extractbyattribute",
    "native:extractbyexpression",
    "native:extractbylocation",
    "native:selectbyattribute",
    "native:selectbyexpression",
    "native:selectbylocation",
    "native:saveselectedfeatures",
    "native:fieldcalculator",
    "native:deletecolumn",
    "native:renametablefield",
    "native:addfieldtoattributestable",
    "native:retainfields",
    "native:addautoincrementalfield",
    "native:adduniquevalueindexfield",
    "native:countpointsinpolygon",
    "native:sumlinelengths",
    # -- native: vector analysis --
    "native:nearestneighbouranalysis",
    "native:meancoordinates",
    "native:basicstatisticsforfields",
    "native:listuniquevalues",
    "native:statisticsbycategories",
    # -- native: vector creation --
    "native:createpointslayerfromtable",
    "native:creategrid",
    "native:randompointsextent",
    "native:randompointsinpolygons",
    # -- native: raster --
    "native:rasterlayerstatistics",
    "native:rasterlayeruniquevaluesreport",
    "native:rasterize",
    # -- gdal: vector --
    "gdal:buffervectors",
    "gdal:clipvectorbyextent",
    "gdal:clipvectorbypolygon",
    "gdal:dissolve",
    "gdal:offsetcurve",
    "gdal:onesidebuffer",
    "gdal:pointsalonglines",
    # -- gdal: raster analysis --
    "gdal:aspect",
    "gdal:contour",
    "gdal:contour_polygon",
    "gdal:fillnodata",
    "gdal:hillshade",
    "gdal:roughness",
    "gdal:slope",
    "gdal:tpitopographicpositionindex",
    "gdal:triterrainruggednessindex",
    # -- gdal: raster conversion --
    "gdal:cliprasterbyextent",
    "gdal:cliprasterbymasklayer",
    "gdal:merge",
    "gdal:translate",
    "gdal:warpreproject",
    "gdal:rasterize",
    "gdal:polygonize",
    "gdal:buildvirtualraster",
    "gdal:gdal2tiles",
    # -- gdal: raster projections --
    "gdal:assignprojection",
    "gdal:extractprojection",
}

# Common near-miss mappings
ALGORITHM_SYNONYMS: dict[str, str] = {
    "native:reproject": "native:reprojectlayer",
    "native:save": "native:saveselectedfeatures",
    "native:statistics": "native:basicstatisticsforfields",
    "native:unique": "native:listuniquevalues",
    "native:fixgeometry": "native:fixgeometries",
    "native:centroid": "native:centroids",
    "native:simplify": "native:simplifygeometries",
    "native:smooth": "native:smoothgeometry",
    "native:multipart": "native:multiparttosingleparts",
    "gdal:warp": "gdal:warpreproject",
    "gdal:contours": "gdal:contour",
    "gdal:dem": "gdal:hillshade",
    "gdal:clipraster": "gdal:cliprasterbyextent",
    "gdal:clipvector": "gdal:clipvectorbyextent",
}


def is_algorithm_allowed(algorithm: str) -> bool:
    """Check if an algorithm is in the allowlist."""
    return algorithm in ALLOWED_ALGORITHMS


def resolve_algorithm(algorithm: str) -> str:
    """Resolve an algorithm ID, trying synonyms if needed.

    Returns the canonical algorithm ID or raises ValueError.
    """
    if algorithm in ALLOWED_ALGORITHMS:
        return algorithm
    canonical = ALGORITHM_SYNONYMS.get(algorithm)
    if canonical and canonical in ALLOWED_ALGORITHMS:
        return canonical
    raise ValueError(
        f"Algorithm {algorithm!r} is not in the allowlist. "
        f"Use native: or gdal: prefixed algorithm IDs."
    )


# -- Path Validation ---------------------------------------------------------

def validate_output_path(path: str, workspace_dir: str) -> str:
    """Ensure an output path is within the workspace directory.

    Expands ~, resolves symlinks, normalizes .., checks containment.
    Returns the resolved absolute path or raises ValueError.
    """
    resolved = Path(os.path.expanduser(path)).resolve()
    workspace = Path(os.path.expanduser(workspace_dir)).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise ValueError(
            f"Output path {path!r} resolves to {resolved} which is "
            f"outside the workspace {workspace}"
        )
    return str(resolved)


def validate_input_path_exists(path: str) -> str:
    """Ensure an input path exists on disk. Returns resolved path."""
    resolved = Path(os.path.expanduser(path)).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Input path does not exist: {resolved}")
    return str(resolved)


def check_overwrite(path: str) -> bool:
    """Return True if the path already exists (potential overwrite)."""
    return Path(os.path.expanduser(path)).resolve().exists()


# -- Plan Limits -------------------------------------------------------------

def validate_plan_size(step_count: int) -> None:
    """Raise if plan exceeds maximum step count."""
    if step_count > MAX_PLAN_STEPS:
        raise ValueError(
            f"Plan has {step_count} steps, exceeding the maximum of {MAX_PLAN_STEPS}"
        )
