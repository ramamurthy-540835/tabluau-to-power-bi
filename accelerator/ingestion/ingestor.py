from __future__ import annotations
import hashlib
import zipfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawArtifact:
    artifact_id: str
    artifact_type: str          # twb, twbx, tds, tdsx, tfl, tflx
    source_path: str
    raw_xml: str
    source_hash: str
    extracts: dict = field(default_factory=dict)   # filename → bytes
    metadata: dict = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_xml_from_zip(zf: zipfile.ZipFile, suffix: str) -> Optional[str]:
    for name in zf.namelist():
        if name.endswith(suffix):
            return zf.read(name).decode("utf-8", errors="replace")
    return None


def ingest(path: str | Path) -> RawArtifact:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Artifact not found: {path}")

    raw_bytes = p.read_bytes()
    source_hash = _sha256(raw_bytes)
    suffix = p.suffix.lower().lstrip(".")

    if suffix in ("twb", "tds", "tfl"):
        raw_xml = raw_bytes.decode("utf-8", errors="replace")
        return RawArtifact(
            artifact_id=source_hash[:12],
            artifact_type=suffix,
            source_path=str(p),
            raw_xml=raw_xml,
            source_hash=source_hash,
        )

    if suffix in ("twbx", "tdsx", "tflx"):
        xml_suffix = {"twbx": ".twb", "tdsx": ".tds", "tflx": ".tfl"}[suffix]
        extracts: dict = {}
        raw_xml = ""
        with zipfile.ZipFile(p) as zf:
            raw_xml_candidate = _read_xml_from_zip(zf, xml_suffix)
            if raw_xml_candidate is None:
                raise ValueError(f"No {xml_suffix} found inside {p.name}")
            raw_xml = raw_xml_candidate
            for name in zf.namelist():
                if name.lower().endswith(".hyper") or name.lower().endswith(".csv"):
                    try:
                        if zf.getinfo(name).file_size < 10 * 1024 * 1024 * 1024:
                            extracts[name] = zf.read(name)
                    except Exception:
                        pass
        return RawArtifact(
            artifact_id=source_hash[:12],
            artifact_type=suffix,
            source_path=str(p),
            raw_xml=raw_xml,
            source_hash=source_hash,
            extracts=extracts,
        )

    raise ValueError(f"Unsupported artifact type: {suffix}")
