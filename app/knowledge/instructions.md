# HIV estimates — how to use this server

This server serves modelled HIV estimates. Every value is a model output, not an observation.

## Sources

Each row has a `source`:

| `source` | Area Level | Time Periods | Population | Uncertainty |
|---|---|---|---|---|
| `Naomi` | Hierarchical down to the subnational level | Cross sectional: Year of last survey, most recent year, 2 step future projections | General by age and sex | `lower`–`upper` interval |
| `Spectrum` | National | Longitudinal: From 1970 to recent year, projections post 2025 | General by age and sex | none |
| `SHIPP` | Hierarchical down to the subnational level | Cross sectional: most recent year only | Risk behaviour by age and sex | none |

The same indicator ID from two sources is the same quantity estimated by two models: pass `source` to choose one, and **never add rows from different sources together**. Use `Spectrum` for longitudinal, national trends, `Naomi` for subnational trends and `SHIPP` for questions on HIV risk and priority populations.

## Dataset status

What a country's figures are depends on the country. Every response's `meta.source` label (or `meta.sources`) says how they must be described — use that wording when quoting a number. Some countries are **synthetic demonstration data** (area names end "- Demo", area IDs end `_demo`): call those synthetic, never present them as real epidemiological facts about any country.

## Workflow — always

1. `search_hiv_metadata` to resolve any plain-language term to an ID. Indicator, area, age
   group, risk group and concept IDs are **not guessable**; never supply one from memory.
2. `get_hiv_data` with the resolved IDs.
3. If `search_hiv_metadata` returns `ambiguous: true`, the top matches are genuinely different
   answers, not just close scores. Ask the user when the choice changes which indicator to
   query; otherwise pick the best match.
4. If `get_hiv_data` returns no rows it also returns a `diagnostic` saying why.
   **Read it before concluding anything.** It distinguishes a value that does not
   exist (with the nearest real ones) from a valid combination that has no data
   (naming the filter at fault and the values that would have worked). Never
   report "there is no data" without reading the `diagnostic` first - the usual
   cause is a filter that can be changed.

Each match says which `field` it is, and that determines what to do with it:

| `field` | What to do |
|---|---|
| `concept` | Use the indicators it lists, pass their `id`s (and `source`, where given) to `get_hiv_data`, and follow its `notes` — that is where the reason an answer is right or wrong lives. Check each indicator's `coverage` before querying. When the user did not say which ages, sexes or source they meant, use `default_disaggregation`: it is the convention for that concept, and asking for everything returns hundreds of overlapping rows. `related` names neighbouring concepts — follow one when the match is close but not what was asked. If `answerable` is `false`, say the data cannot answer it. |
| `indicator` | Pass the `id` as `indicator`. Read its `unit` and `basis`, and its `coverage`, which is keyed by source. |
| `area` | Pass the `id` as `area_id`. `area_level` distinguishes same-named areas. |
| `age_group` | Pass the `id` as `age_group`. |
| `age_partition` | Pass the **name** as `age_partition`, not as `age_group`. The server expands it to a non-overlapping set, which is how to get a breakdown by age safely. |
| `risk_group` | Pass the `id` as `risk_group`, with the `source` and a `sex` it lists. |

Coverage — which countries, sources, quarters, area levels and age groups exist, and how they are labelled — **differs per country and per indicator**, so it is not listed here. Resolve it rather than assuming it.

## Never sum across these dimensions

Age groups, sexes and area levels each contain **totals as well as parts**, and sources are alternatives.
Adding up all the rows double-counts badly:

| Dimension | Naive sum of all rows | What is correct |
|---|---|---|
| `age_group` | **~7x too high** | request an `age_partition` |
| `sex` | **exactly 2x too high** | `both` *is* the total, not a third category |
| `area_level` | **one multiple per level** | every level already covers the whole country |
| `source` | **one multiple per source** | pick one source |

Risk groups are the exception: within one `sex`, the groups do not overlap and add up to the whole population, so they may be summed.

For a breakdown by age, pass an `age_partition` to `get_hiv_data` rather than
listing age groups: a partition tiles its population exactly, so the
rows can be added up. Find the available ones with `search_hiv_metadata` using
`field=age_partition`; they are not listed here, because which exist is a
property of the dataset rather than of this guidance. Never invent one.

For a single age band, request the aggregate age group directly (e.g.
`Y015_049`) rather than summing parts.

## Time periods

**Not every indicator covers every quarter.** Check `coverage` from `search_hiv_metadata` before assuming the newest quarter exists for the indicator and source you want — asking for one it doesn't cover returns no rows.

For longitudinal trends, use `Spectrum` and say which years are projections. Without it, say the data does not cover that period. Never fit a trend to a handful of `Naomi` quarters and describe it as a decade.

## Reading a response

Each response carries a `meta` block. Two fields change what a number means —
read them before interpreting:

- **`unit`** — `proportion` is a fraction **0–1** (`0.108` means **10.8%**, not 0.108%); `count` is a number of people; `rate_per_person_year` (`0.0018` means **1.8 per 1,000 people per year**).
- **`basis`** — `residents` counts people who *live* in the area, `attending` counts people who *receive care* there. These differ substantially at district level. Population questions want `residents`; facility workload questions want `attending`.

## Reporting

- Default to a short, high-level summary of the 2025 data and a simple plot/s where the client supports one, not prose paragraphs. Give the fuller explanation and/or more complicated plots only when asked.
- State plainly when a number comes from this server versus anywhere else. If a matched concept has `answerable: false` or nothing matches at all, say this dataset does not cover it, and ask the user whether they want a pointer to an external source or to supply their own data - never fetch or guess the answer.
- Report the `lower`–`upper` interval alongside `mean` where there is one. Spectrum and SHIPP give point estimates only; say so rather than implying precision.
- When ranking areas, check whether the top entries' intervals overlap. If they do, say the ordering between them is not statistically meaningful.
- "Burden", "risk", "gap" and "priority" are ambiguous. Resolve them with `search_hiv_metadata` first. If a concept maps to several indicators that rank differently (as `treatment_gap` does), report both or ask which the user means.
