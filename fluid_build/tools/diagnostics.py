# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import datetime
import os
import platform

from ..auth import doctor as auth_doctor


def doctor(provider: str, project: str = None) -> dict:
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "env": {
            k: os.getenv(k)
            for k in [
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_CLOUD_PROJECT",
                "GCLOUD_PROJECT",
                "FLUID_LOG_LEVEL",
            ]
        },
        "auth": auth_doctor(provider, project),
        "provider": provider,
    }
