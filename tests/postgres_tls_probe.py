"""Safe PostgreSQL STARTTLS probe for GitHub Actions diagnostics."""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable

SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def classify_openssl_result(output: str, returncode: int) -> str:
    """Map captured OpenSSL output to a fixed category; never return its text."""
    text = output.casefold()
    verify_codes = re.findall(r"verify error:num=(\d+):", text)
    if "hostname mismatch" in text or any(code == "62" for code in verify_codes):
        return "hostname_mismatch"
    if any(token in text for token in ("certificate has expired", "certificate is not yet valid")) or any(
        code in {"9", "10"} for code in verify_codes
    ):
        return "certificate_expiry_or_validity_failure"
    if any(
        token in text
        for token in (
            "unable to get local issuer certificate",
            "unable to verify the first certificate",
            "self-signed certificate",
            "unknown ca",
        )
    ) or any(code in {"18", "19", "20", "21", "27"} for code in verify_codes):
        return "certificate_chain_failure"
    if returncode == 0 and ("verify return code: 0 (ok)" in text or "verification: ok" in text):
        return "system_openssl_verification_passed"
    return "unsupported_or_noncertificate_failure"


def run_postgres_tls_probe(
    hostname: str,
    port: int,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Use OpenSSL's PostgreSQL STARTTLS mode, returning only a safe category."""
    if not hostname or any(char.isspace() for char in hostname) or not 1 <= port <= 65535:
        return "unsupported_or_noncertificate_failure"
    destination_host = f"[{hostname}]" if ":" in hostname else hostname
    command = [
        "openssl", "s_client", "-starttls", "postgres",
        "-connect", f"{destination_host}:{port}",
        "-servername", hostname,
        "-verify_hostname", hostname,
        "-verify_return_error",
        "-ignore_unexpected_eof",
        "-CAfile", SYSTEM_CA_BUNDLE,
    ]
    try:
        result = run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unsupported_or_noncertificate_failure"
    return classify_openssl_result(f"{result.stdout}\n{result.stderr}", result.returncode)


def main() -> None:
    hostname = os.environ.get("PODCAST_TEST_EXPECTED_HOST", "")
    raw_port = os.environ.get("PODCAST_TEST_EXPECTED_PORT", "5432")
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    print("postgres_tls_probe_client=runner_openssl")
    print(f"postgres_tls_openssl_probe={run_postgres_tls_probe(hostname, port)}")


if __name__ == "__main__":
    main()
