# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Read-only registry clients: the Confluent REST API and the Glue Schema Registry.

Both read; neither registers. Registration is OpenTofu's (``iac/providers/``).

* :class:`ConfluentRegistry` speaks the Confluent Schema Registry v1 REST API, which
  Confluent Platform and Cloud, Karapace, Redpanda and Apicurio's ccompat endpoint all
  serve. It goes through ``safe_httpx_client`` (SSRF guard, DNS pinning, no redirects)
  plus the deployment host policy's request guard, which judges the address each
  request is pinned to. ``httpx`` is imported on first use.
* :class:`GlueRegistry` calls ``glue:GetSchemaVersion`` through boto3, imported on
  first use from the ``[aws]`` extra. It authenticates as the caller's IAM identity.

Errors are :class:`RegistryError`, which quotes the registry's own message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urlsplit

_ACCEPT = "application/vnd.schemaregistry.v1+json"
_MESSAGE_LIMIT = 500


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class RegistryError(Exception):
    """A registry request failed. ``code`` is the registry's error code, when it gave one.

    The message quotes the registry, so control characters (a terminal escape in an
    error body) are replaced before it is printed anywhere.
    """

    def __init__(self, message: str, *, code: Optional[Union[int, str]] = None) -> None:
        super().__init__(_CONTROL.sub("?", message))
        self.code = code


@dataclass(frozen=True)
class SubjectVersion:
    """One version of a subject: its number, schema type and schema text."""

    subject: str
    version: int
    data_format: str  # AVRO | JSON | PROTOBUF
    schema: str
    schema_id: Optional[str] = None


def _segment(text: Union[str, int]) -> str:
    return quote(str(text), safe="")


