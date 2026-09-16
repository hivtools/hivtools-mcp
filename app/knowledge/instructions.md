# HIV estimates — how to use this server

This server serves modelled HIV estimates. Every value is a model output, not an observation.

## Sources

Each row has a `source`: the model it comes from. They cover very different ground:

| `source` | Covers | Uncertainty |
|---|---|---|
| `naomi` | Subnational (every area level), a few recent quarters | `lower`–`upper` interval |
| `spectrum` | National only, one value a year from 1970; later years are **projections** | none |
| `shipp` | Adults 15–49, one quarter, split by behavioural `risk_group` (female sex workers, MSM, PWID, …) | none |

Not every country has every source. The same indicator ID from two sources is the same quantity estimated by two models: pass `source` to choose one, and **never add rows from different sources together**. Use `naomi` for the current subnational picture, `spectrum` for long-term national trends, and `shipp` for key and priority populations.

## Dataset status

What a country's figures are depends on the country. Every response's `meta.source` label (or `meta.sources`) says how they must be described — use that wording whenever you quote a number. Some countries are **synthetic demonstration data** (area names end "- Demo", area IDs end `_demo`): call those synthetic, and never present them as real epidemiological facts about any country.

## Workflow — always

1. `search_hiv_metadata` to resolve any plain-language term to an ID. Indicator, area, age
   group, risk group and concept IDs are **not guessable**; never supply one from memory.
2. `get_hiv_data` with the resolved IDs.
3. If `search_hiv_metadata` returns `ambiguous: true`, the top matches are genuinely different
   answers. Choose deliberately, and ask the user when the choice changes the
   result.
4. If `get_hiv_data` returns no rows it also returns a `diagnostic` saying why.
   **Read it before concluding anything.** It distinguishes a value that does not
   exist (with the nearest real ones) from a valid combination that has no data
   (naming the filter at fault and the values that would have worked). Never
   report "there is no data" from an empty result you have not read the
   `diagnostic` for - the usual cause is a filter you can simply change.

Each match says which `field` it is, and that determines what to do with it:

| `field` | What to do |
|---|---|
| `concept` | The most useful kind. Use the indicators it lists, pass their `id`s (and `source`, where given) to `get_hiv_data`, and follow its `notes` — that is where the reason an answer is right or wrong lives. Check each indicator's `coverage` before querying. When the user did not say which ages, sexes or source they meant, use `default_disaggregation`: it is the convention for that concept, and asking for everything returns hundreds of overlapping rows. `related` names neighbouring concepts — follow one when the match is close but not what was asked. If `answerable` is `false`, say the data cannot answer it. |
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
listing age groups yourself: a partition tiles its population exactly, so the
rows can be added up. Find the available ones with `search_hiv_metadata` using
`field=age_partition`; they are not listed here, because which exist is a
property of the dataset rather than of this guidance. Never invent one.

For a single age band, request the aggregate age group directly (e.g.
`Y015_049`) rather than summing parts.

## Time

Naomi estimates cover a small number of quarters spanning a few years, and **not every indicator covers every quarter**. Do not assume the newest quarter exists for the indicator and source you want: check that indicator's `coverage` from `search_hiv_metadata` first. Asking for a quarter it does not cover returns no rows.

For change over 5, 10 or 20 years, use `spectrum` where the country has it, and say which years are projections. Without it, say the data does not cover that period. Never fit a trend to a handful of Naomi quarters and describe it as a decade.

## Reading a response

Each response carries a `meta` block. Two fields there change what a number
means, and you must read them before interpreting it:

- **`unit`** — `proportion` is a fraction **0–1** (`0.108` means **10.8%**, not 0.108%); `count` is a number of people; `rate_per_person_year` (`0.0018` means **1.8 per 1,000 people per year**).
- **`basis`** — `residents` counts people who *live* in the area, `attending` counts people who *receive care* there. These differ substantially at district level. Population questions want `residents`; facility workload questions want `attending`.

## Reporting

- Report the `lower`–`upper` interval alongside `mean` where there is one. Spectrum and SHIPP give point estimates only; say so rather than implying precision.
- When ranking areas, check whether the top entries' intervals overlap. If they do, say the ordering between them is not statistically meaningful.
- "Burden", "risk", "gap" and "priority" are ambiguous. Resolve them with `search_hiv_metadata` first. If a concept maps to several indicators that rank differently (as `treatment_gap` does), report both or ask which the user means.
