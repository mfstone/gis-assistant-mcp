"""Tests for QGISClient: layer resolution and directory scanning."""

import os
from unittest.mock import patch

import pytest

from gis_assistant.qgis_client import AmbiguousLayerError, QGISClient


# -- Helpers -----------------------------------------------------------------

def _make_client():
    """Create a QGISClient without connecting."""
    client = QGISClient.__new__(QGISClient)
    client.host = "127.0.0.1"
    client.port = 9876
    client._socket = None
    return client


LAYERS = [
    {
        "id": "parcels_abc123",
        "name": "Parcels",
        "source": "/data/parcels.gpkg|layername=parcels",
        "type": "vector_0",
    },
    {
        "id": "dem_def456",
        "name": "DEM",
        "source": "/data/dem.tif",
        "type": "raster",
    },
    {
        "id": "mem_layer_1",
        "name": "TempPoints",
        "source": "memory",
        "type": "vector_0",
    },
]


# -- resolve_layer tests -----------------------------------------------------

def test_resolve_layer_exact_id():
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        assert client.resolve_layer("parcels_abc123") == "parcels_abc123"


def test_resolve_layer_source_path():
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        assert client.resolve_layer("/data/parcels.gpkg") == "parcels_abc123"


def test_resolve_layer_source_with_pipe_suffix():
    """Source matching strips |layername=... before normalizing."""
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        # Match the base path even though the source has a pipe suffix
        assert client.resolve_layer("/data/parcels.gpkg") == "parcels_abc123"


def test_resolve_layer_source_raster():
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        assert client.resolve_layer("/data/dem.tif") == "dem_def456"


def test_resolve_layer_name_match():
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        assert client.resolve_layer("DEM") == "dem_def456"


def test_resolve_layer_ambiguous_name():
    """Two layers with the same name should raise AmbiguousLayerError."""
    dup_layers = LAYERS + [
        {"id": "dem_copy", "name": "DEM", "source": "/data/dem2.tif", "type": "raster"},
    ]
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": dup_layers}):
        with pytest.raises(AmbiguousLayerError):
            client.resolve_layer("DEM")


def test_resolve_layer_not_found():
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        with pytest.raises(ValueError, match="Layer not found"):
            client.resolve_layer("Nonexistent")


def test_resolve_layer_memory_source_skipped():
    """Memory layer sources should not match filesystem path identifiers."""
    client = _make_client()
    with patch.object(client, "get_layers", return_value={"result": LAYERS}):
        # "memory" shouldn't match as a source path; should fall through to name
        assert client.resolve_layer("TempPoints") == "mem_layer_1"


# -- scan_directory tests ----------------------------------------------------

def test_scan_directory_basic(tmp_path):
    (tmp_path / "a.gpkg").write_text("data")
    (tmp_path / "b.shp").write_text("data")
    (tmp_path / "c.txt").write_text("data")
    files = QGISClient.scan_directory(str(tmp_path))
    assert len(files) == 3
    assert all(os.path.isabs(f) for f in files)


def test_scan_directory_extension_filter(tmp_path):
    (tmp_path / "a.gpkg").write_text("data")
    (tmp_path / "b.shp").write_text("data")
    (tmp_path / "c.txt").write_text("data")
    files = QGISClient.scan_directory(str(tmp_path), extensions=[".gpkg", ".shp"])
    assert len(files) == 2
    assert any("a.gpkg" in f for f in files)
    assert any("b.shp" in f for f in files)


def test_scan_directory_limit(tmp_path):
    for i in range(10):
        (tmp_path / f"file_{i}.gpkg").write_text("data")
    files = QGISClient.scan_directory(str(tmp_path), limit=3)
    assert len(files) == 3
