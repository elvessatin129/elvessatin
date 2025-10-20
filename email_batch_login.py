#!/usr/bin/env python3
"""
Batch email login tester using IMAP and POP3.

- Reads credentials from a CSV (at minimum: email,password). Optional columns:
  username, imap_host, imap_port, pop3_host, pop3_port, protocol, security
- Supports per-row overrides and global CLI defaults
- Protocols: imap, pop3, both
- Security: ssl (implicit TLS), starttls, plain
- Concurrency with ThreadPoolExecutor
- Writes results to CSV: email,username,protocol,security,host,port,status,error,greeting,response,duration_ms

Note: Some providers require app-specific passwords or OAuth and will fail with
basic authentication. This tool simply attempts basic auth to verify credentials.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import imaplib
import poplib


# ----------------------------- Data structures ----------------------------- #


@dataclass
class AccountInput:
    email: str
    password: str
    username: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    pop3_host: Optional[str] = None
    pop3_port: Optional[int] = None
    protocol: Optional[str] = None  # imap | pop3 | both
    security: Optional[str] = None  # ssl | starttls | plain
    row_index: int = -1


@dataclass
class AttemptConfig:
    email: str
    username: str
    password: str
    protocol: str  # imap | pop3
    security: str  # ssl | starttls | plain
    host: str
    port: int
    timeout: float
    verify_tls: bool


@dataclass
class AttemptResult:
    email: str
    username: str
    protocol: str
    security: str
    host: str
    port: int
    status: str  # success | auth_failed | timeout | dns_error | connection_refused | tls_error | unsupported | network_error | error
    error: Optional[str]
    server_greeting: Optional[str]
    server_response: Optional[str]
    duration_ms: int


# ----------------------------- Utility helpers ----------------------------- #


def bytes_to_text(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    for enc in ("utf-8", "latin-1", "ascii"):
        try:
            return data.decode(enc, errors="replace")
        except Exception:
            continue
    return data.decode(errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)


def normalize_protocol(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in {"imap", "pop3"}:
        return v
    if v in {"both", "all"}:
        return "both"
    return None


def normalize_security(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in {"ssl", "implicit", "implicit_tls"}:
        return "ssl"
    if v in {"starttls", "tls"}:
        return "starttls"
    if v in {"plain", "none", "insecure"}:
        return "plain"
    return None


def extract_domain(email: str) -> Optional[str]:
    try:
        return email.split("@", 1)[1].lower()
    except Exception:
        return None


# Known provider host mappings
PROVIDER_IMAP: Dict[str, str] = {
    # Global
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "office365.com": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "aol.com": "imap.aol.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "proton.me": "127.0.0.1",  # Proton requires Bridge; placeholder
    "protonmail.com": "127.0.0.1",  # requires Bridge
    # China mainland popular
    "qq.com": "imap.qq.com",
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "yeah.net": "imap.yeah.net",
    "sina.com": "imap.sina.com",
    "sohu.com": "imap.sohu.com",
    "foxmail.com": "imap.foxmail.com",
}

PROVIDER_POP3: Dict[str, str] = {
    "gmail.com": "pop.gmail.com",
    "googlemail.com": "pop.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "office365.com": "outlook.office365.com",
    "yahoo.com": "pop.mail.yahoo.com",
    "aol.com": "pop.aol.com",
    # Apple iCloud POP is generally unsupported; left empty on purpose
    "icloud.com": "pop.mail.me.com",
    "me.com": "pop.mail.me.com",
    "mac.com": "pop.mail.me.com",
    "proton.me": "127.0.0.1",
    "protonmail.com": "127.0.0.1",
    # China mainland popular
    "qq.com": "pop.qq.com",
    "163.com": "pop.163.com",
    "126.com": "pop.126.com",
    "yeah.net": "pop.yeah.net",
    "sina.com": "pop.sina.com",
    "sohu.com": "pop3.sohu.com",
    "foxmail.com": "pop.foxmail.com",
}


def guess_imap_host(email: str) -> Optional[str]:
    domain = extract_domain(email)
    if not domain:
        return None
    if domain in PROVIDER_IMAP:
        return PROVIDER_IMAP[domain]
    # Default heuristic
    return f"imap.{domain}"


def guess_pop3_host(email: str) -> Optional[str]:
    domain = extract_domain(email)
    if not domain:
        return None
    if domain in PROVIDER_POP3:
        return PROVIDER_POP3[domain]
    return f"pop.{domain}"


def default_port(protocol: str, security: str) -> int:
    if protocol == "imap":
        return 993 if security == "ssl" else 143
    return 995 if security == "ssl" else 110


# ----------------------------- Network operations ----------------------------- #


def make_ssl_context(verify: bool) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    # Insecure context requested; intended for testing only
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def attempt_imap_login(cfg: AttemptConfig) -> AttemptResult:
    start = time.perf_counter()
    context = make_ssl_context(cfg.verify_tls)
    imap: Optional[imaplib.IMAP4] = None
    greeting: Optional[str] = None
    response_text: Optional[str] = None
    status = "error"
    error_msg: Optional[str] = None

    # Use global socket timeout to influence underlying library
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(cfg.timeout)
    try:
        if cfg.security == "ssl":
            imap = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=context)
        else:
            imap = imaplib.IMAP4(cfg.host, cfg.port)
            if cfg.security == "starttls":
                imap.starttls(ssl_context=context)
        greeting = bytes_to_text(getattr(imap, "welcome", None))
        typ, data = imap.login(cfg.username, cfg.password)
        response_text = (typ or "").upper()
        if data:
            try:
                response_text = f"{response_text} {bytes_to_text(data[0] if isinstance(data, (list, tuple)) else data)}"
            except Exception:
                pass
        status = "success" if (typ and typ.upper() == "OK") else "auth_failed"
    except imaplib.IMAP4.error as e:
        msg = str(e)
        error_msg = msg
        if any(key in msg.upper() for key in ["AUTH", "LOGIN", "BAD"]):
            status = "auth_failed"
        else:
            status = "error"
    except ssl.SSLCertVerificationError as e:
        status = "tls_error"
        error_msg = f"SSL cert verify error: {e}"
    except ssl.SSLError as e:
        status = "tls_error"
        error_msg = f"SSL error: {e}"
    except socket.timeout:
        status = "timeout"
        error_msg = "operation timed out"
    except ConnectionRefusedError as e:
        status = "connection_refused"
        error_msg = str(e)
    except socket.gaierror as e:
        status = "dns_error"
        error_msg = str(e)
    except OSError as e:
        status = "network_error"
        error_msg = str(e)
    except Exception as e:
        status = "error"
        error_msg = f"{type(e).__name__}: {e}"
    finally:
        try:
            if imap is not None:
                imap.logout()
        except Exception:
            pass
        socket.setdefaulttimeout(previous_timeout)

    duration_ms = int((time.perf_counter() - start) * 1000)
    return AttemptResult(
        email=cfg.email,
        username=cfg.username,
        protocol=cfg.protocol,
        security=cfg.security,
        host=cfg.host,
        port=cfg.port,
        status=status,
        error=error_msg,
        server_greeting=greeting,
        server_response=response_text,
        duration_ms=duration_ms,
    )


def attempt_pop3_login(cfg: AttemptConfig) -> AttemptResult:
    start = time.perf_counter()
    context = make_ssl_context(cfg.verify_tls)
    pop: Optional[poplib.POP3] = None
    greeting: Optional[str] = None
    response_text: Optional[str] = None
    status = "error"
    error_msg: Optional[str] = None

    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(cfg.timeout)
    try:
        if cfg.security == "ssl":
            pop = poplib.POP3_SSL(cfg.host, cfg.port, context=context, timeout=cfg.timeout)
        else:
            pop = poplib.POP3(cfg.host, cfg.port, timeout=cfg.timeout)
            if cfg.security == "starttls":
                pop.stls(context=context)
        greeting = bytes_to_text(getattr(pop, "welcome", None))
        pop.user(cfg.username)
        resp = pop.pass_(cfg.password)
        response_text = bytes_to_text(resp)
        status = "success"
    except poplib.error_proto as e:
        msg = str(e)
        error_msg = msg
        if any(key in msg.lower() for key in ["auth", "login", "password", "authorization"]):
            status = "auth_failed"
        else:
            status = "error"
    except ssl.SSLCertVerificationError as e:
        status = "tls_error"
        error_msg = f"SSL cert verify error: {e}"
    except ssl.SSLError as e:
        status = "tls_error"
        error_msg = f"SSL error: {e}"
    except socket.timeout:
        status = "timeout"
        error_msg = "operation timed out"
    except ConnectionRefusedError as e:
        status = "connection_refused"
        error_msg = str(e)
    except socket.gaierror as e:
        status = "dns_error"
        error_msg = str(e)
    except OSError as e:
        status = "network_error"
        error_msg = str(e)
    except Exception as e:
        status = "error"
        error_msg = f"{type(e).__name__}: {e}"
    finally:
        try:
            if pop is not None:
                pop.quit()
        except Exception:
            pass
        socket.setdefaulttimeout(previous_timeout)

    duration_ms = int((time.perf_counter() - start) * 1000)
    return AttemptResult(
        email=cfg.email,
        username=cfg.username,
        protocol=cfg.protocol,
        security=cfg.security,
        host=cfg.host,
        port=cfg.port,
        status=status,
        error=error_msg,
        server_greeting=greeting,
        server_response=response_text,
        duration_ms=duration_ms,
    )


# ----------------------------- CSV IO ----------------------------- #


REQUIRED_COLUMNS = {"email", "password"}
OPTIONAL_COLUMNS = {
    "username",
    "imap_host",
    "imap_port",
    "pop3_host",
    "pop3_port",
    "protocol",
    "security",
    # Generic fallbacks (applied to both protocols if specific host/port missing)
    "host",
    "port",
}


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except Exception:
        return None


def read_credentials(input_csv: Path, delimiter: str) -> List[AccountInput]:
    rows: List[AccountInput] = []
    with input_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = {h.strip(): h for h in (reader.fieldnames or [])}
        missing = [c for c in REQUIRED_COLUMNS if c not in headers]
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")

        for idx, raw in enumerate(reader, start=2):  # data typically starts at line 2
            email = (raw.get(headers.get("email")) or "").strip()
            password = (raw.get(headers.get("password")) or "").strip()
            if not email or not password:
                # Skip incomplete lines; keep behavior strict
                continue
            username = (raw.get(headers.get("username")) or "").strip() or email

            # Specific hosts/ports
            imap_host = (raw.get(headers.get("imap_host")) or "").strip() or None
            pop3_host = (raw.get(headers.get("pop3_host")) or "").strip() or None
            imap_port = _int_or_none(raw.get(headers.get("imap_port")))
            pop3_port = _int_or_none(raw.get(headers.get("pop3_port")))

            # Generic fallback host/port
            generic_host = (raw.get(headers.get("host")) or "").strip() or None
            generic_port = _int_or_none(raw.get(headers.get("port")))
            imap_host = imap_host or generic_host
            pop3_host = pop3_host or generic_host
            imap_port = imap_port or generic_port
            pop3_port = pop3_port or generic_port

            protocol = normalize_protocol(raw.get(headers.get("protocol")))
            security = normalize_security(raw.get(headers.get("security")))

            rows.append(
                AccountInput(
                    email=email,
                    password=password,
                    username=username,
                    imap_host=imap_host,
                    imap_port=imap_port,
                    pop3_host=pop3_host,
                    pop3_port=pop3_port,
                    protocol=protocol,
                    security=security,
                    row_index=idx,
                )
            )
    return rows


def write_results(output_csv: Path, rows: Iterable[AttemptResult], delimiter: str) -> None:
    headers = [
        "email",
        "username",
        "protocol",
        "security",
        "host",
        "port",
        "status",
        "error",
        "greeting",
        "response",
        "duration_ms",
    ]
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, delimiter=delimiter)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "email": r.email,
                    "username": r.username,
                    "protocol": r.protocol,
                    "security": r.security,
                    "host": r.host,
                    "port": r.port,
                    "status": r.status,
                    "error": (r.error or "").strip(),
                    "greeting": (r.server_greeting or "").strip(),
                    "response": (r.server_response or "").strip(),
                    "duration_ms": r.duration_ms,
                }
            )


# ----------------------------- Orchestration ----------------------------- #


def build_attempts(
    accounts: List[AccountInput],
    protocols_cli: str,
    security_cli: str,
    imap_host_cli: Optional[str],
    imap_port_cli: Optional[int],
    pop3_host_cli: Optional[str],
    pop3_port_cli: Optional[int],
    timeout: float,
    verify_tls: bool,
) -> List[AttemptConfig]:
    attempts: List[AttemptConfig] = []
    for acc in accounts:
        protocols = acc.protocol or protocols_cli
        # Expand 'both' into two attempts
        expanded_protocols: List[str] = ["imap", "pop3"] if protocols == "both" else [protocols]
        for proto in expanded_protocols:
            if proto == "imap":
                security = acc.security or security_cli
                host = acc.imap_host or imap_host_cli or guess_imap_host(acc.email)
                port = acc.imap_port or imap_port_cli or default_port("imap", security)
            elif proto == "pop3":
                security = acc.security or security_cli
                host = acc.pop3_host or pop3_host_cli or guess_pop3_host(acc.email)
                port = acc.pop3_port or pop3_port_cli or default_port("pop3", security)
            else:
                # Unknown protocol, skip
                continue

            # Final normalization and guard clauses
            security = normalize_security(security) or "ssl"
            if not host:
                # Should not happen after heuristics, but guard nevertheless
                continue
            attempts.append(
                AttemptConfig(
                    email=acc.email,
                    username=acc.username or acc.email,
                    password=acc.password,
                    protocol=proto,
                    security=security,
                    host=host,
                    port=int(port),
                    timeout=timeout,
                    verify_tls=verify_tls,
                )
            )
    return attempts


def run_attempt(cfg: AttemptConfig) -> AttemptResult:
    if cfg.protocol == "imap":
        return attempt_imap_login(cfg)
    if cfg.protocol == "pop3":
        return attempt_pop3_login(cfg)
    return AttemptResult(
        email=cfg.email,
        username=cfg.username,
        protocol=cfg.protocol,
        security=cfg.security,
        host=cfg.host,
        port=cfg.port,
        status="unsupported",
        error=f"unsupported protocol: {cfg.protocol}",
        server_greeting=None,
        server_response=None,
        duration_ms=0,
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch email login tester (IMAP/POP3)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, help="Path to input CSV (email,password,...) ")
    parser.add_argument(
        "-o",
        "--output",
        help="Path to output CSV. Defaults to <input>.results.csv in the same directory.",
    )
    parser.add_argument(
        "-p",
        "--protocols",
        choices=["imap", "pop3", "both"],
        default="imap",
        help="Which protocol(s) to attempt for each row",
    )
    parser.add_argument(
        "-s",
        "--security",
        choices=["ssl", "starttls", "plain"],
        default="ssl",
        help="Connection security to use when connecting",
    )
    parser.add_argument("--imap-host", help="Override IMAP host for all rows")
    parser.add_argument("--imap-port", type=int, help="Override IMAP port for all rows")
    parser.add_argument("--pop3-host", help="Override POP3 host for all rows")
    parser.add_argument("--pop3-port", type=int, help="Override POP3 port for all rows")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=min(32, (os.cpu_count() or 8) * 4),
        help="Number of concurrent worker threads",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=15.0,
        help="Per-connection timeout in seconds",
    )
    parser.add_argument(
        "-d",
        "--delimiter",
        default=",",
        help="CSV delimiter for input/output",
    )
    parser.add_argument(
        "--no-verify-tls",
        action="store_true",
        help="Disable TLS certificate verification (INSECURE; for testing only)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args(argv)

    # Normalize
    args.protocols = normalize_protocol(args.protocols) or "imap"
    args.security = normalize_security(args.security) or "ssl"
    args.verify_tls = not args.no_verify_tls

    # Output default
    if not args.output:
        inp = Path(args.input)
        args.output = str(inp.with_suffix(inp.suffix + ".results.csv"))

    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    input_csv = Path(args.input)
    output_csv = Path(args.output)

    if not input_csv.exists():
        logging.error("Input CSV not found: %s", input_csv)
        return 2

    try:
        accounts = read_credentials(input_csv, delimiter=args.delimiter)
    except Exception as e:
        logging.error("Failed to read CSV: %s", e)
        return 2

    if not accounts:
        logging.error("No valid rows found in input CSV")
        return 2

    attempts = build_attempts(
        accounts=accounts,
        protocols_cli=args.protocols,
        security_cli=args.security,
        imap_host_cli=args.imap_host,
        imap_port_cli=args.imap_port,
        pop3_host_cli=args.pop3_host,
        pop3_port_cli=args.pop3_port,
        timeout=args.timeout,
        verify_tls=args.verify_tls,
    )

    if not attempts:
        logging.error("No attempts to perform. Check your CSV and flags.")
        return 2

    logging.info("Starting %d login attempt(s) with %d worker(s)...", len(attempts), min(args.workers, len(attempts)))

    results: List[AttemptResult] = []
    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(attempts)))) as executor:
        future_to_cfg = {executor.submit(run_attempt, cfg): cfg for cfg in attempts}
        for future in as_completed(future_to_cfg):
            cfg = future_to_cfg[future]
            try:
                result = future.result()
            except Exception as e:
                # This should be rare; wrap into a generic error result
                logging.exception("Unhandled error in worker for %s://%s:%s", cfg.protocol, cfg.host, cfg.port)
                result = AttemptResult(
                    email=cfg.email,
                    username=cfg.username,
                    protocol=cfg.protocol,
                    security=cfg.security,
                    host=cfg.host,
                    port=cfg.port,
                    status="error",
                    error=f"{type(e).__name__}: {e}",
                    server_greeting=None,
                    server_response=None,
                    duration_ms=0,
                )
            results.append(result)
            logging.debug(
                "[%s] %s@%s:%d -> %s (%s)",
                result.protocol,
                result.username,
                result.host,
                result.port,
                result.status,
                (result.error or "ok"),
            )

    try:
        write_results(output_csv, results, delimiter=args.delimiter)
    except Exception as e:
        logging.error("Failed to write results CSV: %s", e)
        return 2

    # Brief summary
    success = sum(1 for r in results if r.status == "success")
    auth_failed = sum(1 for r in results if r.status == "auth_failed")
    timeouts = sum(1 for r in results if r.status == "timeout")
    logging.info(
        "Finished. success=%d, auth_failed=%d, timeout=%d, total=%d -> %s",
        success,
        auth_failed,
        timeouts,
        len(results),
        output_csv,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
