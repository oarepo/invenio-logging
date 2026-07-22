# SPDX-FileCopyrightText: 2016-2018 CERN.
# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o.
# SPDX-License-Identifier: MIT

"""Sentry logging tests."""

from __future__ import absolute_import, print_function

import gzip
import time
from unittest.mock import patch

import sentry_sdk
from flask import Flask, g
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.flask import FlaskIntegration

from invenio_logging.sentry import InvenioLoggingSentry, LegacyStoreTransport


def test_init():
    """Test initialization."""
    app = Flask("testapp")
    InvenioLoggingSentry(app)
    assert "invenio-logging-sentry" not in app.extensions

    app = Flask("testapp")
    app.config["SENTRY_DSN"] = "http://user:pw@localhost/0"
    InvenioLoggingSentry(app)
    assert "invenio-logging-sentry" in app.extensions

    sentry_global_scope = sentry_sdk.get_global_scope()

    assert sentry_global_scope.client is not None

    if app.config["LOGGING_SENTRY_CELERY"]:
        assert sentry_global_scope.client.get_integration(CeleryIntegration)
    else:
        assert sentry_global_scope.client.get_integration(FlaskIntegration)


def test_sentry_failure():
    """Test that sentry works and logs a failure."""

    app = Flask("testapp")
    app.config["SENTRY_DSN"] = "http://a-secret-hash@127.0.0.1:8000/1"
    InvenioLoggingSentry(app)

    sentry_transport = sentry_sdk.get_global_scope().client.transport

    with patch.object(sentry_transport, "_send_request") as mock_send_request:
        try:
            1 / 0
        except:
            app.logger.exception("Division by zero")

        # wait a bit for sentry-sdk to send the message
        time.sleep(2)

        mock_send_request.assert_called()

    assert sentry_sdk.last_event_id() is not None


def test_legacy_store_transport_hits_store_endpoint():
    """LegacyStoreTransport sends events to the /store/ endpoint."""
    app = Flask("testapp")
    app.config["SENTRY_DSN"] = "http://user:pw@localhost/0"
    app.config["LOGGING_SENTRY_INIT_KWARGS"] = {"transport": LegacyStoreTransport}
    InvenioLoggingSentry(app)

    transport = sentry_sdk.get_global_scope().client.transport
    assert isinstance(transport, LegacyStoreTransport)

    with patch.object(transport, "_send_request") as mock_send_request:
        # An app context is needed because before_send writes g.sentry_event_id.
        with app.app_context():
            try:
                1 / 0
            except ZeroDivisionError:
                app.logger.exception("boom")
        time.sleep(2)
        mock_send_request.assert_called()
        endpoint = mock_send_request.call_args.kwargs["endpoint_type"]
        # resolves to the legacy /store/ URL
        assert getattr(endpoint, "value", endpoint) == "store"


def test_default_transport_is_not_legacy_store():
    """Without opting in, the standard envelope transport is used."""
    app = Flask("testapp")
    app.config["SENTRY_DSN"] = "http://user:pw@localhost/0"
    InvenioLoggingSentry(app)

    transport = sentry_sdk.get_global_scope().client.transport
    assert not isinstance(transport, LegacyStoreTransport)


def test_legacy_store_transport_drops_non_event_items():
    """Only `event` items are forwarded; sessions/etc. are dropped."""
    from sentry_sdk.envelope import Envelope

    app = Flask("testapp")
    app.config["SENTRY_DSN"] = "http://user:pw@localhost/0"
    app.config["LOGGING_SENTRY_INIT_KWARGS"] = {"transport": LegacyStoreTransport}
    InvenioLoggingSentry(app)

    transport = sentry_sdk.get_global_scope().client.transport
    env = Envelope()
    env.add_session({"sid": "x", "status": "ok"})  # a non-event item
    with patch.object(transport, "_send_request") as mock_send_request:
        transport._send_envelope(env)
        mock_send_request.assert_not_called()


def test_request_id_tag_delivered_with_existing_tags():
    """Tagged events are delivered and get a request_id tag.

    Regression: before_send appended to event["tags"], which sentry-sdk 2.x
    represents as a dict once any tag is set. Appending to it raised inside
    before_send, so the event was silently dropped.
    """
    app = Flask("testapp")
    app.config["SENTRY_DSN"] = "http://user:pw@localhost/0"
    InvenioLoggingSentry(app)

    transport = sentry_sdk.get_global_scope().client.transport
    with patch.object(transport, "_send_request") as mock_send_request:
        with app.test_request_context("/"):
            g.request_id = "req-123"
            sentry_sdk.set_tag("existing", "value")  # makes event["tags"] a dict
            try:
                1 / 0
            except ZeroDivisionError:
                app.logger.exception("boom")
        time.sleep(2)
        # Delivered rather than dropped by before_send.
        mock_send_request.assert_called()
        payload = gzip.decompress(mock_send_request.call_args.args[0]).decode()
        assert '"request_id":"req-123"' in payload
        assert '"existing":"value"' in payload