class ConfluentRegistry:
    """A Confluent-API registry at ``url``.

    ``credential`` is the resolved ``user:password`` (an API key and secret), sent as
    HTTP Basic auth, the way Confluent's own clients send ``basic.auth.user.info``.
    Only ``fluid apply`` passes one, after the secrets policy cleared the host
    (``verify.py::verify_at_apply``); the client then refuses a non-https URL, except
    on loopback.
    """

    def __init__(
        self, url: str, *, timeout: float = 10.0, credential: Optional[str] = None
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._credential = credential
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            from fluid_build.build_runners._deployment_host_policy import request_guard
            from fluid_build.secrets.policy import is_loopback
            from fluid_build.util.safe_http import safe_httpx_client

            headers = {"Accept": _ACCEPT}
            if self._credential is not None:
                import base64

                token = base64.b64encode(self._credential.encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            host = urlsplit(self.url).hostname
            self._client = safe_httpx_client(
                base_url=self.url.rstrip("/") + "/",
                allow_private=True,
                require_https=self._credential is not None and not is_loopback(host),
                timeout=self.timeout,
                headers=headers,
                event_hooks={"request": [request_guard(self.url, engine="schema registry")]},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "ConfluentRegistry":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        from fluid_build._errors import FluidUserError

        try:
            response = self._http().request(method, path, **kwargs)
        except FluidUserError as exc:  # the host policy refused the pinned address
            raise RegistryError(f"{exc.what}: {exc.why}") from None
        except Exception as exc:  # noqa: BLE001 - httpx, DNS and SSRF errors alike
            raise RegistryError(
                f"{self.url} could not be reached ({type(exc).__name__}: {exc})"
            ) from None
        if response.status_code >= 400:
            code: Optional[Union[int, str]] = response.status_code
            message = response.text
            try:
                body = response.json()
                code = body.get("error_code", code)
                message = str(body.get("message", message))
            except (ValueError, AttributeError):
                pass
            raise RegistryError(
                f"HTTP {response.status_code} ({code}): {message[:_MESSAGE_LIMIT]}", code=code
            )
        try:
            body = response.json()
        except ValueError:
            raise RegistryError(f"{self.url} answered {path} with no JSON") from None
        if not isinstance(body, (dict, list)):
            raise RegistryError(f"{self.url} answered {path} with JSON that is no object")
        return body

    def _object(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        body = self._call(method, path, **kwargs)
        if not isinstance(body, dict):
            raise RegistryError(f"{self.url} answered {path} with JSON that is no object")
        return body

    def get_version(self, subject: str, version: Union[str, int] = "latest") -> SubjectVersion:
        """``GET /subjects/{subject}/versions/{version}``."""
        body = self._object("GET", f"subjects/{_segment(subject)}/versions/{_segment(version)}")
        return SubjectVersion(
            subject=str(body.get("subject", subject)),
            version=int(body["version"]),
            data_format=str(body.get("schemaType") or "AVRO"),
            schema=str(body["schema"]),
            schema_id=str(body.get("id")) if body.get("id") is not None else None,
        )

    def check_compatibility(
        self, subject: str, version: Union[str, int], schema: str, data_format: str
    ) -> Tuple[bool, List[str]]:
        """``POST /compatibility/subjects/{subject}/versions/{version}?verbose=true``.

        The registry judges ``schema`` against ``version`` under the subject's own
        compatibility mode, and registers nothing.
        """
        body = self._object(
            "POST",
            f"compatibility/subjects/{_segment(subject)}/versions/{_segment(version)}",
            params={"verbose": "true"},
            headers={"Content-Type": _ACCEPT},
            json={"schema": schema, "schemaType": data_format},
        )
        messages = body.get("messages") or []
        return bool(body.get("is_compatible")), [_CONTROL.sub("?", str(m)) for m in messages]

    def compatibility_level(self, subject: str) -> Optional[str]:
        """The subject's mode, falling back to the registry's global one."""
        for path in (f"config/{_segment(subject)}", "config"):
            try:
                body = self._object("GET", path, params={"defaultToGlobal": "true"})
            except RegistryError as exc:
                if exc.code in (404, 40401, 40408):
                    continue
                raise
            level = body.get("compatibilityLevel") or body.get("compatibility")
            if level:
                return str(level)
        return None

    def subjects(self) -> List[str]:
        """``GET /subjects``."""
        return [str(s) for s in self._call("GET", "subjects")]


class GlueRegistry:
    """The Glue Schema Registry of the caller's AWS account and region (IAM, no secret)."""

    def __init__(self, *, region: Optional[str] = None, client: Any = None) -> None:
        self.region = region
        self._client = client

    def _glue(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise RegistryError(
                    "reading the Glue Schema Registry needs boto3: "
                    "pip install 'data-product-forge[aws]'"
                ) from None
            self._client = boto3.client("glue", region_name=self.region)
        return self._client

    def get_version(
        self, registry: str, schema: str, version: Union[str, int] = "latest"
    ) -> SubjectVersion:
        """``glue:GetSchemaVersion`` for ``registry``/``schema`` at ``latest`` or N."""
        number = {"LatestVersion": True} if version == "latest" else {"VersionNumber": int(version)}
        try:
            body = self._glue().get_schema_version(
                SchemaId={"RegistryName": registry, "SchemaName": schema},
                SchemaVersionNumber=number,
            )
        except RegistryError:
            raise
        except Exception as exc:  # noqa: BLE001 - botocore ClientError, credentials, network
            response = getattr(exc, "response", None) or {}
            error = response.get("Error") or {}
            code = error.get("Code") or type(exc).__name__
            message = error.get("Message") or str(exc)
            raise RegistryError(f"{code}: {message[:_MESSAGE_LIMIT]}", code=code) from None
        if body.get("Status") not in (None, "AVAILABLE"):
            raise RegistryError(
                f"schema version {body.get('VersionNumber')} is {body.get('Status')}, not AVAILABLE"
            )
        return SubjectVersion(
            subject=schema,
            version=int(body["VersionNumber"]),
            data_format=str(body.get("DataFormat") or "AVRO"),
            schema=str(body["SchemaDefinition"]),
            schema_id=body.get("SchemaVersionId"),
        )

    def schemas(self, registry: str) -> List[str]:
        """The schema names in ``registry`` (``glue:ListSchemas``, paginated)."""
        names: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs: dict = {"RegistryId": {"RegistryName": registry}}
            if token:
                kwargs["NextToken"] = token
            try:
                page = self._glue().list_schemas(**kwargs)
            except Exception as exc:  # noqa: BLE001
                error = (getattr(exc, "response", None) or {}).get("Error") or {}
                raise RegistryError(
                    f"{error.get('Code') or type(exc).__name__}: {error.get('Message') or exc}"
                ) from None
            names.extend(str(s["SchemaName"]) for s in page.get("Schemas") or [])
            token = page.get("NextToken")
            if not token:
                return names


def parse_schema(text: str) -> Any:
    """A schema's text as JSON; a registry's Avro or JSON Schema is always JSON."""
    try:
        return json.loads(text)
    except ValueError:
        raise RegistryError("the subject's schema is not JSON") from None
