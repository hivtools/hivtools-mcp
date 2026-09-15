# Naomi HIV estimates — how to use this server

Naomi is a small-area model producing subnational HIV estimates. Every value is a modelled estimate with uncertainty, not an observation.

## Dataset status

The dataset currently served is **synthetic demonstration data**, not official estimates. Area names carry a "- Demo" suffix and area IDs end in `_demo`. Say so whenever you quote a number, and never present these as real epidemiological facts about any country.

## Workflow — always

1. `search` to resolve any plain-language term to an ID. Indicator, area, age
   group and concept IDs are **not guessable**; never supply one from memory.
2. `get_hiv_data` with the resolved IDs.
3. If `search` returns `ambiguous: true`, the top matches are genuinely different
   answers. Choose deliberately, and ask the user when the choice changes the
   result.

Each match says which `field` it is, and that determines what to do with it:

| `field` | What to do |
|---|---|
| `concept` | The most useful kind. Use the indicators it lists, pass their `id`s to `get_hiv_data`, and follow its `notes` — that is where the reason an answer is right or wrong lives. Check each indicator's `coverage` before querying. If `answerable` is `false`, say the data cannot answer it. |
| `indicator` | Pass the `id` as `indicator`. Read its `unit` and `basis`. |
| `area` | Pass the `id` as `area_id`. `area_level` distinguishes same-named areas. |
| `age_group` | Pass the `id` as `age_group`. |
| `age_partition` | Pass the **name** as `age_partition`, not as `age_group`. The server expands it to a non-overlapping set, which is how to get a breakdown by age safely. |

Coverage — which countries, quarters, area levels and age groups exist, and how they are labelled — **differs per country and per indicator**, so it is not listed here. Resolve it rather than assuming it.

## Never sum across these dimensions

Age groups, sexes and area levels each contain **totals as well as parts**.
Adding up all the rows double-counts badly:

| Dimension | Naive sum of all rows | What is correct |
|---|---|---|
| `age_group` | **~7x too high** | request an `age_partition` |
| `sex` | **exactly 2x too high** | `both` *is* the total, not a third category |
| `area_level` | **one multiple per level** | every level already covers the whole country |

For a breakdown by age, pass an `age_partition` to `get_hiv_data` rather than
listing age groups yourself: a partition tiles its population exactly, so the
rows can be added up. Find the available ones with `search` using
`field=age_partition`; they are not listed here, because which exist is a
property of the dataset rather than of this guidance. Never invent one.

For a single age band, request the aggregate age group directly (e.g.
`Y015_049`) rather than summing parts.

## Time

Naomi estimates cover a small number of quarters spanning a few years — never
decades — and **not every indicator covers every quarter**. Do not assume the
newest quarter in the dataset exists for the indicator you want: check that
indicator's `coverage.calendar_quarter` from `search` first. Asking for a quarter
an indicator does not cover returns no rows, with nothing to say why.

For any question about change over 5, 10 or 20 years, check coverage first. If the span requested exceeds it, say the data does not cover that period. Do not fit a trend to a handful of points and describe it as a decade.

## Reading a response

Each response carries a `meta` block. Two fields there change what a number
means, and you must read them before interpreting it:

- **`unit`** — `proportion` is a fraction **0–1** (`0.108` means **10.8%**, not 0.108%); `count` is a number of people; `rate_per_person_year` (`0.0018` means **1.8 per 1,000 people per year**).
- **`basis`** — `residents` counts people who *live* in the area, `attending` counts people who *receive care* there. These differ substantially at district level. Population questions want `residents`; facility workload questions want `attending`.

## Reporting

- Report the `lower`–`upper` interval alongside `mean`. These are estimates.
- When ranking areas, check whether the top entries' intervals overlap. If they do, say the ordering between them is not statistically meaningful.
- "Burden", "risk", "gap" and "priority" are ambiguous. Resolve them with `search` first. If a concept maps to several indicators that rank differently (as `treatment_gap` does), report both or ask which the user means.
