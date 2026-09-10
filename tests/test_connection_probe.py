from __future__ import annotations

import logging

from app.connection_probe import SafeConnectionLog


def test_probe_log_never_formats_credentials_or_exception(capsys) -> None:
    handler = SafeConnectionLog()
    secret = "sensitive-value-must-not-appear"
    handler.emit(
        logging.LogRecord(
            "sdk",
            logging.INFO,
            __file__,
            1,
            "connected to %s",
            ("wss://example.test/?ticket=" + secret,),
            None,
        )
    )
    handler.emit(
        logging.LogRecord(
            "sdk",
            logging.ERROR,
            __file__,
            1,
            "request failed: %s",
            (secret,),
            (RuntimeError, RuntimeError(secret), None),
        )
    )
    handler.emit(logging.LogRecord("sdk", logging.DEBUG, __file__, 1, secret, (), None))
    output = capsys.readouterr().out
    assert secret not in output
    assert "wss://" not in output
    assert '"websocket_connected": true' in output
    assert '"collection_enabled": false' in output
    assert '"connection_error": true' in output
