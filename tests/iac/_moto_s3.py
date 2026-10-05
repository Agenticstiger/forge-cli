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

"""moto keeps the tags a CreateBucket request carries, as S3 does.

S3's CreateBucket takes tags in its ``CreateBucketConfiguration`` (``<Tags><Tag>``,
added to the API in 2025), and terraform-provider-aws 6.23 and later tag a bucket
that way, skipping ``PutBucketTagging`` when the create succeeds. moto 5.2.3 reads
only ``LocationConstraint`` from that body and drops the tags, so every bucket the
moto lanes apply was left untagged, and a clean apply read back as tag drift
(``tags.fluid_contract``, ``tags.managed_by``). On real S3 the tags are kept: the
lab's eleven AWS products planned no change on provider 6.67.

:func:`keep_create_bucket_tags` wraps moto's bucket PUT so a create also stores the
body's tags, the fix fakecloud made in its own S3 (faiscadev/fakecloud#2557). It
changes nothing when moto is not installed, and is idempotent.
"""

from __future__ import annotations

from typing import Any, Dict


def _create_tags(body: Any) -> Dict[str, str]:
    """The tags of a CreateBucketConfiguration body, or ``{}``."""
    import xmltodict

    if not body:
        return {}
    try:
        cfg = (xmltodict.parse(body) or {}).get("CreateBucketConfiguration") or {}
    except Exception:  # noqa: BLE001 — not a configuration body: no tags
        return {}
    tags = (cfg.get("Tags") or {}).get("Tag") if isinstance(cfg, dict) else None
    if isinstance(tags, dict):
        tags = [tags]
    return {str(t["Key"]): str(t.get("Value") or "") for t in tags or () if t.get("Key")}


def keep_create_bucket_tags() -> bool:
    """Make moto's CreateBucket keep its tags. True when moto is installed and patched."""
    try:
        from moto.s3.responses import S3Response
    except ImportError:
        return False
    original = S3Response._bucket_response_put
    if getattr(original, "_fluid_keeps_create_tags", False):
        return True

    def bucket_response_put(self: Any, request: Any, bucket_name: str, querystring: Any) -> Any:
        result = original(self, request, bucket_name, querystring)
        if not querystring:  # a CreateBucket, not a sub-resource PUT
            tags = _create_tags(self.body)
            if tags:
                self.backend.put_bucket_tagging(bucket_name, tags)
        return result

    bucket_response_put._fluid_keeps_create_tags = True  # type: ignore[attr-defined]
    S3Response._bucket_response_put = bucket_response_put
    return True
