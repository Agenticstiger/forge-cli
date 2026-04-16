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

"""
AWS Provider for FLUID Build - Enterprise Cloud Data Platform Integration.

Now includes comprehensive implementation with:
- S3, Glue, Athena, Redshift support
- Event-driven orchestration (EventBridge, Lambda, Step Functions)
- IAM integration and security
- Complete planning and execution engine
"""

# Import old provider for backward compatibility
try:
    from .aws import AWSProvider as LegacyAWSProvider
except ImportError:
    LegacyAWSProvider = None

# Import new comprehensive provider
from .provider import AwsProvider

# Import types if available
try:
    from .types import (
        AWSProviderOptions,
        CostOptimizationConfig,
        MonitoringConfig,
        SecurityConfig,
        ServiceConfig,
    )

    _types_available = True
except ImportError:
    _types_available = False

# Register the new provider
from fluid_build.providers import register_provider

register_provider("aws", AwsProvider)

# Export
if _types_available:
    __all__ = [
        "AwsProvider",
        "LegacyAWSProvider",
        "AWSProviderOptions",
        "ServiceConfig",
        "SecurityConfig",
        "MonitoringConfig",
        "CostOptimizationConfig",
    ]
else:
    __all__ = ["AwsProvider", "LegacyAWSProvider"]
