import re
from logging import Logger

from flask.globals import current_app
from flask_restful import Resource, abort
from flask import request, jsonify
from urllib.parse import urljoin

from pbench.server import PbenchServerConfig
from pbench.server.api.resources.query_apis import get_index_prefix


class EndpointConfig(Resource):
    """
    EndpointConfig API resource: this supports dynamic dashboard configuration
    from the Pbench server rather than constructing a disconnected dashboard
    config file.
    """

    forward_pattern = re.compile(r";\s*host\s*=\s*(?P<host>[^;\s]+)")
    x_forward_pattern = re.compile(r"\s*(?P<host>[^;\s,]+)")
    param_template = re.compile(r"<\w+:\w+>")

    def __init__(self, config: PbenchServerConfig, logger: Logger):
        """
        __init__ Construct the API resource

        Args:
            :config: server config values
            :logger: message logging

        Report the server configuration to a web client. By default, the Pbench
        server ansible script sets up a local Apache reverse proxy routing
        through the HTTP port (80); an external reverse-proxy can be configured
        without the knowledge of the server, and this API will use reverse-proxy
        Forwarded or X-Forwarded-Host HTTP headers to discover the proxy
        configuration. All server endpoints will be reported with respect to that
        address.
        """
        self.logger = logger
        self.host = config.get("pbench-server", "host")
        self.uri_prefix = config.rest_uri
        self.prefix = get_index_prefix(config)
        self.commit_id = config.COMMIT_ID

    def get(self):
        """
        Return server configuration information required by web clients
        including the Pbench dashboard UI. This includes:

        indices: Information about the server's ES indices. (NOTE: once
                we've removed all direct Elasticsearch queries from the
                dashboard, these won't be necessary.)
            result_index: The "root" index name for Pbench result data,
                qualified by the current index version and prefix. In the
                current ES schema, this is "v5.result-data-sample."
            result_data_index: The "result-data" index has been broken into
                "result-data-sample" and "result-data" indices for the
                Elasticsearch V7 transition. In the current ES schema, this
                is "v5.result-data."
            run_index: The "master" run-data index root. In the current ES
                schema, this is "v6.run-data."
            run_toc_index: The Elasticsearch V7 index for run TOC data. In
                the current ES schema, this is "v6.run-toc."
        identification: The Pbench server name and version
        api:    A dict of the server APIs supported; we give a name, which
                identifies the service, and the full URI relative to the
                configured host name and port (local or remote reverse proxy).

                This is dynamically generated by processing the Flask URI
                rules; refer to api/__init__.py for the code which creates
                those mappings, or test_endpoint_configure.py for code that
                validates the current set (and must be updated when the API
                set changes). We supplement the Flask API list with the
                "results" API, which is currently just an Apache public_html
                file mapping but is referenced by the dashboard code.

        TODO: We need an internal mechanism to track the active versions of the
        various Elasticsearch template documents. We're hardcoding them here and
        in other APIs. We should consider persisting an equivalent of the
        mapping table built in "indexer.py" for use across the server APIs.

        TODO: We provide Elasticsearch index root names here, which the dashboard
        code needs to perform the queries we've not yet replaced with server-side
        implementations. The entire "indices" section can be removed once that is
        resolved.
        """
        self.logger.debug("Received these headers: {!r}", request.headers)
        origin = None
        host_source = "configuration"
        host_value = self.host
        header = request.headers.get("Forwarded")
        if header:
            m = self.forward_pattern.search(header)
            if m:
                origin = m.group("host")
                host_source = "Forwarded"
                host_value = header
        if not origin:
            header = request.headers.get("X-Forwarded-Host")
            if header:
                m = self.x_forward_pattern.match(header)
                if m:
                    origin = m.group("host")
                    host_source = "X-Forwarded-Host"
                    host_value = header
        if not origin:
            origin = self.host
        host = f"http://{origin}"
        self.logger.info(
            "Advertising endpoints at {} relative to {} ({})",
            host,
            host_source,
            host_value,
        )

        # We pre-load the APIs list with the "results" link, which isn't yet
        # a Pbench server API. The dashboard static endpoint configuration
        # included this, and it makes sense for consistency.
        apis = {"results": urljoin(host, "/results")}

        # Iterate through the Flask endpoints to add a description for each.
        for rule in current_app.url_map.iter_rules():
            url = str(rule.rule)

            # Ignore anything that doesn't use our API prefix, because it's
            # not in our API.
            if url.startswith(self.uri_prefix):
                # If the URI is parameterized with a Flask "<type:name>"
                # template string, we don't want to report it, so we remove
                # it from the URI. We derive an API name by converting the
                # "/" characters in the URI to "_", after removing a trailing
                # "/" that would have been left by removing a template... we
                # don't remove the trailing "/" from the URI, which serves as
                # an indication that the parameter is needed.
                #
                # E.g, "/api/v1/controllers/list" yields:
                #     "controllers_list": "/api/v1/controllers/list"
                #
                # while "/api/v1/users/<string:username>" yields:
                #     "users": "/api/v1/users/"
                #
                # TODO: This won't work right with embedded template strings,
                # which we're not currently using anywhere; but it'll require
                # adjustment later if we add any. (E.g., something like
                # "/api/v1/foo/<string:name>/detail/<string:param>")
                url = self.param_template.sub("", url)
                path = url[len(self.uri_prefix) + 1 :]
                if path.endswith("/"):
                    path = path[:-1]
                path = path.replace("/", "_")
                apis[path] = urljoin(host, url)

        try:
            endpoints = {
                "identification": f"Pbench server {self.commit_id}",
                "indices": {
                    "run_index": f"{self.prefix}.v6.run-data.",
                    "run_toc_index": f"{self.prefix}.v6.run-toc.",
                    "result_index": f"{self.prefix}.v5.result-data-sample.",
                    "result_data_index": f"{self.prefix}.v5.result-data.",
                },
                "api": apis,
            }
            response = jsonify(endpoints)
        except Exception:
            self.logger.exception("Something went wrong constructing the endpoint info")
            abort(500, message="INTERNAL ERROR")
        else:
            response.status_code = 200
            return response
