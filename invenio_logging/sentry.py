# SPDX-FileCopyrightText: 2015-2024 CERN.
# SPDX-License-Identifier: MIT

"""Sentry logging module."""

from __future__ import absolute_import, print_function

import gzip
import io
import logging
from types import SimpleNamespace

from flask import g

from . import config
from .ext import InvenioLoggingBase

try:
    import sentry_sdk
    from sentry_sdk.transport import HttpTransport
except ImportError:
    sentry_sdk = None
    HttpTransport = object


class InvenioLoggingSentry(InvenioLoggingBase):
    """Invenio-Logging extension for Sentry."""

    def init_app(self, app):
        """Flask application initialization."""
        self.init_config(app)

        # Only configure Sentry if SENTRY_DSN is set.
        if app.config["SENTRY_DSN"] is None:
            return

        # If SENTRY_DSN is set, check also that sentry-sdk is installed
        if sentry_sdk is None:
            app.logger.warning(
                "The `SENTRY_DSN` config is set, but `sentry-sdk` is not installed. "
                "Please install `sentry-sdk` to use the Sentry logging extension."
            )
            return

        self.install_handler(app)

        app.extensions["invenio-logging-sentry"] = self

        # Set sentry on template context
        def sentry_app_context():
            """Set sentry last event id."""
            g.sentry_event_id = sentry_sdk.last_event_id()
            return {"sentry_event_id": g.sentry_event_id}

        app.context_processor(sentry_app_context)

    def init_config(self, app):
        """Initialize configuration."""
        for k in dir(config):
            if k.startswith("LOGGING_SENTRY") or k.startswith("SENTRY_"):
                app.config.setdefault(k, getattr(config, k))

    def install_handler(self, app):
        """Install log handler."""
        level = getattr(logging, app.config["LOGGING_SENTRY_LEVEL"])
        logging_exclusions = None
        if not app.config["LOGGING_SENTRY_PYWARNINGS"]:
            logging_exclusions = (
                "gunicorn",
                "south",
                "sentry.errors",
                "django.request",
                "dill",
                "py.warnings",
            )

        self.install_sentry_sdk_handler(app, logging_exclusions, level)

        # Werkzeug only adds a stream handler if there's no other handlers
        # defined, so when Sentry adds a log handler no output is
        # received from Werkzeug unless we install a console handler
        # here on the werkzeug logger.
        if app.debug:
            logger = logging.getLogger("werkzeug")
            logger.setLevel(logging.INFO)
            logger.addHandler(logging.StreamHandler())

    def install_sentry_sdk_handler(self, app, logging_exclusions, level):
        """Install sentry-python sdk log handler."""
        # NOTE: It's ok to import these here, as the extension is only loaded once
        from sentry_sdk import configure_scope
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.redis import RedisIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        integrations = [FlaskIntegration()]
        init_kwargs = {}
        if app.config["LOGGING_SENTRY_CELERY"]:
            integrations.append(CeleryIntegration())
        if app.config["LOGGING_SENTRY_SQLALCHEMY"]:
            integrations.append(SqlalchemyIntegration())
        if app.config["LOGGING_SENTRY_REDIS"]:
            integrations.append(RedisIntegration())
        if app.config["LOGGING_SENTRY_INIT_KWARGS"]:
            init_kwargs = app.config["LOGGING_SENTRY_INIT_KWARGS"]

        sentry_sdk.init(
            dsn=app.config["SENTRY_DSN"],
            in_app_exclude=logging_exclusions,
            integrations=integrations,
            before_send=self.add_request_id_sentry_python,
            **init_kwargs,
        )
        with configure_scope() as scope:
            scope.level = level

    def add_request_id_sentry_python(self, event, hint):
        """Add the request id as a tag."""
        if g and hasattr(g, "request_id"):
            tags = event.get("tags") or []
            tags.append(["request_id", g.request_id])
            event["tags"] = tags
        event_id = sentry_sdk.last_event_id()
        if event_id is not None:
            g.sentry_event_id = event_id
        return event


class LegacyStoreTransport(HttpTransport):
    """Route Sentry error events to the legacy ``/store/`` endpoint.

    sentry-sdk 2.x sends only envelopes, which pre-envelope Sentry servers
    (e.g. Sentry 9.x) reject, so every event is silently dropped. This transport
    posts events to the ``/store/`` endpoint those servers understand.

    Enable it in ``invenio.cfg``::

        from invenio_logging.sentry import LegacyStoreTransport

        LOGGING_SENTRY_INIT_KWARGS = {"transport": LegacyStoreTransport}
    """

    # sentry-sdk 2.x's EndpointType enum dropped "store", but Auth.get_api_url()
    # still builds the URL from <endpoint>.value, so this marker resolves to the
    # legacy /store/ URL when passed to _send_request().
    _STORE_ENDPOINT = SimpleNamespace(value="store")

    def _send_envelope(self, envelope):
        """Forward error events in the envelope to the legacy store endpoint."""
        for item in envelope.items:
            if item.type != "event":
                continue
            # Gzip the event and POST it as application/json, as sentry-sdk 1.x's
            # HttpTransport._send_event did (the last version to speak /store/):
            # https://github.com/getsentry/sentry-python/blob/282b8f7fae3da3c3ec26e5ee5e1599fc74661a72/sentry_sdk/transport.py#L376-L414
            body = io.BytesIO()
            with gzip.GzipFile(fileobj=body, mode="w") as fp:
                fp.write(item.get_bytes())
            self._send_request(
                body.getvalue(),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                endpoint_type=self._STORE_ENDPOINT,
                envelope=envelope,
            )
