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

import datetime
import os
import platform

from ..auth import doctor as auth_doctor


def doctor(provider: str, project: str = None) -> dict:
    return {
        "timestamp": datetime.datetime.utcnow().isoformat(),
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
