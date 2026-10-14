"""QGIS socket client -- adapted from qgis_mcp.qgis_socket_client.

Communicates with the QGIS MCP Plugin socket server on 127.0.0.1:9876
using NDJSON framing.  Intentionally omits execute_code.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger("gis_assistant.qgis_client")

PROTOCOL_VERSION = 1


class AmbiguousLayerError(Exception):
    """Raised when a layer identifier matches more than one layer."""

    def __init__(self, identifier: str, matches: list[dict]):
        self.identifier = identifier
        self.matches = matches
        choices = ", ".join(
            f"{m.get('name', '?')} (id={m.get('id', '?')})" for m in matches
        )
        super().__init__(
            f"Ambiguous layer {identifier!r}: matches {len(matches)} layers: {choices}"
        )


class QGISClient:
    """TCP client for the QGIS MCP Plugin socket server."""

    MAX_LINE_LENGTH = 10 * 1024 * 1024  # 10 MB

    def __init__(self, host: str = "127.0.0.1", port: int = 9876):
        self.host = host
        self.port = port
        self._socket: socket.socket | None = None

    # -- connection management -----------------------------------------------

    def connect(self) -> bool:
        """Connect to the QGIS plugin socket server."""
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.connect((self.host, self.port))
            logger.info("Connected to QGIS at %s:%s", self.host, self.port)
            return True
        except Exception:
            logger.exception("Failed to connect to QGIS")
            self._socket = None
            return False

    def disconnect(self) -> None:
        """Close the connection."""
        if self._socket:
            self._socket.close()
            self._socket = None

    def ensure_connected(self) -> None:
        """Reconnect if the socket is dead."""
        if self._socket is None:
            if not self.connect():
                raise ConnectionError("Cannot reach QGIS plugin socket server")
            return
        try:
            self._socket.sendall(b"")
        except Exception:
            self.disconnect()
            if not self.connect():
                raise ConnectionError("Cannot reach QGIS plugin socket server")

    # -- low-level send/recv -------------------------------------------------

    def send_command(self, command_type: str, params: dict | None = None) -> dict:
        """Send a command and return the parsed response."""
        self.ensure_connected()
        assert self._socket is not None

        command = {"v": PROTOCOL_VERSION, "type": command_type, "params": params or {}}
        self._socket.sendall(json.dumps(command).encode("utf-8") + b"\n")

        buf = b""
        while True:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise ConnectionError("QGIS socket closed unexpectedly")
            buf += chunk
            if len(buf) > self.MAX_LINE_LENGTH:
                raise ValueError("Response exceeded maximum line length")
            if b"\n" in buf:
                line = buf.split(b"\n", 1)[0]
                return json.loads(line.decode("utf-8"))

    # -- high-level helpers --------------------------------------------------

    def ping(self) -> dict:
        return self.send_command("ping")

    def get_qgis_info(self) -> dict:
        return self.send_command("get_qgis_info")

    def get_project_info(self) -> dict:
        return self.send_command("get_project_info")

    def get_layers(self) -> dict:
        return self.send_command("get_layers")

    def get_layer_features(self, layer_id: str, limit: int = 10) -> dict:
        return self.send_command(
            "get_layer_features", {"layer_id": layer_id, "limit": limit}
        )

    def add_vector_layer(
        self, path: str, name: str | None = None, provider: str = "ogr"
    ) -> dict:
        params: dict = {"path": path, "provider": provider}
        if name:
            params["name"] = name
        return self.send_command("add_vector_layer", params)

    def add_raster_layer(
        self, path: str, name: str | None = None, provider: str = "gdal"
    ) -> dict:
        params: dict = {"path": path, "provider": provider}
        if name:
            params["name"] = name
        return self.send_command("add_raster_layer", params)

    def remove_layer(self, layer_id: str) -> dict:
        return self.send_command("remove_layer", {"layer_id": layer_id})

    def zoom_to_layer(self, layer_id: str) -> dict:
        return self.send_command("zoom_to_layer", {"layer_id": layer_id})

    def execute_processing(self, algorithm: str, parameters: dict) -> dict:
        return self.send_command(
            "execute_processing",
            {"algorithm": algorithm, "parameters": parameters},
        )

    def render_map(self, path: str, width: int = 800, height: int = 600) -> dict:
        return self.send_command(
            "render_map", {"path": path, "width": width, "height": height}
        )

    def save_project(self, path: str | None = None) -> dict:
        params = {"path": path} if path else {}
        return self.send_command("save_project", params)

    def load_project(self, path: str) -> dict:
        return self.send_command("load_project", {"path": path})

    # -- layer resolution ----------------------------------------------------

    def resolve_layer(self, identifier: str) -> str:
        """Resolve a layer identifier to a stable layer ID.

        Tries (in order): exact layer ID match, source path match, name match.
        Raises AmbiguousLayerError if name matches multiple layers.
        """
        resp = self.get_layers()
        layers = resp.get("result", resp) if isinstance(resp, dict) else resp
        if not isinstance(layers, list):
            raise ValueError(f"Unexpected get_layers response: {resp}")

        # 1) Exact ID match
        for layer in layers:
            if layer.get("id") == identifier:
                return identifier

        # 2) Source path match — normalize both sides, strip GPKG pipe suffixes
        norm_id = os.path.normcase(os.path.normpath(identifier))
        for layer in layers:
            source = layer.get("source", "")
            if source and not source.startswith(("memory", "http", "https")):
                source_path = source.split("|")[0]
                if os.path.normcase(os.path.normpath(source_path)) == norm_id:
                    return layer["id"]

        # 3) Name match
        matches = [l for l in layers if l.get("name") == identifier]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            raise AmbiguousLayerError(identifier, matches)

        raise ValueError(f"Layer not found: {identifier!r}")

    # -- local helpers (no socket) -------------------------------------------

    @staticmethod
    def scan_directory(
        directory: str,
        extensions: list[str] | None = None,
        limit: int = 200,
    ) -> list[str]:
        """List data files in a directory. Runs locally, not via QGIS."""
        exts = {e.lower().lstrip(".") for e in (extensions or [])}
        results: list[str] = []
        try:
            entries = sorted(os.listdir(directory))
        except OSError as e:
            logger.warning("Cannot scan directory %s: %s", directory, e)
            return []
        for entry in entries:
            if exts and Path(entry).suffix.lower().lstrip(".") not in exts:
                continue
            results.append(os.path.join(directory, entry))
            if len(results) >= limit:
                break
        return results
