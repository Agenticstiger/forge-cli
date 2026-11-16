# RFC: Rules about valid data: invariants, identifier formats, placeholders and shared attributes

**Status:** Proposed (2026-11-16) · **Target schema:** 0.7.7 (preview, per `schema_manager.PREVIEW_VERSIONS`) · **Scope:** `$defs.exposeContract` (`invariants`), `$defs.column` (`identifierFormat`, `placeholders`, `sharedAttribute`), `$defs.semanticRelationship` (`sharedColumns`), one expression language (FLUID SQL/1), and five consumers: the validator, the dbt bridge, the ODCS exporter, the MCP query compiler and FLUX

**Related:** `RFC-semantics-tier1.md` (#445; its §4 relationships and §5 validity windows landed in #894) · `RFC-data-profile-port.md` (#845; measured statistics stay out of the contract) · flux-engine ADR 0008 and `docs/spec/EXPR-SPEC-2.md` (Agenticstiger/flux-engine#38), which implement §2 · FLUX relational data requirements v3, §5.1 (REL-9), §5.3 (COL-4, COL-7) and §5.5 (DQ-3)

---

## Motivation

FLUX is to generate a synthetic copy of a whole multi-table source from its FLUID contracts. The copy is only realistic if it keeps the relations between columns that the real data keeps. Gross amounts follow from net amounts and tax rates. Dates come in order. Ages follow from birth dates. An id determines its name. A total is the sum of its lines. A flag agrees with the value it summarizes. An attribute that two tables carry has one value. Identifiers have their source's format, and some codes are placeholders.

On 2026-11-16 the founder decided where these rules live:

- **Decision 12.** Rules about what valid data looks like belong in the FLUID contract. There, dbt, ODCS and catalog consumers reuse them, and FLUX reads them instead of restating them. Distributions, behavior and run settings stay in FLUX documents. Measured statistics stay in separate profile documents (`RFC-data-profile-port.md`).
- **Decision 13.** Invariants are written in a SQL expression subset that this RFC defines. flux-expr/2 implements exactly that subset.
- **Decision 14.** Date arithmetic is whole-unit differences (`YEARS_BETWEEN`, `MONTHS_BETWEEN`, `DAYS_BETWEEN`) with an explicit as-of date. The results are exact integers.

The 0.7.7 schema cannot say any of this today:

| Need | Closest field in 0.7.7 | Why it does not fit |
|---|---|---|
| A derived column (`gross` from `net` and `tax_rate`) | `contract.quality[].expression` | A check in any SQL dialect, with no scope and no share. Nothing says which column it determines, so a generator cannot use it. |
| A rule through a link (`speed_mbps > product.speed_threshold_mbps`) | none | Every rule sees one table. |
| A functional dependency, or a total over child rows | none | |
| A rule that holds on 98 % of rows | `dq.rules[].threshold` | Typed rules only (freshness, completeness and so on), not expressions. |
| An identifier format (`TWR` and 5 digits) | `validationRules` with `type: regex` | A regex can be checked but not enumerated, so a generator cannot produce unique values from it. |
| Placeholder codes (`TBD_*`) | column `quality[]` with `missingValues` | ODCS `missingValues` takes a list of values only. ODCS `invalidValues` with `arguments.pattern` describes the valid values, so a placeholder would need a negated pattern, and nothing can generate from one. |
| An attribute that two tables carry | column `semanticType` | Free text. Two columns marked `email` are not one attribute. |

Without new fields, FLUX would restate these rules in its own bundle, where dbt and ODCS consumers cannot see them, and the two copies of each rule would drift apart.

Every shape below is borrowed:

- derived columns, from PostgreSQL generated columns;
- shares, from ODCS v3.1.0's `mustBe*` operators and `unit`, and the share-of-rows check from Deequ's `satisfies`;
- functional-dependency counts, from the g3 measure of approximate dependencies (Kivinen and Mannila, used by TANE);
- summaries, from SQL aggregates with SQL's null rules and the aggregate `FILTER` clause;
- masks, from COBOL PICTURE strings;
- the placeholder exemption, from Frictionless Table Schema `missingValues`;
- whole-unit date differences, from DuckDB's `date_sub`.

ODCS v3.2.0 (released 2026-09-08), the Apache Ossie core spec (0.3.0.dev0) and Snowflake semantic views were checked too. None of them has invariants, masks, placeholder patterns or attribute equivalence, so FLUID defines them here, as close to those shapes as it can.

## Design overview

**Additive and preview-gated.** Every new field is optional. Each lands only in `fluid-schema-0.7.7.json`, the open preview that #894 used, and carries the "NEW in v0.7.7" description marker. A contract without the new fields validates, exports and digests exactly as today. Every touched object keeps `additionalProperties: false`.

**One expression language, FLUID SQL/1 (§2).** It is spelled as SQL, so dbt, ODCS and catalog consumers can read it. It is closed, so every consumer reaches the same verdict on the same row. Its numbers are exact. flux-expr/2 implements it exactly (decision 13). Windows, aggregates written inside expressions, the previous version of an entity and paths over several links stay FLUX-only extensions.

**One declaration, two uses.** A check says which rows are valid. A derived column says what a column's value is. Consumers check both, and FLUX will also generate from them.

**Consumers follow tier-1's posture split.**

- The validator is strict about references and types.
- The dbt bridge renders exactly, or skips with a logged reason. It never emits a test whose verdict differs from FLUID's.
- The ODCS exporter renders executable quality rules from the parsed expression, never from the raw text.
- The MCP compiler treats everything here as information.
- FLUX will refuse, with an FLX code, any declaration it cannot honor.

Where each kind sits:

| Kind | Field path (all new in 0.7.7) |
|---|---|
| Arithmetic within a row, values as of a date, flags that agree with values | `exposes[].contract.invariants[]`: `derive` and `expr` |
| Date order, and any other relation that is only checked | `exposes[].contract.invariants[]`: `check` |
| Summaries across tables | `exposes[].contract.invariants[]`: `derive` and `aggregate`, on the parent expose |
| Functional dependencies | `exposes[].contract.invariants[]`: `dependency` |
| Expected share of rows | `exposes[].contract.invariants[]`: one `mustBe*` operator and `unit` |
| Identifier formats | `exposes[].contract.schema[].identifierFormat[]` |
| Placeholder values | `exposes[].contract.schema[].placeholders` |
| Shared attributes through one link | `exposes[].semantics.relationships[].sharedColumns[]` |
| Shared attributes across several links | `exposes[].contract.schema[].sharedAttribute` |

`contract.quality[]` keeps its meaning: free SQL checks in the warehouse's dialect, exported to ODCS and never read by FLUX. An expression that needs SQL outside FLUID SQL/1 belongs there.

---

## 1. Invariants (DQ-3)

### 1.1 Shape

```yaml
fluidVersion: 0.7.7
exposes:
  - exposeId: invoices
    contract:
      schema:
        - {name: invoice_id, type: string, required: true}
        - {name: contract_id, type: string}
        - {name: net, type: "decimal(12,2)"}
        - {name: tax_rate, type: "decimal(5,4)"}
        - {name: gross, type: "decimal(12,2)"}
        - {name: billed_base_fee, type: "decimal(12,2)"}
        - {name: issue_date, type: date}
        - {name: due_date, type: date}
      invariants:                                   # NEW in 0.7.7
        - id: gross_from_net                        # arithmetic within a row
          derive: gross
          expr: "ROUND(net * (1 + tax_rate), 2)"
        - id: due_after_issue                       # date order, on 99.5 % of rows or more
          check: "issue_date <= due_date"
          mustBeLessOrEqualTo: 0.5
          unit: percent
        - id: billed_base_fee_from_lines            # a summary across tables
          derive: billed_base_fee
          aggregate:
            function: sum
            column: amount
            from: invoice_lines
            via: invoice
            filter: "kind = 'base_fee'"
            ifNone: 0
        - id: base_fee_is_monthly_fee               # through a link
          check: "billed_base_fee = contract.monthly_fee"
    semantics:
      relationships:
        - {name: contract, to: contracts, fromColumns: [contract_id], toColumns: [contract_id], cardinality: many_to_one}

  - exposeId: subscribers
    contract:
      invariants:
        - id: age_at_snapshot                       # a value as of a date
          derive: age
          expr: "YEARS_BETWEEN(birth_date, snapshot_date)"
        - id: activation_order                      # date order, holding on about 98 % of rows
          check: "activation_date <= first_use_date AND first_use_date <= min_term_end"
          when: "first_use_date IS NOT NULL"
          mustBeBetween: [1.5, 2.5]
          unit: percent
        - id: fast_flag                             # a flag that agrees with a value
          derive: is_fast
          expr: "speed_mbps > product.speed_threshold_mbps"
        - id: group_name_by_id                      # a functional dependency
          dependency: {from: [group_id], to: [group_name], bothWays: true}
```

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | `^[A-Za-z][A-Za-z0-9_]*$`, unique within the contract, across all exposes and kinds. It names the rule in reports, tests and ODCS rules, and FLUX keys the rule's seed streams by it. |
| `description` | no | prose |
| `check` | one form | a FLUID SQL/1 predicate over the row (§2) |
| `derive` | one form | the column the invariant determines, with exactly one of `expr` and `aggregate` |
| `expr` | with `derive` | the column's value, a FLUID SQL/1 expression over the row |
| `aggregate` | with `derive` | the column's value, a summary over child rows (§1.2) |
| `dependency` | one form | `{from, to, bothWays}`: a functional dependency (§1.2) |
| `when` | no | a FLUID SQL/1 predicate: the rows the invariant covers (§1.3); not on a `dependency` in v1, because flux-expr/2's dependencies have no scope |
| `severity` | no | `error` (the default), `warning` or `info`, the words `contract.quality[]` uses |
| one `mustBe*` operator, `unit` | no | the expected number of violating rows (§1.4); without one, `mustBe: 0` |

An entry has exactly one form: `check`; `derive` with `expr`; `derive` with `aggregate`; or `dependency`. The schema enforces this with `oneOf`.

### 1.2 The six kinds

| Kind (DQ-3) | Written as | Example |
|---|---|---|
| Arithmetic within a row | `derive` and `expr` | `derive: gross`, `expr: "ROUND(net * (1 + tax_rate), 2)"` |
| Date order | `check` | `"activation_date <= first_use_date AND first_use_date <= min_term_end"` |
| Values as of a date | `derive` and `expr` | `derive: age`, `expr: "YEARS_BETWEEN(birth_date, snapshot_date)"` |
| Functional dependencies | `dependency` | `{from: [group_id], to: [group_name], bothWays: true}` |
| Summaries across tables | `derive` and `aggregate`, then a `check` through a link | an invoice's billed base fee is the sum of its base-fee lines, and equals its contract's monthly fee |
| Flags that agree with values | `derive` and `expr` | `derive: is_fast`, `expr: "speed_mbps > product.speed_threshold_mbps"` |

- **Arithmetic.** `derive` and `expr` are a generated column in PostgreSQL's sense (`GENERATED ALWAYS AS (expression)`). The value comes from the row and from the rows its links reach. It must fit the column's scale, so a product or a quotient goes through `ROUND` (§2.4). Unlike PostgreSQL, one derived column may read another: the validator refuses only cycles.
- **Date order.** A `check`. A row on which a compared date is NULL violates it (§2.5). So `when: "first_use_date IS NOT NULL"` keeps subscribers who never used the service out of the count.
- **Values as of a date.** The as-of date is a column (`snapshot_date`) or a literal (`DATE '2026-11-16'`), never the clock: no FLUID SQL/1 function reads it (decision 14). Age and tenure are whole units and exact integers.
- **Functional dependencies.** A structure, not an expression: equal values in `from` mean equal values in `to`. `bothWays: true` adds the reverse, so the map is one-to-one; it takes one column on each side. When the `from` columns are the key of a linked table, a copy through the link says more. Then write `derive` with `expr: "<relationship>.<column>"`, or `sharedColumns` (§5), instead.
- **Summaries across tables.** Declared on the parent. The summary is a structure, not an aggregate written inside an expression; aggregates inside expressions stay FLUX-only.

  | `aggregate` key | Meaning |
  |---|---|
  | `function` | `count`, `sum`, `min` or `max` |
  | `column` | the child column summarized: none for `count`, a number of bounded scale for `sum` (§2.4), any orderable type for `min` and `max` |
  | `from` | the child expose |
  | `via` | a relationship that the child declares, whose `to` is this expose |
  | `filter` | optional: a FLUID SQL/1 predicate over the child row, as in SQL's `SUM(x) FILTER (WHERE ...)`; only children for which it is TRUE count |
  | `ifNone` | optional: a literal that replaces a NULL summary, as `COALESCE` would: when no child counts, or when every counted child's `column` is NULL. Without it, SQL's rules apply: `sum`, `min` and `max` of nothing are NULL, and `count` is 0. |

  `count` has scale 0, `sum` has its column's scale, and `min` and `max` have their column's type.
- **Flags.** A comparison with NULL is FALSE (§2.5), so a flag derived from a NULL speed is `FALSE`.

### 1.3 Which rows an invariant covers

- A row is covered when `when` is TRUE. Without `when`, every row is covered.
- Several `derive` invariants may determine one column, each with its own `when`. A row is covered by the first one, in declaration order, whose `when` holds, as in SQL's `CASE WHEN`. Each invariant is checked on the rows it covers. A row that none covers takes the column's other source: its own value in real data, or its own statistics in a FLUX run.
- A path through a relationship whose foreign key is NULL, or matches no row, gives NULL for every column it reads.
- A summary covers parent rows. A dependency does not cover a row with a NULL in a `from` column.
- No other row is left out. A row on which a compared value is NULL is covered, and it usually violates the invariant (§2.5); write `when` to leave such rows out. EXPR-SPEC-2 §7 leaves out rows with a NULL bound by itself, and for rules read from FLUID it does not (§2.9, change 5), so FLUX and SQL consumers count the same rows.

### 1.4 Expected share of rows

An invariant's share is a threshold on the number of covered rows that violate it. It is written with ODCS v3.1.0's operators and `unit`, which ODCS v3.2.0 keeps.

| Operator | The invariant holds when the violating rows are |
|---|---|
| `mustBe: n` | exactly n |
| `mustBeLessThan: n`, `mustBeLessOrEqualTo: n` | fewer than n, or at most n |
| `mustBeGreaterThan: n`, `mustBeGreaterOrEqualTo: n` | more than n, or at least n |
| `mustBeBetween: [lo, hi]` | from lo to hi, both included, with lo below hi |

- At most one operator per invariant. Without one, the invariant holds only when no covered row violates it (`mustBe: 0`). ODCS's `mustNotBe` and `mustNotBeBetween` are not accepted, because neither describes a band to generate within.
- `unit: rows` (the default) counts rows. `unit: percent` is violating rows times 100 divided by covered rows, and 0 when no row is covered.
- The numbers are exact decimals: a consumer reads `0.5` as one half, from its text, never as the nearest binary double. They have at most 15 significant digits, so the text survives a round trip through a double (FLUX decision Q4).
- "Holds on about 98 % of real rows" is `mustBeBetween: [1.5, 2.5], unit: percent`.

The band is a rule; the measured share is a statistic. `fluid profile` (decision 11) measures each declared invariant on the real data and writes the result to a profile document. Because zero violations are disclosive, it reports "at least X %" (decision Q9). The contract never holds a measured number. FLUX will choose where in the band a run generates (a FLUX setting, which may come from a profile), inject the violations, and check the run against the band. FLUX will check a run against the band itself, as every consumer does (§2.9, change 7), and generate from a `percent` band by choosing a target strictly inside it. Three bands cannot be met in expectation: a zero-width `percent` band above 0 (`mustBe: 2`), a `rows` band other than zero violations (a count does not scale to a run of another size), and a dependency band that allows a holding share at or below one half (EXPR-SPEC-2 §9). FLUX will check such a band, generate no violation for it, and, under strictness, refuse one that zero violations would fail.

### 1.5 What a check counts

| Form | A covered row violates the invariant when |
|---|---|
| `check` | the predicate is not TRUE; FLUID SQL/1 is two-valued (§2.5), so it is FALSE |
| `derive` and `expr` | the column `IS DISTINCT FROM` the value, so a NULL value and a NULL column agree, and a copied NULL holds |
| `derive` and `aggregate` | the parent's column `IS DISTINCT FROM` the summary over its children |
| `dependency` | the row does not carry its key's most common `to` value |

For a dependency, the violating rows of one key are its covered rows minus the rows that carry its most common `to` tuple. A NULL in a `to` column is a value. Ties do not change the count. This is the g3 measure (Kivinen and Mannila): the fewest rows to remove so that the dependency holds. With `bothWays`, the forward and the reverse dependency are counted separately, and each count must meet the threshold. The forward count covers the rows whose `from` columns are not NULL, and there a NULL `to` is a value. The reverse count covers the rows whose `from` and `to` columns are both not NULL, so it never counts a row that the forward count leaves out.

This is what dbt, the ODCS exporter, `fluid profile` and FLUX's own check compute. Deequ's `satisfies` makes the same choice at the top level: a row whose predicate is NULL does not satisfy it.

### 1.6 What FLUX reads, and what stays in FLUX

FLUX will read every invariant, format, placeholder declaration and shared attribute of the contracts that a Simulation pins, through the contract digest. Changing any of them changes the digest, and the plan refuses until the Simulation pins the new digest (REL-2's rule). EXPR-SPEC-2 §1 left open where rules are declared and which digest covers them. This RFC answers it: the FLUID contract, under the contract digest.

What stays in FLUX documents:

- which column a check generates, for the bounds and difference forms of EXPR-SPEC-2 §7;
- where in a band a run generates;
- how violating values are drawn (`violation`);
- prefix weights, and prefixes taken from a linked row;
- placeholder rates, and the text after a placeholder prefix;
- distributions, and every run setting.

FLUX will refuse, with an FLX code, an invariant it cannot generate as declared: two sources, a cycle, or nothing to draw from. A check it cannot generate from will still be checked on every run.

---

## 2. FLUID SQL/1, the expression subset

FLUID SQL/1 is the language of `check`, `expr`, `when` and `aggregate.filter`. It is a closed subset of SQL: a text the grammar does not derive is a validation error, never an extension point. Growth goes to FLUID SQL/2 in a later schema version. There is no per-expression language key, because the contract's `fluidVersion` names the version: 0.7.7 carries FLUID SQL/1.

### 2.1 Grammar

```text
expr        = or_expr ;
or_expr     = and_expr , { "OR" , and_expr } ;
and_expr    = not_expr , { "AND" , not_expr } ;
not_expr    = { "NOT" } , predicate ;
predicate   = value , [ cmp_op , value ]
            | value , [ "NOT" ] , "IN" , "(" , in_item , { "," , in_item } , ")"
            | value , [ "NOT" ] , "BETWEEN" , value , "AND" , value
            | value , "IS" , [ "NOT" ] , "NULL"
            | value , "IS" , [ "NOT" ] , "DISTINCT" , "FROM" , value ;
cmp_op      = "=" | "<>" | "!=" | "<" | "<=" | ">" | ">=" ;
value       = term , { ( "+" | "-" ) , term } ;
term        = factor , { ( "*" | "/" ) , factor } ;
factor      = [ "-" ] , primary ;
primary     = literal | column_ref | call | case_expr | "(" , expr , ")" ;
call        = "ROUND" , "(" , value , "," , integer , [ "," , round_mode ] , ")"
            | "COALESCE" , "(" , expr , "," , expr , { "," , expr } , ")"
            | date_diff , "(" , value , "," , value , ")" ;
date_diff   = "DAYS_BETWEEN" | "MONTHS_BETWEEN" | "YEARS_BETWEEN" ;
round_mode  = "'HALF_AWAY_FROM_ZERO'" | "'HALF_TO_EVEN'" ;
case_expr   = "CASE" , "WHEN" , expr , "THEN" , expr ,
              { "WHEN" , expr , "THEN" , expr } , [ "ELSE" , expr ] , "END" ;
column_ref  = name , [ "." , name ] ;
literal     = number | string | "TRUE" | "FALSE" | "NULL" | "DATE" , string ;
in_item     = [ "-" ] , number | string | "TRUE" | "FALSE" | "DATE" , string ;
integer     = digit , { digit } ;
(* name, number and string are lexical: see below *)
```

- Keywords and function names are case-insensitive. A name is case-sensitive and names a column exactly as `contract.schema[].name` spells it. A name that is a keyword, or that does not match `[A-Za-z_][A-Za-z0-9_]*`, is double-quoted (`"order"`, `"Net Amount"`), with `""` for a quote inside it.
- Strings are single-quoted, with `''` for a quote inside. A number is digits with an optional fraction (`3`, `1.50`); there is no exponent, and a leading `-` is negation. The digits after the point are the literal's scale.
- An `IN` list holds literals of one type. The `NULL` literal is never an `IN` item or the operand of a comparison: `x = NULL` would always be FALSE (§2.5), so the validator refuses it and points to `x IS NULL`.
- Whitespace is space, tab and newline. The text holds no `;`, and `--` and `/*` are refused wherever they stand outside a string literal, before parsing: SQL reads them as comments (sqlglot reads `net--1 > 0` as `net`). A double negation is written `net - -1`.
- The text is NFC-normalized before it is parsed. It is at most 4,096 code points long and nests at most 64 levels deep, the limits of EXPR-SPEC-2 §3.
- Precedence, tightest first: unary `-`; `*` and `/`; `+` and `-`; comparisons, `IN`, `BETWEEN` and `IS`; `NOT`; `AND`; `OR`. Comparisons do not chain.

### 2.2 Names and the row scope

An expression is evaluated for one row of the expose that declares it.

- A bare name is a column of that expose's `contract.schema`.
- `<relationship>.<column>` is a column of the row that a relationship reaches. The relationship is one of `semantics.relationships[]` on this expose, matched by `name`, and the column is in its target's schema. Both cardinalities reach at most one row. A NULL foreign key, or one that matches no row, reaches none, and the path is NULL. A path, a summary, a `sharedColumns` entry and a `sharedAttribute` hop never go through a relationship whose target declares a validity window. #894 says that the row reached is the version valid at the declaring row's point in time, but nothing yet names the declaring row's time column, and a plain key join would return one row per version. This is a validation error in v1, until a later design names that column.
- A path crosses one link. Paths over several links (`account.group.segment`) are FLUX-only; across tables, `sharedAttribute` (§5) covers the common case.
- In `aggregate.filter`, names resolve against the child's row: its columns and its own relationships.
- Nested `fields`, and columns of a type with no FLUID SQL/1 type (§2.3), cannot be named.

### 2.3 Types

Six types: bool, number, string, date, timestamp, and timestamp with time zone. NULL is a value of every type, not a type of its own.

| FLUID column type | FLUID SQL/1 type |
|---|---|
| `boolean`, `bool`, `bit` | bool |
| `int`, `integer`, `int2` to `int64`, `tinyint`, `smallint`, `mediumint`, `bigint`, `long`, `longint`, `serial`, `bigserial` | number, scale 0 |
| `decimal(p,s)`, `dec(p,s)`, `numeric(p,s)`, `number(p,s)`, `bignumeric(p,s)` | number, scale s |
| `float`, `float4`, `float8`, `float32`, `float64`, `double`, `double precision`, `real` | number, read as the shortest decimal that converts back to the same double, so `f = 0.1` holds where `f` stores 0.1, as engines compare it (FLUX's decimal-backed floats, decision Q4, keep at most 15 significant digits); never a `derive` target |
| `string`, `text`, `varchar`, `varchar2`, `nvarchar`, `char`, `nchar`, `character`, `clob`, `uuid`, `uniqueidentifier`, `guid`, `enum` | string |
| `date` | date |
| `datetime`, `datetime2`, `smalldatetime`, `timestamp`, `timestamp_ntz`, `timestampntz` | timestamp (no time zone) |
| `timestamptz`, `timestamp_tz`, `timestamp_ltz` | timestamp with time zone (an instant) |
| every other type (`time`, `interval`, `money`, `variant`, `json`, `array`, `struct`, `map`, the binary and spatial types and so on) | none: an expression cannot name the column |

A `decimal`-family column without `(p,s)` has no declared scale. An expression can read it, but a `derive` target needs `(p,s)`.

Type rules:

- `=`, `<>`, `!=`, `IS [NOT] DISTINCT FROM` and `IN` take operands of one type. Numbers of different scales compare by value.
- `<`, `<=`, `>`, `>=` and `BETWEEN` take numbers, strings, dates or timestamps, one type throughout. Strings compare by the code points of the text as stored; only the contract's own texts (names, literals, placeholder values and prefixes) are NFC-normalized. A date never meets a timestamp, and a timestamp never meets a timestamp with time zone, because FLUID SQL/1 has no `CAST` and the two readings of a zoneless value differ by engine.
- `+`, `-`, `*`, `/` and unary `-` take numbers. There is no date or string arithmetic beyond the date differences.
- The value branches of `CASE` and `COALESCE` share one type. The `NULL` literal may stand for any of them, but not for all of them.
- A predicate position takes a bool: `check`, `when`, `aggregate.filter`, `CASE WHEN` and the operands of `AND`, `OR` and `NOT`.
- A string literal compared with a column whose `validationRules` declare an `enum` must be one of its values. This is flux-expr/1's closed-set rule, and it turns a typo such as `kind = 'base_fe'` into a validation error.

### 2.4 Exact numbers

- Every number is an exact rational. `+`, `-` and `*` are exact, and `/` gives the exact quotient: `1 / 3` is one third. Nothing is binary floating point: a float column's value is read as the shortest decimal that converts back to the same double (§2.3).
- `x / 0` is NULL, so evaluation never raises.
- Every number has a static scale, as in EXPR-SPEC-2 §4:
  - a literal has its written scale, and a column its declared scale; a float column, and a decimal column without `(p,s)`, are unbounded;
  - `+` and `-` take the larger scale, and `*` the sum of the two;
  - `ROUND(x, s)` has scale s, and the date differences scale 0;
  - `CASE` and `COALESCE` take the largest scale of their branches;
  - `/` is unbounded, and so are `+`, `-`, `*` and unary `-` over an unbounded operand, and a `CASE` or `COALESCE` with an unbounded value branch; a `/` inside a condition does not count (EXPR-SPEC-2 §4).
- A `derive` value must fit its column. Its static scale may not exceed the column's, and the validator says to wrap it in `ROUND(x, s)`. An unbounded scale never fits.
- Integer digits are not checked statically. A computed value too large for its column differs from every value the column can hold, so the row violates the invariant; FLUX refuses to write such a value.

### 2.5 Nulls and truth

FLUID SQL/1 is two-valued: every predicate is TRUE or FALSE, never unknown. The rules are the flux-expr/2 picks of 2026-11-16, written in SQL terms:

- Arithmetic follows SQL: a NULL operand gives NULL, and `x / 0` gives NULL.
- A comparison with a NULL operand is FALSE. That covers `=`, `<>`, `!=`, `<`, `<=`, `>`, `>=`, `IN` and `NOT IN`. `NOT IN` is one comparison, so it is FALSE when its left side is NULL, on the rows SQL's UNKNOWN would filter out. `BETWEEN` and `NOT BETWEEN` are their two comparisons: `x BETWEEN a AND b` is `x >= a AND x <= b`, and `x NOT BETWEEN a AND b` is `x < a OR x > b`. So `10 NOT BETWEEN NULL AND 5` is TRUE, as in SQL.
- A bool column or bool expression that is NULL counts as FALSE where a predicate is expected.
- `NOT`, `AND` and `OR` combine TRUE and FALSE. So `NOT (a = b)` is TRUE when `a` is NULL, while `a <> b` is FALSE: the two differ only on NULL. SQL would make both UNKNOWN. §2.8 shows how consumers render the difference exactly.
- `IS NULL`, `IS NOT NULL`, `IS DISTINCT FROM` and `IS NOT DISTINCT FROM` are exact: they treat NULL as a value.
- `CASE` takes the first `WHEN` that is TRUE, so a NULL condition falls through, as in SQL. `COALESCE` returns its first argument that is not NULL.
- A `derive` holds where the column `IS NOT DISTINCT FROM` the value, so a NULL copied from a NULL holds. A `check` written `a = b` does not hold where either side is NULL. Write `a IS NOT DISTINCT FROM b` for a null-safe check.

Why two-valued: a row whose predicate is not TRUE does not satisfy it. Deequ's `satisfies` and the ODCS exporter's `IS NOT TRUE` already read checks that way at the top level. FLUID SQL/1 extends the reading to every sub-expression, so that a check has one verdict per row in every engine. dbt_utils' `expression_is_true` (`where not(expr)`) lets a NULL row pass, and a SQL `CHECK` constraint accepts a NULL result, so neither is used as written (§2.8).

### 2.6 Functions

| Call | Result |
|---|---|
| `ROUND(x, s)` | x rounded to s decimal places, ties away from zero, as warehouses round fixed-point numbers (founder pick, 2026-11-16); s is a non-negative integer literal; NULL gives NULL |
| `ROUND(x, s, 'HALF_TO_EVEN')` | ties to the even neighbor (banker's rounding), in Snowflake's spelling; `'HALF_AWAY_FROM_ZERO'` spells the default |
| `COALESCE(a, b, ...)` | the first argument that is not NULL, else NULL; two or more arguments |
| `CASE WHEN c THEN a ... [ELSE b] END` | as in SQL; without `ELSE`, NULL |
| `DAYS_BETWEEN(a, b)` | the whole days from date a to date b |
| `MONTHS_BETWEEN(a, b)`, `YEARS_BETWEEN(a, b)` | the whole months or years from date a to date b |
| `DATE 'YYYY-MM-DD'` | a date literal: a valid calendar date from 0001-01-01 to 9999-12-31 |

The date differences use EXPR-SPEC-2 §6's definition (decision 14). For b on or after a, the result is the largest k such that a moved forward by k units is not after b. Moving forward by months or years keeps the day of the month, or lands on the month's last day when that month is shorter: 31 January 2026 plus one month is 28 February 2026, and 29 February 2000 plus 26 years is 28 February 2026. For b before a, the result is minus the result from b to a. Both arguments are dates, and a NULL argument gives NULL. Age is `YEARS_BETWEEN(birth_date, snapshot_date)`.

Two warnings for SQL readers:

- These are not `DATEDIFF`, `DATE_DIFF` or DuckDB's `date_diff`, which count the unit boundaries crossed. `DATEDIFF(month, '2026-01-31', '2026-02-01')` is 1, while `MONTHS_BETWEEN(DATE '2026-01-31', DATE '2026-02-01')` is 0. They equal DuckDB's `date_sub`.
- Oracle, Snowflake and Spark have a `MONTHS_BETWEEN(d1, d2)` of their own. It returns a fraction, d1 minus d2, so its arguments run the other way. No consumer passes FLUID's `MONTHS_BETWEEN` to an engine by name (§2.8).

### 2.7 Not in FLUID SQL/1

- String functions and `LIKE`, regular expressions, `CAST`, timestamp literals and functions over timestamps;
- date arithmetic other than the differences, `ABS`, `LEAST`, `GREATEST`, `NULLIF`, parameters, user functions and the clock;
- aggregates and windows inside expressions, subqueries, paths over several links, and the previous version of an entity.

Windows, aggregates over children, `previous` and multi-link paths are flux-expr/2 extensions that a FLUX bundle may use. The rest waits for FLUID SQL/2.

### 2.8 Rendering for SQL engines

Consumers render SQL from the parsed expression, never from the raw text (see Security). The rendering below is exact; the last column says where an engine's own function would not be.

| FLUID SQL/1 | Rendering | Why |
|---|---|---|
| a comparison `p` | `COALESCE(p, FALSE)` | makes the comparison two-valued (§2.5); dropped where both operands are known never to be NULL |
| a bool-valued column, `CASE` or `COALESCE` in a predicate position | `COALESCE(e, FALSE)` | a bool column, a `CASE` without `ELSE`, a `COALESCE` of NULLs and a path through a missing row can all be NULL |
| a `check` violation | `NOT (<rendered check>)` | every operand of `NOT`, `AND` and `OR` is now a comparison or a wrapped bool, never NULL, so `NOT` is exact |
| the rows the k-th `derive` invariant of a column covers | `<when k> AND NOT <when 1> ... AND NOT <when k-1>`, each `when` rendered as above | the first-match coverage of §1.3, so a test of a later invariant never counts the rows an earlier one covers |
| a `derive` violation | `NOT (c IS NOT DISTINCT FROM (<rendered value>))` | Snowflake also has `EQUAL_NULL`; an engine without the operator gets `(c = v OR (c IS NULL AND v IS NULL))` |
| `a / b` | `CAST(a AS <exact numeric>) / NULLIF(b, 0)`, with the engine's exact decimal type | the cast stops integer division (PostgreSQL gives 7 / 2 = 3 otherwise); `NULLIF` keeps `x / 0` NULL, where PostgreSQL, Snowflake and BigQuery raise and DuckDB 1.5.1 returns `inf` |
| `ROUND(x, s)` | `ROUND(x, s)` on a fixed-point value | PostgreSQL `numeric`, Snowflake `NUMBER`, BigQuery `NUMERIC` and DuckDB `DECIMAL` all round ties away from zero |
| `ROUND(x, s, 'HALF_TO_EVEN')` | Snowflake `ROUND(x, s, 'HALF_TO_EVEN')`, Databricks `bround(x, s)`, elsewhere the `CASE` form below | DuckDB's `round_even` returns `DOUBLE`, so it is not used for `DECIMAL` |
| `DAYS_BETWEEN(a, b)` | `b - a` (PostgreSQL), `DATEDIFF(day, a, b)` (Snowflake), `DATE_DIFF(b, a, DAY)` (BigQuery), `datediff(b, a)` (Databricks), `date_sub('day', a, b)` (DuckDB) | both are dates, so day boundaries and whole days agree |
| `MONTHS_BETWEEN(a, b)`, `YEARS_BETWEEN(a, b)` | DuckDB `date_sub('month', a, b)` or `date_sub('year', a, b)`, elsewhere the `EXTRACT` form below | never `DATEDIFF`, `DATE_DIFF`, `date_diff` or an engine's own `MONTHS_BETWEEN` |
| a string compared with `<`, `<=`, `>` or `>=` | as written | PostgreSQL adds `COLLATE "C"`, so text orders by code point, as in Snowflake, BigQuery and DuckDB |
| `rel.col` | `LEFT JOIN <target> AS rel ON <fromColumns = toColumns>` | a left join whatever the relationship's `joinType`, so a missing row reads as NULL |
| `DATE 'YYYY-MM-DD'` | as written | SQL's typed literal |

The `CASE` form of half-to-even, with `y = x * 10^s` in fixed point:
`CASE WHEN ABS(y - TRUNC(y)) = 0.5 THEN TRUNC(y) + CASE WHEN MOD(TRUNC(y), 2) = 0 THEN 0 ELSE SIGN(y) END ELSE ROUND(y) END / 10^s`.

The `EXTRACT` form of whole months from a to b, for b on or after a:
`12 * (EXTRACT(YEAR FROM b) - EXTRACT(YEAR FROM a)) + (EXTRACT(MONTH FROM b) - EXTRACT(MONTH FROM a)) - CASE WHEN EXTRACT(DAY FROM a) > EXTRACT(DAY FROM b) AND b < LAST_DAY(b) THEN 1 ELSE 0 END`.
For b before a, the result is minus the same from b to a. Whole years are whole months divided by 12 and rounded toward zero, because moving forward by k years is moving forward by 12k months. PostgreSQL has no `LAST_DAY`; it uses `(DATE_TRUNC('month', b) + INTERVAL '1 month' - INTERVAL '1 day')::date`, and dbt has `{{ dbt.last_day(b, 'month') }}`.

Two constructs are exact in FLUID SQL/1 but approximate in every engine. The validator notes both:

- A `/` outside `ROUND`. Engines divide at their own precision; DuckDB 1.5.1 divides `DECIMAL` values in `DOUBLE`. A consumer can then disagree only on a row whose quotient lies within that precision of the boundary.
- Arithmetic on a float column. Engines compute it in binary.

### 2.9 flux-expr/2 implements FLUID SQL/1

flux-expr/2 is spelled after flux-expr/1 (`==`, `&&`, `if(...)`). FLUX will therefore read a FLUID text through a SQL front end that produces flux-expr/2's syntax tree. The mapping is what decision 13 asks of flux-expr/2:

| FLUID SQL/1 | flux-expr/2 |
|---|---|
| `=`, `<>` or `!=`, `<`, `<=`, `>`, `>=` | `==`, `!=`, `<`, `<=`, `>`, `>=` |
| `AND`, `OR`, `NOT` | `&&`, `\|\|`, `!` |
| `x IN (l1, l2)` | `x in [l1, l2]` for numbers and strings; for dates or bools, which flux-expr/1's list literal cannot hold, `x == l1 \|\| x == l2` |
| `x NOT IN (l1, l2)` | `!(x == null) && !(x in [l1, l2])`; for dates or bools, `!(x == null) && !(x == l1 \|\| x == l2)` |
| `x BETWEEN a AND b` | `x >= a && x <= b` |
| `x NOT BETWEEN a AND b` | `x < a \|\| x > b` |
| `x IS NULL`, `x IS NOT NULL` | `x == null`, `!(x == null)` |
| `a IS NOT DISTINCT FROM b` | `a == b \|\| (a == null && b == null)` |
| `a IS DISTINCT FROM b` | `!(a == b \|\| (a == null && b == null))` |
| `CASE WHEN c1 THEN v1 WHEN c2 THEN v2 ELSE v3 END` | `if(c1, v1, if(c2, v2, v3))`; without `ELSE`, `null` |
| `COALESCE(...)` | `coalesce(...)` |
| `ROUND(x, s, 'HALF_TO_EVEN')` | `round(x, s)` |
| `ROUND(x, s)` | a half-away-from-zero rounding, which flux-expr/2 adds |
| `DAYS_BETWEEN`, `MONTHS_BETWEEN`, `YEARS_BETWEEN` | `days_between`, `months_between`, `years_between` |
| `DATE '2026-11-16'` | `date('2026-11-16')` |
| a column name `c` | the explicit path `row.c`, so a column named `row`, `link`, `previous` or `children`, or one that shares a relationship's name, needs no rule of its own; a quoted name that flux-expr/2's identifiers cannot spell is a FLUX refusal until flux-expr/2 can quote it |
| `rel.col` | `link.<rel>.col`, always through the `link` root (EXPR-SPEC-2 §5), so a relationship named `row`, `link`, `previous` or `children`, or one that shares a column's name, still reads the linked row |
| `derive: c` with `expr: e` | a rule `c == e` with `determines: c` |
| `derive: c` with `aggregate` | a rule `c == coalesce(<function>(children.<from>.<via>, if(<filter>, <column>, null)), <ifNone>)` with `determines: c`; `count` with a filter is `count(children.<from>.<via>, <filter>)` |
| `check` | a rule without `determines`; a FLUX extension may name the column that a bounds or difference form determines |
| `dependency` | a dependency `{from, to, bothWays}` |
| a share (§1.4) | the band itself for the check; `share` and `tolerance` only as the generator's target (§1.4) |
| `sharedColumns`, `sharedAttribute` (§5) | copy rules `c == link.<path>.<owner's column>` with `determines: c` and `when: !(link.<path>.<key> == null)`, so a row whose path reaches nothing is not covered; a path over several links uses flux-expr/2's chained links |

Seven changes follow for EXPR-SPEC-2, which the flux-expr/2 design (Agenticstiger/flux-engine#38) is to make and cite this RFC for:

1. §1's open question has its answer: rules and dependencies are declared in the FLUID contract, under the contract digest.
2. A half-away-from-zero rounding joins `round`, which stays half-even.
3. The null-safe reading of a rule whose whole expression is `c == e` applies only to rules with `determines`, the copies it was made for. A FLUID `check` written `a = b` keeps the plain comparison.
4. A `bothWays` dependency is counted in each direction separately (§1.5).
5. A rule read from FLUID covers exactly the rows its `when` selects. The implicit exclusion of rows with a NULL bound (EXPR-SPEC-2 §7) does not apply to it: such a row is covered, cannot hold, and is generated as infeasible (EXPR-SPEC-2 §8).
6. A rule or dependency read from FLUID is identified by its contract and its `id`. FLUID makes ids unique within one contract, and two contracts that a Simulation pins may reuse an id, so FLUX keys their streams and reports by both, and FLX-6201 applies within a contract.
7. A rule read from FLUID is checked against its own band (operator, values and unit), so FLUX and SQL consumers reach one verdict; `share` and `tolerance` stay the generator's target. A band that cannot be met in expectation (§1.4) is checked and not generated from.

**Conformance vectors.** forge-cli keeps `fluid-sql-vectors.json` beside the validator. Each vector gives a text, its verdict (accepted, or refused with a reason), its syntax tree, its type and scale, and its value against given rows. The validator runs the file. flux-engine runs the same file through its front end, as EXPR-SPEC-2 §12 runs flux-expr/1's vectors. DuckDB is the oracle for the SQL renderings, with the exact-rational exceptions that EXPR-SPEC-2 §12 already names. The vectors define the subset; neither parser does.

**Parser.** The validator parses with sqlglot, already a base dependency (`iac/row_access.py` parses row-filter predicates the same way). It accepts only the node types the grammar derives. sqlglot 30 reads `MONTHS_BETWEEN` as its own node, which carries Oracle's meaning, and `DATE '...'` as a cast. The validator accepts each only in the shape the grammar derives and gives it FLUID's meaning, and no renderer passes a FLUID text through sqlglot's dialect generators. The import stays inside the function, so `fluid --help` keeps the module budget that `tests/perf/test_startup_budget.py` pins.

---

## 3. Identifier formats (COL-4)

```yaml
contract:
  schema:
    - name: tower_id
      type: string
      identifierFormat:                 # NEW in 0.7.7
        - prefixes: [MSC, TWR]
          mask: "99999"
        - prefixes: ["LEGACY-"]
          mask: "A9-99AA"
```

- `identifierFormat` is a list of alternatives. A value has the format when it is one of an alternative's `prefixes`, followed by a string its `mask` matches. `prefixes` defaults to one empty prefix.
- **Mask grammar**, after COBOL PICTURE strings (founder pick, 2026-11-16):
  - `9` is a digit, 0 to 9;
  - `A` is an upper-case ASCII letter, A to Z;
  - `X` is an upper-case ASCII letter or a digit;
  - a backslash makes the next character literal (`\A`), and every other printable ASCII character stands for itself.

  A mask is 1 to 64 characters, with at least one symbol. There are no repeat counts (COBOL's `9(5)`): write `99999`, so that `(` and `)` stay literal. Prefixes are separate, so `ACC` needs no escaping. Lower-case codes are not in v1; a regex rule covers them.
- **Compiled regex.** Consumers get one anchored regex per column. The example compiles to `^((MSC|TWR)[0-9]{5}|LEGACY-[A-Z][0-9]-[0-9]{2}[A-Z]{2})$`. Runs of one symbol become counts, and literals are escaped. Only groups, alternation, character ranges and counts appear, so the same text works in ECMA-262 (ODCS `logicalTypeOptions.pattern`), POSIX ERE, RE2 and Python. XSD and I-Regexp (RFC 9485) match whole values and have no anchors, so they take the body without `^` and `$`; that body is an I-Regexp. Python consumers match with `re.fullmatch`, because Python's `$` also matches before a trailing newline.
- **Enumerable.** An alternative holds `|prefixes|` × 10^d × 26^a × 36^x values, for a mask with d `9`s, a `A`s and x `X`s, so a generator can enumerate them. FLUX's keyed Feistel permutation will decode an ordinal into the mask's mixed radix (COL-4), so every value is unique by construction. Prefix weights, and prefixes taken from a linked row, are FLUX settings.
- **No overlap.** Expand every alternative into one pattern per prefix: the prefix's characters, then the mask's. Two patterns from different alternatives overlap when they have the same length and, at every position, character sets that intersect (a literal is a set of one). An overlap is a validation error. Two prefixes of one alternative never overlap, because distinct prefixes of one length differ in a literal. Such a value would not say which alternative made it, and a generator could not keep values unique.
- **Uniqueness** comes from the column's key or unique flag, not from the format.
- **Regex rules.** A `validationRules` entry with `type: regex` and `identifierFormat` both apply. A regex can say what a mask cannot (lower case, variable length), but cannot be enumerated. The validator notes a column that declares both.
- **Foreign keys.** A column in a relationship's `fromColumns` carries the values of the matching `toColumns` column, so it has that column's format. It may repeat the format identically; a different one is an error. It inherits the target column's placeholders (§4) with the format, so a copied placeholder is exempt on both sides.
- Placeholders (§4) are exempt. When a column declares both, the exported pattern also accepts its placeholders (each value escaped, each prefix escaped and followed by `.*`), and the dbt test skips placeholder rows, so every consumer applies the same exemption. Only string-family columns take a format.

---

## 4. Placeholder values (COL-7)

```yaml
    - name: product_code
      type: string
      identifierFormat:
        - {prefixes: [P], mask: "99999"}
      placeholders:                     # NEW in 0.7.7
        values: ["XXXXX"]
        prefixes: ["TBD_"]
        mustBeLessOrEqualTo: 1
        unit: percent
```

- A value is a placeholder when it equals one of `values`, or starts with one of `prefixes`. Matching is exact and case-sensitive on the stored text. NULL is never a placeholder.
- A placeholder is exempt from the column's own value checks: `identifierFormat`, `validationRules` and the column's enum. Everywhere else (invariants, keys, relationships) it is an ordinary value. Frictionless Table Schema does the same for its `missingValues`, which it reads as null before it applies a field's constraints. Every existing rendering of those checks gains the exemption: the dbt `accepted_values` and range tests that `validationRules` produce (the bridge emits no regex test for them today) skip placeholder rows through a `where` config, and the ODCS `invalidValues` rules accept them (the values join `validValues`, and an extended `pattern` or a `sql` rule covers the prefixes).
- One `mustBe*` operator with `unit` bounds the number of placeholder rows, as in §1.4; `unit: percent` counts them per 100 rows of the expose. Without an operator, placeholders are declared but not bounded: consumers report them and do not fail on them.
- Only string-family columns take placeholders.

**Why a field and not column `quality[]`.** The brief asked whether ODCS `invalidValues` with a `pattern` argument fits before adding a field. It does not:

- ODCS `invalidValues` with `arguments.pattern` describes the valid values. To count `TBD_*` it would need a negated pattern, which POSIX ERE (Snowflake) and RE2 (BigQuery) cannot write, and nothing can generate from.
- ODCS `missingValues` takes a list of values and no pattern, in v3.1.0 and v3.2.0 alike. Frictionless `missingValues` is a list too. Soda's `missing regex` was the only pattern form found, and its regex is written in the engine's dialect.
- FLUX needs to know that a value is a placeholder, so that it can generate one and list it in the evidence pack (EVD-4). That a value fails a check is not enough.

So `placeholders` is a declaration that the exporters render into those rules (Consumer impact matrix).

**Sentinels and placeholders are different.** A sentinel stands in for no value, such as `UNKNOWN` in place of NULL. Sentinels stay in column `quality[]` as ODCS `missingValues` (COL-5). A placeholder stands in for a real value that is not known yet, such as `TBD_0042` in place of a product code. The same text should not be both.

---

## 5. Shared attributes (REL-9)

**Through one link.**

```yaml
semantics:
  relationships:
    - name: subscription
      to: subscriptions
      fromColumns: [subscription_id]
      toColumns: [subscription_id]
      cardinality: many_to_one
      sharedColumns: [status, access_id]    # NEW in 0.7.7
```

- Each listed column is in both exposes' schemas, under the same name. It is copied through the link: on every row whose link reaches a target row, the column `IS NOT DISTINCT FROM` the target's. The entry is short for `derive: status` with `expr: "subscription.status"` and `when: "subscription.subscription_id IS NOT NULL"` (the link reaches a row), with no violation allowed. A lower share is written as that invariant, with its operator.
- Rows whose foreign key is NULL, or matches no row, are not covered.
- The relationship's own key columns are not listed: they agree by definition.

**Across several links.**

```yaml
# accounts
- name: segment
  type: string
  sharedAttribute: customer_segment          # NEW in 0.7.7
# invoices, linked to accounts through contracts
- name: customer_segment
  type: string
  sharedAttribute: customer_segment
```

- The columns of one contract that declare the same `sharedAttribute` carry one attribute.
- The **owner** is the member that every other member reaches through relationships, following `to`, on exactly one path. Each hop is a relationship that the hop's own expose declares.
- On every row of another member whose path reaches an owner row, the value `IS NOT DISTINCT FROM` the owner's. A broken path, a NULL foreign key or an orphan on any hop, leaves the row uncovered.
- Validation errors: no owner, two candidates, two paths to the owner, one expose carrying the attribute twice, or members of different types.
- A name matches `^[A-Za-z][A-Za-z0-9_.]*$` and is local to the contract. Equivalence across contracts is out of scope.
- FLUX will generate the attribute once, on the owner, and copy it along the path: REL-9's one source per attribute. dbt and ODCS consumers check it with the same joins.
- `semanticType` keeps its meaning, a kind of value such as `email` (founder pick, 2026-11-16). Two columns with the same `semanticType` are not one attribute.

**Never inferred from names.** Two columns with the same name on the two ends of a relationship are not assumed to be one attribute: `status` on accounts and `status` on subscriptions usually differ. `fluid validate` notes each same-name pair that is neither shared nor related by an invariant, so that the author decides. FLUX's REL-9 strictness setting may refuse such a pair in a run.

**One source per value** (EXPR-SPEC-2 §10). A column has at most one source among:

- the `derive` invariants on it, where several with `when`s count as one;
- the `to` side of a `dependency`;
- a `sharedColumns` entry;
- membership of a `sharedAttribute` other than the owner.

A column with two sources is a validation error that names both. A format is not a source: on a column that one of these fills, `identifierFormat` only checks the values, and FLUX's identifier scheme generates only columns with no other source.

---

## Consumer impact matrix (v1 of this RFC)

| Feature | Schema 0.7.7 | Validator lint | dbt bridge | ODCS exporter | MCP compiler | FLUX (will read) |
|---|---|---|---|---|---|---|
| `check` | ✓ | parse, names, types, enum literals | generated test: two-valued rendering, `when` as filter, threshold | schema-level `sql` rule: violating rows, operator and `unit` | informational, via `describe` | checks every run; generates when the bundle names the column |
| `derive` and `expr` | ✓ | as for `check`, plus the target exists, types match, its scale fits, it is not a float, no cycle, one source | generated test: `NOT (c IS NOT DISTINCT FROM v)` | property `transformLogic` and `transformSourceObjects`, plus a `sql` rule | informational | sets the column |
| `derive` and `aggregate` | ✓ | `from` and `via` point here, `function` and `column` agree | generated test over a grouped join | schema-level `sql` rule | informational | sets the parent's column from its children |
| `dependency` | ✓ | columns exist and are disjoint, `bothWays` takes one a side | generated test: g3 count per direction | schema-level `sql` rule | informational | one value per key; a bijection for `bothWays` |
| shares | ✓ | one operator, values that fit the unit | the threshold, in rows or percent | operator and `unit`, copied | informational | generates within the band and checks against it |
| `identifierFormat` | ✓ | mask grammar, string column, no overlap, foreign-key format | regex test through an adapter-dispatched macro | `logicalTypeOptions.pattern`, plus an `invalidValues` rule with `arguments.pattern` | informational | unique values by keyed permutation; prefix weights stay in FLUX |
| `placeholders` | ✓ | string column, values and prefixes not empty | generated test, only when bounded | one `sql` rule that counts placeholder rows, values and prefixes together, when bounded; a `text` rule otherwise, since an ODCS `library` or `sql` rule needs an operator | informational | generates at FLUX's rates, lists them in the evidence pack, and refuses them under strictness |
| `sharedColumns` | ✓ | on both ends, not keys, same type, one source | generated test: equality over the join | schema-level `sql` rule | informational | copies through the link |
| `sharedAttribute` | ✓ | one owner, one path, same type, one source | generated test: joins along the path | schema-level `sql` rule | informational | generates on the owner, copies along the path |

**dbt bridge.** The bridge writes one generic test macro into the generated project, `fluid_rule`. It counts covered and violating rows, joins linked models with `ref()`, and compares the counts with the ODCS operator, in rows or in percent. dbt's `error_if` and `warn_if` compare a figure computed from the failing rows alone, so they cannot express a percent of the covered rows. `dbt_utils.expression_is_true` lets a NULL row pass. Neither is used. No `check` constraint is emitted in v1: a `CHECK` accepts a NULL result, cannot read another table, and dbt enforces it only on Postgres and Databricks. `severity` maps to the test's severity, `error` or `warn`. A construct the bridge cannot render for an adapter is skipped with a logged reason, never approximated.

**ODCS exporter.** Every rule gets:

- the invariant's `id`;
- a `dimension`: consistency for invariants and shared attributes, conformity for formats, completeness for placeholders;
- the operator and `unit`;
- a `query` rendered from the syntax tree, with `${table}` and the names of linked objects. With `unit: rows` it returns the count of violating rows; with `unit: percent` it returns violating rows times 100 divided by covered rows, and 0 when no row is covered.

The FLUID declarations round-trip through the exporter's existing `fluidExtras` blob (`mappers/extras.py`), which already carries every contract and column key that ODCS has no slot for; the importer restores them from there, never from the SQL. A column's own `quality[]` still replaces only the rules forge derives from `required`, keys and `validationRules` (`to_odcs_property` replays it as written); the rules generated from the new fields are appended after it. Each generated rule carries `customProperties` `{property: fluidGenerated, value: true}`. An importer that restores the declarations from `fluidExtras` drops marked rules, so a generated rule never comes back as a hand-written one; with `ODCS_FLUID_EXTRAS=false` there is nothing to restore, and marked rules stay ordinary rules. Where a property already has `logicalTypeOptions` or `transformLogic` (a pass-through, or the stream lineage `mappers/schema.py` writes), the exporter keeps that value and does not write its own; the rules and `fluidExtras` still carry the declaration. Nothing these rules use changed in ODCS v3.2.0.

**MCP compiler.** `describe` returns the new fields with the rest of the contract. The governed query path does not compile or enforce them. No phase 2 is planned. Excluding placeholders from a governed query on request would be a later, separate design.

## Validator lint (v1)

Errors (any one fails `fluid validate`):

1. A new field in a contract below 0.7.7. The message says to declare `fluidVersion: 0.7.7`, as #894's does. `schema_manager._PREVIEW_FIELDS` learns the new fields; for a field inside a list item (a column, a relationship or an invariant), the lookup takes the last named key of the path, because the path ends in a list index there.
2. An `id` that is missing, does not match `^[A-Za-z][A-Za-z0-9_]*$`, or is used twice in the contract, counting `dq.rules[].id` and `contract.quality[].rule`: the ODCS exporter keys rules by id and silently skips a taken one.
3. An entry with no form, or with two.
4. A text outside FLUID SQL/1, with its position and the construct. The hint names `contract.quality[]` for SQL outside the subset.
5. An unknown column, relationship or relationship column, a path over two links, or a path, summary, `sharedColumns` entry or `sharedAttribute` hop through a relationship whose target declares a validity window.
6. A type error (§2.3), including a literal outside a column's enum, or the `NULL` literal as an `IN` item or the operand of a comparison.
7. A `derive` target that is missing or a float, whose type differs from its value's type, or, for a number target, that has no declared scale or whose value's scale does not fit. The message says, for example, "wrap it in ROUND(x, 2)".
8. A cycle among derived columns, within one expose, or across exposes through links and aggregates.
9. A column with two sources (§5).
10. An `aggregate` whose `via` is not a relationship of `from` that points to this expose, whose `function` and `column` disagree, whose `filter` is not a predicate over the child, or whose `ifNone` is not a literal of the summary's type.
11. A `dependency` with an empty or unknown column list, a column on both sides, `bothWays` with more than one column a side, or a `when`.
12. More than one share operator, a negative value, a percent above 100, or a `mustBeBetween` whose bounds are not ascending.
13. A mask with no symbol or with a bad escape, a prefix that is not printable ASCII, a format on a column that is not a string, overlapping alternatives, or a foreign key with a different format.
14. Placeholders on a column that is not a string, or an empty value or prefix.
15. A `sharedColumns` entry that names a key column, or a column missing on either end, or columns of different types.
16. A `sharedAttribute` group with no owner, two candidates, two paths, one expose twice, or mixed types.

Notes, reported and never failing:

- Two same-named columns on the two ends of a relationship, neither shared nor related.
- A `/` outside `ROUND`, or arithmetic on a float column: exact here, approximate in SQL engines.
- A column with both `identifierFormat` and a regex rule.
- A `contract.quality[]` expression that parses as FLUID SQL/1. It can move to `invariants` if FLUX should read it.

## Compatibility

- Additive only. Every field is optional, and `additionalProperties: false` is kept on every touched object.
- 0.7.6 GA is untouched. The fields are preview-gated in 0.7.7, like #894's: an untagged contract never resolves into the preview.
- A contract without the new fields validates, exports and digests exactly as today. A FLUX bundle that pins such a contract will keep every stream and every digest.
- The new fields are covered by the contract digest. A FLUX plan will refuse a changed contract until the Simulation pins the new digest.
- The ODCS round trip is pinned in the exporter's test matrix: FLUID → ODCS → FLUID loses no invariant, format, placeholder or shared attribute.
- `contract.quality[]`, `validationRules` and column `quality[]` are unchanged.

## Security

- **Expressions are data.** No consumer runs the raw text. Renderers print SQL from the parsed tree, quote every name for the target dialect and escape every string literal again. An expression therefore cannot carry a second statement, a comment or a subquery into a dbt test, an ODCS query or a warehouse. The grammar has no statement separator, comment, subquery or function beyond §2.6. Today the ODCS exporter embeds `contract.quality[].expression` as written; the new fields do not take that path.
- **Jinja too.** The dbt bridge writes rendered SQL into a dbt project, where test arguments are rendered as Jinja. A string literal or a name that holds `{{`, `{%` or `{#` is therefore escaped for Jinja as well as for SQL (for example, emitted inside `{% raw %}`), so a contract author cannot run Jinja during `dbt test`.
- **One escaping path.** Mask regexes, placeholder values and prefixes, and the object names in ODCS queries go through the same quoting and escaping functions as the expression renderer, never through a separate string-building path. Conformance vectors feed each renderer hostile names and literals: quotes, `;`, `--`, `/*` and Jinja delimiters.
- **Bounded parsing.** A text is at most 4,096 code points and 64 levels deep, which bounds the parser's work.
- **Linear regexes.** forge compiles the mask regexes; users do not write them. They hold no repeated group, so matching takes linear time. Placeholder values and prefixes are matched as text, and are escaped wherever they enter an exported pattern.
- **No execution path.** The MCP compiler never compiles these texts; `describe` only returns them.

## Out of scope (deliberately)

- **FLUX-only syntax:** windows, aggregates inside expressions, the previous version of an entity and paths over several links.
- **FLUID SQL/2 candidates:** string functions, regular expressions, `CAST`, timestamps, `ABS`, `LEAST` and `GREATEST`, a parameter for the as-of date, `avg` summaries (they need a rounding rule) and summaries compared with anything but equality.
- **Cross-contract** relationships and shared attributes.
- **Point-in-time joins** through a relationship whose target declares a validity window.
- **Generation choices:** distributions, prefix weights, violation draws and the target inside a band. These live in FLUX documents.
- **Measured shares and statistics.** These live in profile documents (`fluid profile`, decision 11; `RFC-data-profile-port.md`).
- **Discovery:** finding invariants or placeholders that nobody declared (decision Q9).
- **Docs pages.** User docs go to forge_docs. This RFC sits at the repository root, following tier-1.

## Implementation plan

Each step is its own PR:

1. Schema 0.7.7 fields, the validator and `fluid-sql-vectors.json`, in forge-cli.
2. The ODCS exporter and the importer's round trip, including the placeholder exemption in the existing `invalidValues` rules.
3. The dbt bridge's `fluid_rule` macro and its tests, plus the placeholder exemption in the existing `accepted_values` and range tests, behind the real `dbt parse` acceptance gate that tier-1's Compatibility section names.
4. In flux-engine, EXPR-SPEC-2 takes the seven changes of §2.9 and cites this RFC; then the FLUID reader and the SQL front end.
5. The forge_docs pages.

## References

- PostgreSQL 18, generated columns and constraints: postgresql.org/docs/current/ddl-generated-columns.html, /ddl-constraints.html
- ODCS v3.1.0 (vendored as `fluid_build/providers/odcs/odcs-schema-v3.1.0.json`) and v3.2.0 data quality: bitol-io.github.io/open-data-contract-standard/v3.2.0/data-quality/
- dbt constraints and data tests: docs.getdbt.com/reference/resource-properties/constraints, /reference/data-test-configs; dbt_utils `expression_is_true`; dbt-expectations, maintained at github.com/metaplane/dbt-expectations
- Snowflake `ROUND`, `MONTHS_BETWEEN`, `DATEDIFF`, `IS [NOT] DISTINCT FROM`, `EQUAL_NULL` and semantic view validation rules: docs.snowflake.com/en/sql-reference/functions/, /user-guide/views-semantic/validation-rules
- BigQuery date functions and rounding modes: docs.cloud.google.com/bigquery/docs/reference/standard-sql/date_functions; DuckDB date functions: duckdb.org/docs/2.0/sql/functions/date.html
- Apache Ossie core spec: github.com/apache/ossie
- Deequ `satisfies` and `Compliance`: github.com/awslabs/deequ
- J. Kivinen and H. Mannila, "Approximate inference of functional dependencies from relations", Theoretical Computer Science 149(1), 1995; Y. Huhtala et al., "TANE", The Computer Journal 42(2), 1999
- COBOL PICTURE symbols: IBM Enterprise COBOL language reference, "Symbols used in the PICTURE clause"
- Frictionless Table Schema `missingValues`: specs.frictionlessdata.io/table-schema/; Soda missing metrics (`missing regex`): docs.soda.io
- RFC 9485, I-Regexp: an interoperable regular expression format
- Internal: flux-engine ADR 0008 and EXPR-SPEC-2 (Agenticstiger/flux-engine#38); FLUX relational data requirements v3; #894 (relationships and validity); #845 (data profile port); `fluid_build/providers/odcs/mappers/quality.py` (contract quality as ODCS `sql` rules); `fluid_build/engines/dbt/_test_mapping.py`; `fluid_build/iac/row_access.py` (sqlglot predicate parsing)
