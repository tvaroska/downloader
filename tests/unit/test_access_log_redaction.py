"""Unit and loopback Uvicorn subprocess tests for access-log credential redaction (`S-9`)."""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from downloader.logging_config import (
    CustomJsonFormatter,
    RedactingAccessFormatter,
    redact_access_path,
)


@pytest.mark.unit
class TestAccessLogRedaction:
    def test_redact_access_path_strips_userinfo_and_encoded_query_tokens(self) -> None:
        user_cred = "alice:" + "s3cr3tPass99"
        query_tok = "tok_live_987654"

        assert (
            redact_access_path(f"/https://{user_cred}@Example.COM.:443/private/doc?render=true")
            == "/https://example.com/private/doc"
        )
        assert (
            redact_access_path(
                f"/https://alice%3As3cr3tPass99%40example.com:8443/api/v1%3Fkey%3D{query_tok}%26p%3D1?ocr=true"
            )
            == "/https://example.com:8443/api/v1"
        )
        assert (
            redact_access_path(
                f"/https%3A%2F%2Falice%3As3cr3tPass99%40example.com%2Ffeed%3Fsig%3D{query_tok}"
            )
            == "/https://example.com/feed"
        )
        assert (
            redact_access_path(f"/{user_cred}@example.com/my%20file.pdf%3Fauth%3D{query_tok}")
            == "/http://example.com/my%20file.pdf"
        )
        assert (
            redact_access_path(f"/ftp://{user_cred}@files.example.com/archive.zip")
            == "/ftp://files.example.com/archive.zip"
        )
        assert redact_access_path("/health?probe=1") == "/health"
        assert redact_access_path(f"/render?key={query_tok}") == "/render"
        assert (
            redact_access_path("/schedules/abc-123/history?limit=10")
            == "/schedules/abc-123/history"
        )
        assert redact_access_path(f"https://{user_cred}@bad") == "/<redacted-target>"
        assert redact_access_path(f"/https://{user_cred}@[::1/broken") == "/<redacted-target>"

    def test_json_and_plain_formatters_redact_tuple_and_string_access_records(self) -> None:
        user_cred = "bob:" + "hunter2pw"
        query_tok = "bearer_xyz_777"
        raw_path = (
            f"/https://{user_cred}@example.com/callback%3Fcode%3D{query_tok}%23frag?bad_param=1"
        )

        json_fmt = CustomJsonFormatter("%(timestamp)s %(level)s %(logger)s %(message)s")
        plain_fmt = RedactingAccessFormatter("%(asctime)s - %(levelname)s - %(message)s")

        tuple_rec = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:50123", "GET", raw_path, "1.1", 401),
            exc_info=None,
        )
        json_out = json_fmt.format(tuple_rec)
        plain_out = plain_fmt.format(tuple_rec)
        parsed_json = json.loads(json_out)

        assert parsed_json["path"] == "/https://example.com/callback"
        assert parsed_json["status_code"] == 401
        assert "/https://example.com/callback HTTP/1.1" in parsed_json["message"]
        assert "/https://example.com/callback HTTP/1.1" in plain_out
        for leaked in ("bob", "hunter2pw", query_tok, "bad_param"):
            assert leaked not in json_out
            assert leaked not in plain_out

        str_rec = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=2,
            msg=f'127.0.0.1:50124 - "GET {raw_path} HTTP/1.1" 400',
            args=(),
            exc_info=None,
        )
        str_json_out = json_fmt.format(str_rec)
        str_plain_out = plain_fmt.format(str_rec)
        assert json.loads(str_json_out)["path"] == "/https://example.com/callback"
        for leaked in ("bob", "hunter2pw", query_tok, "bad_param"):
            assert leaked not in str_json_out
            assert leaked not in str_plain_out

    @pytest.mark.parametrize("json_logs", ["true", "false"])
    def test_shipped_uvicorn_cli_redacts_401_and_400_access_logs(self, json_logs: str) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        sock.set_inheritable(True)
        port = sock.getsockname()[1]

        api_gate_val = "gate-key-check-88"
        env = {
            **os.environ,
            "ENVIRONMENT": "development",
            "DOWNLOADER_KEY": api_gate_val,
            "LOG_JSON_LOGS": json_logs,
            "REDIS_URI": "",
            "OTEL_ENABLED": "false",
            "SHUTDOWN_PRE_STOP_DELAY": "0.0",
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--app-dir",
                str(repo_root / "src"),
                "downloader.main:app",
                "--fd",
                str(sock.fileno()),
                "--timeout-graceful-shutdown",
                "5",
            ],
            env=env,
            pass_fds=(sock.fileno(),),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        sock.close()
        captured: list[str] = []
        try:
            assert proc.stdout is not None
            ready = False
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and proc.poll() is None:
                rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
                if not rlist:
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                captured.append(line)
                if "Uvicorn running on" in line or "Application startup complete" in line:
                    ready = True
                    break
            assert ready, "".join(captured)

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            cred_pw = "leakPass401"
            enc_tok = "encQueryTok400"

            req_401 = urllib.request.Request(
                f"http://127.0.0.1:{port}/https://alice:{cred_pw}@example.com/secret%3Fk%3D{enc_tok}"
            )
            with pytest.raises(urllib.error.HTTPError) as exc_401:
                opener.open(req_401, timeout=5.0)
            assert exc_401.value.code == 401

            req_400 = urllib.request.Request(
                f"http://127.0.0.1:{port}/http://alice%3A{cred_pw}%40127.0.0.1:9999/admin%3Ftok%3D{enc_tok}",
                headers={"X-API-Key": api_gate_val},
            )
            with pytest.raises(urllib.error.HTTPError) as exc_400:
                opener.open(req_400, timeout=5.0)
            assert exc_400.value.code == 400

            proc.send_signal(signal.SIGTERM)
            tail, _ = proc.communicate(timeout=10.0)
            full_output = "".join(captured) + tail
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5.0)

        assert "/https://example.com/secret" in full_output
        assert "/http://127.0.0.1:9999/admin" in full_output
        assert cred_pw not in full_output
        assert enc_tok not in full_output
        assert "alice" not in full_output
