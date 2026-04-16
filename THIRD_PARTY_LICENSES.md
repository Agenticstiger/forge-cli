# Third-Party Licenses

> **Generated:** 2026-03-05 via `pip-licenses 5.5.1`
>
> This document lists all third-party Python packages used by FLUID Forge CLI
> and their respective licenses. All dependencies are compatible with the
> project's Apache License 2.0.

## License Summary

| License Family | Count | Compatible with Apache 2.0? |
|----------------|:-----:|:---------------------------:|
| MIT | 23 | Yes |
| Apache 2.0 | 8 | Yes |
| BSD (2/3-Clause) | 11 | Yes |
| MPL 2.0 | 1 | Yes |
| PSF 2.0 | 1 | Yes |
| Artistic (dual-licensed) | 1 | Yes (see note below) |

**No GPL-only or AGPL dependencies detected.**

### Note on `text-unidecode`

`text-unidecode` v1.3 is dual-licensed under the **Artistic License** or
**GPLv2+**. Under dual licensing, users may choose either license. FLUID Forge
CLI uses `text-unidecode` under the **Artistic License**, which is permissive
and compatible with Apache 2.0. This package is a transitive dependency pulled
in by `python-slugify`.

---

## Direct Dependencies

These are declared in `pyproject.toml` under `[project.dependencies]`:

| Package | Version | License | URL |
|---------|---------|---------|-----|
| PyYAML | 6.0.3 | MIT | https://pyyaml.org/ |
| typing_extensions | 4.15.0 | PSF-2.0 | https://github.com/python/typing_extensions |

> **Note:** Several direct dependencies (`click`, `jsonschema`, `rich`,
> `pydantic`, `jinja2`, `httpx`, `fluid-provider-sdk`) are installed but were
> excluded by `pip-licenses` because they are namespace packages or locally
> installed editable packages. Their licenses are documented below:

| Package | License | URL |
|---------|---------|-----|
| click | BSD-3-Clause | https://github.com/pallets/click |
| jsonschema | MIT | https://github.com/python-jsonschema/jsonschema |
| rich | MIT | https://github.com/Textualize/rich |
| pydantic | MIT | https://github.com/pydantic/pydantic |
| jinja2 | BSD-3-Clause | https://github.com/pallets/jinja |
| httpx | BSD-3-Clause | https://github.com/encode/httpx |
| fluid-provider-sdk | Apache-2.0 | (internal) |

---

## All Installed Packages

| Package | Version | License | URL |
|---------|---------|---------|-----|
| PyJWT | 2.11.0 | MIT | https://github.com/jpadilla/pyjwt |
| PyYAML | 6.0.3 | MIT | https://pyyaml.org/ |
| Pygments | 2.19.2 | BSD | https://pygments.org |
| asn1crypto | 1.5.1 | MIT | https://github.com/wbond/asn1crypto |
| daff | 1.4.2 | MIT | https://github.com/paulfitz/daff |
| fastjsonschema | 2.21.2 | BSD | https://github.com/horejsek/python-fastjsonschema |
| leather | 0.4.1 | MIT | https://leather.readthedocs.io/ |
| numpy | 2.4.2 | BSD-3-Clause | https://numpy.org |
| oauthlib | 3.3.1 | BSD-3-Clause | https://github.com/oauthlib/oauthlib |
| orderly-set | 5.5.0 | MIT | https://github.com/seperman/orderly-set |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| paginate | 0.5.7 | MIT | https://github.com/Signum/paginate |
| parsedatetime | 2.6 | Apache-2.0 | https://github.com/bear/parsedatetime |
| pathspec | 0.12.1 | MPL 2.0 | https://github.com/cpburnz/python-pathspec |
| platformdirs | 4.9.2 | MIT | https://github.com/tox-dev/platformdirs |
| pluggy | 1.6.0 | MIT | https://github.com/pytest-dev/pluggy |
| propcache | 0.4.1 | Apache-2.0 | https://github.com/aio-libs/propcache |
| protobuf | 6.33.5 | BSD-3-Clause | https://developers.google.com/protocol-buffers/ |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| pyarrow | 23.0.1 | Apache-2.0 | https://arrow.apache.org/ |
| pyasn1 | 0.6.2 | BSD-2-Clause | https://github.com/pyasn1/pyasn1 |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| python-slugify | 8.0.4 | MIT | https://github.com/un33k/python-slugify |
| pytimeparse | 1.1.8 | MIT | https://github.com/wroberts/pytimeparse |
| pytokens | 0.4.1 | MIT | https://github.com/tusharsadhwani/pytokens |
| pytz | 2025.2 | MIT | http://pythonhosted.org/pytz |
| rpds-py | 0.30.0 | MIT | https://github.com/crate-py/rpds |
| ruamel.yaml | 0.19.1 | MIT | https://sourceforge.net/p/ruamel-yaml/code/ci/default/tree/ |
| ruff | 0.15.1 | MIT | https://docs.astral.sh/ruff |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| sniffio | 1.3.1 | Apache-2.0 / MIT | https://github.com/python-trio/sniffio |
| sortedcontainers | 2.4.0 | Apache-2.0 | http://www.grantjenks.com/docs/sortedcontainers/ |
| sqlparse | 0.5.4 | BSD | https://github.com/andialbrecht/sqlparse |
| tenacity | 9.1.4 | Apache-2.0 | https://github.com/jd/tenacity |
| text-unidecode | 1.3 | Artistic License *(dual: GPL)* | https://github.com/kmike/text-unidecode/ |
| tomlkit | 0.14.0 | MIT | https://github.com/sdispater/tomlkit |
| traitlets | 5.14.3 | BSD | https://github.com/ipython/traitlets |
| types-PyYAML | 6.0.12.20250915 | Apache-2.0 | https://github.com/python/typeshed |
| typing_extensions | 4.15.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| tzdata | 2025.3 | Apache-2.0 | https://github.com/python/tzdata |
| tzlocal | 5.3.1 | MIT | https://github.com/regebro/tzlocal |
| uc-micro-py | 1.0.3 | MIT | https://github.com/tsutsu3/uc.micro-py |
| urllib3 | 2.6.3 | MIT | https://github.com/urllib3/urllib3 |
| watchdog | 6.0.0 | Apache-2.0 | https://github.com/gorakhargosh/watchdog |
| websockets | 15.0.1 | BSD | https://github.com/python-websockets/websockets |
| zipp | 3.23.0 | MIT | https://github.com/jaraco/zipp |

---

## Regenerating This File

```bash
pip install pip-licenses
pip-licenses --format=markdown --with-urls --order=name > THIRD_PARTY_LICENSES.md
```

Review for GPL/AGPL:
```bash
pip-licenses --format=json | python3 -c "
import json, sys
for p in json.load(sys.stdin):
    if 'GPL' in p['License'] or 'AGPL' in p['License']:
        print(f\"  WARNING: {p['Name']} ({p['License']})\")
" || echo "No GPL/AGPL dependencies found."
```
