"""Append raw tick records as newline-delimited JSON."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any, TextIO


class JsonlRawSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: TextIO | None = None

    def write(self, record: Mapping[str, Any]) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8", buffering=1)
        json.dump(record, self._file, ensure_ascii=False, separators=(",", ":"))
        self._file.write("\n")

    def close(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            file.close()

