"""Evaluate URL-check classifications against controlled, labeled cases.

The runner accepts only an HTTP loopback base URL, so its reference dataset
cannot direct checks to public websites. The normal URL checker still applies
its safety rules; controlled tests provide a narrowly scoped DNS seam for the
local HTTP fixture, just as the Day 3 tests do.
"""
from __future__ import annotations

import csv
import ipaddress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from .url_checker import (
    HTTP_ERROR,
    INCONCLUSIVE,
    INVALID_INPUT,
    LIKELY_WORKING,
    SUSPECTED_SOFT_404,
    UrlCheckResult,
    check_website_url,
)

KNOWN_CLASSIFICATIONS = {
    LIKELY_WORKING, HTTP_ERROR, SUSPECTED_SOFT_404, INCONCLUSIVE, INVALID_INPUT
}
DEFAULT_REFERENCE_PATH = Path(__file__).resolve().parent.parent / "data" / "reference_url_outcomes.csv"


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    url: str
    expected_classification: str


@dataclass(frozen=True)
class CaseEvaluation:
    case_id: str
    expected_classification: str
    predicted_classification: str
    matched: bool
    evidence: str


@dataclass(frozen=True)
class EvaluationReport:
    case_count: int
    correct_count: int
    accuracy: float | None
    confusion_matrix: dict[str, dict[str, int]]
    cases: tuple[CaseEvaluation, ...]

    def to_dict(self) -> dict[str, object]:
        """Return JSON-ready metrics and case-level evidence."""
        return {
            "case_count": self.case_count,
            "correct_count": self.correct_count,
            "accuracy": self.accuracy,
            "confusion_matrix": self.confusion_matrix,
            "cases": [asdict(case) for case in self.cases],
        }


def _loopback_base_url(base_url: str) -> str:
    """Validate the controlled runner target and return it without a trailing slash."""
    try:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        is_loopback = host == "localhost"
        if not is_loopback:
            is_loopback = ipaddress.ip_address(host).is_loopback
        _ = parts.port
    except (ValueError, TypeError) as exc:
        raise ValueError("base_url must be a valid HTTP loopback URL.") from exc
    if parts.scheme.lower() not in {"http", "https"} or not is_loopback:
        raise ValueError("base_url must use HTTP(S) and point to localhost or a loopback IP.")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("base_url must not contain credentials, a query, or a fragment.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def load_reference_cases(
    reference_path: str | Path = DEFAULT_REFERENCE_PATH,
    *,
    base_url: str,
) -> tuple[EvaluationCase, ...]:
    """Load labeled fixture routes and bind them to a controlled loopback server."""
    base = _loopback_base_url(base_url)
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    with Path(reference_path).open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"case_id", "path", "expected_classification"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Reference CSV must include case_id, path, expected_classification columns.")
        for line_number, row in enumerate(reader, start=2):
            case_id = (row.get("case_id") or "").strip()
            route = (row.get("path") or "").strip()
            expected = (row.get("expected_classification") or "").strip()
            if not case_id or case_id in seen_ids:
                raise ValueError(f"Reference case ID is blank or repeated on line {line_number}.")
            if not route.startswith("/") or route.startswith("//") or "://" in route:
                raise ValueError(f"Reference path must be a local absolute path on line {line_number}.")
            if expected not in KNOWN_CLASSIFICATIONS:
                raise ValueError(f"Unknown expected classification on line {line_number}: {expected!r}.")
            seen_ids.add(case_id)
            cases.append(EvaluationCase(case_id, f"{base}{route}", expected))
    return tuple(cases)


def evaluate_url_cases(
    cases: Iterable[EvaluationCase],
    *,
    checker: Callable[[str], UrlCheckResult] = check_website_url,
) -> EvaluationReport:
    """Run the existing checker and summarize labeled outcomes.

    Inconclusive results remain their own predicted class; they are not silently
    mapped to HTTP errors. Accuracy is descriptive only for these supplied labels.
    """
    case_results: list[CaseEvaluation] = []
    seen_ids: set[str] = set()
    labels: set[str] = set()
    for case in cases:
        if not case.case_id or case.case_id in seen_ids:
            raise ValueError("Evaluation case IDs must be nonblank and unique.")
        if case.expected_classification not in KNOWN_CLASSIFICATIONS:
            raise ValueError(f"Unsupported reference label: {case.expected_classification!r}.")
        seen_ids.add(case.case_id)
        result = checker(case.url)
        predicted = result.classification
        if predicted not in KNOWN_CLASSIFICATIONS:
            raise ValueError(f"Checker returned an unknown classification: {predicted!r}.")
        labels.update((case.expected_classification, predicted))
        case_results.append(CaseEvaluation(
            case_id=case.case_id,
            expected_classification=case.expected_classification,
            predicted_classification=predicted,
            matched=case.expected_classification == predicted,
            evidence=result.evidence,
        ))

    matrix = {expected: {predicted: 0 for predicted in sorted(labels)} for expected in sorted(labels)}
    for case in case_results:
        matrix[case.expected_classification][case.predicted_classification] += 1
    correct = sum(case.matched for case in case_results)
    total = len(case_results)
    return EvaluationReport(
        case_count=total,
        correct_count=correct,
        accuracy=correct / total if total else None,
        confusion_matrix=matrix,
        cases=tuple(case_results),
    )


def evaluate_reference_dataset(
    reference_path: str | Path = DEFAULT_REFERENCE_PATH,
    *,
    base_url: str,
    checker: Callable[[str], UrlCheckResult] = check_website_url,
) -> EvaluationReport:
    """Load the controlled CSV reference cases, check them, and compute metrics."""
    cases = load_reference_cases(reference_path, base_url=base_url)
    return evaluate_url_cases(cases, checker=checker)
