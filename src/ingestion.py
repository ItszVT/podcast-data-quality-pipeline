"""Read podcast CSV/XLSX exports without changing their source values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
REQUIRED_COLUMNS = ("feed_id", "title", "website_url")


class IngestionError(ValueError):
    """Raised when a source file cannot be read as a supported tabular file."""


@dataclass(frozen=True)
class IngestionResult:
    """Imported rows and file metadata; values remain strings where present."""

    source_path: Path
    source_filename: str
    columns: tuple[str, ...]
    records: tuple[dict[str, Any], ...]

    @property
    def total_rows(self) -> int:
        return len(self.records)


def load_podcast_file(file_path: str | Path) -> IngestionResult:
    """Load a CSV or Excel file and preserve cell text and source row order.

    Header whitespace is stripped as specified by the input contract. Cell
    values are otherwise left untouched. Blank cells become empty strings so
    validation can distinguish missing values from malformed non-empty values.
    """
    path = Path(file_path).expanduser()
    if not path.exists():
        raise IngestionError(f"Input file does not exist: {path}")
    if not path.is_file():
        raise IngestionError(f"Input path is not a file: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise IngestionError(f"Unsupported file format '{suffix or '(no extension)'}'. Supported formats: {allowed}.")

    try:
        if suffix == ".csv":
            frame = pd.read_csv(path, dtype="string", keep_default_na=False, encoding="utf-8-sig")
        else:
            frame = pd.read_excel(path, dtype=str, keep_default_na=False, engine="openpyxl")
    except pd.errors.EmptyDataError as exc:
        raise IngestionError("The input file is empty or has no readable header row.") from exc
    except (OSError, UnicodeError, ValueError, ImportError) as exc:
        raise IngestionError(f"Could not read '{path.name}': {exc}") from exc
    except Exception as exc:
        # Excel engines and pandas can raise format-specific exceptions. Expose
        # a useful message without leaking a traceback to a normal caller.
        raise IngestionError(f"Could not read '{path.name}': {exc}") from exc

    normalized_columns = [str(column).strip() for column in frame.columns]
    if len(set(normalized_columns)) != len(normalized_columns):
        duplicates = sorted({name for name in normalized_columns if normalized_columns.count(name) > 1})
        raise IngestionError(
            "Column names are duplicated after trimming surrounding whitespace: "
            + ", ".join(repr(name) for name in duplicates)
        )
    frame.columns = normalized_columns

    if frame.empty:
        raise IngestionError("The input file contains a header but no data rows.")

    records: list[dict[str, Any]] = []
    for row_number, values in enumerate(frame.to_dict(orient="records"), start=2):
        # Preserve every provided column, including unexpected source columns.
        records.append({"source_row": row_number, **values})

    return IngestionResult(
        source_path=path.resolve(),
        source_filename=path.name,
        columns=tuple(normalized_columns),
        records=tuple(records),
    )
