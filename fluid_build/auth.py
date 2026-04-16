# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from dataclasses import dataclass
from typing import Optional

try:
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials as SACredentials
except Exception:  # pragma: no cover
    google = None

    class DefaultCredentialsError(Exception):  # pragma: no cover
        """Fallback so except clause doesn't NameError when google-auth missing."""


SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/devstorage.full_control",
]


@dataclass
class GcpAuthResult:
    credentials: object
    project_id: Optional[str]
    mode: str


def _adc():
    if google is None:
        raise RuntimeError(
            "google-auth not installed. Run: pip install google-auth google-cloud-* or use --provider local"
        )
    creds, proj = google.auth.default(scopes=SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return GcpAuthResult(creds, proj, "adc")


def _sa_key(path: str):
    if google is None:
        raise RuntimeError("google-auth not installed.")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Service account key not found: {path}")
    creds = SACredentials.from_service_account_file(path, scopes=SCOPES)
    return GcpAuthResult(creds, creds.project_id, "sa-key")


def _external(path: str):
    if google is None:
        raise RuntimeError("google-auth not installed.")
    if not os.path.exists(path):
        raise FileNotFoundError(f"External account JSON not found: {path}")
    creds, proj = google.auth.load_credentials_from_file(
        path, scopes=SCOPES, quota_project_id=os.getenv("GOOGLE_CLOUD_QUOTA_PROJECT")
    )
    return GcpAuthResult(creds, proj, "external")


def authenticate(mode: Optional[str], credentials_path: Optional[str]) -> GcpAuthResult:
    """
    mode: adc | sa-key | external | None
    """
    mode = mode or "adc"
    try:
        if mode == "adc":
            return _adc()
        elif mode == "sa-key":
            if not credentials_path:
                raise ValueError("Pass --credentials PATH for --auth sa-key")
            return _sa_key(credentials_path)
        elif mode == "external":
            if not credentials_path:
                raise ValueError("Pass --credentials PATH for --auth external")
            return _external(credentials_path)
        else:
            raise ValueError(f"Unknown auth mode: {mode}")
    except DefaultCredentialsError as e:
        raise RuntimeError(
            "No ADC detected. Run `gcloud auth application-default login` or pass --auth sa-key/--credentials"
        ) from e
    except Exception:
        raise


def doctor(provider: str, project: Optional[str]) -> dict:
    info = {
        "provider": provider,
        "project": project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT"),
        "adc_env": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        "quota_project": os.getenv("GOOGLE_CLOUD_QUOTA_PROJECT"),
    }
    return info
