################################################################
# Spectrum and SHIPP outputs -> the hivtools-mcp Parquet dataset.
#
# Run by extract_indicators.py, which writes the Naomi source and the manifest;
# this writes the `spectrum` and `shipp` sources in the same layout:
#
#   <out-dir>/facts/country=TZA/source=spectrum/part-0.parquet
#   <out-dir>/dim_period/country=TZA/source=spectrum/part-0.parquet
#   ...
#
# Both reuse the Naomi output's areas, age groups and indicator labels, so the
# Naomi zip is an input here too. Indicators that exist in Naomi keep Naomi's ID:
# they are the same quantity, and `source` says which model produced a row.
#
# Spectrum (national, one value a year): single-year ages are summed into every
# Naomi age group, and `both` is added for every indicator, so it is queried
# exactly like Naomi.
#
# SHIPP (adults 15-49, one year): the population, PLHIV and new infections of
# Naomi's districts, split by behavioural risk group. Only the five-year bands
# are read; wider age groups, `both`, and every area level above the districts
# are summed from them, and rates recomputed from the sums. Deriving the wider
# age groups also sidesteps the workbook's own, which are wrong for MSM
# population in the male sheet.
#
# Usage:
#   Rscript extract_spectrum_shipp.R --country=TZA --naomi=<zip> --out-dir=<dir>
#       [--spectrum=<pjnz>] [--shipp=<xlsx>]
################################################################

# Needs R >= 4.1 (native pipe) and the packages in the Makefile's R_PACKAGES;
# every call is namespace-qualified, so nothing is attached.

FACT_COLUMNS <- c(
  "area_level", "area_id", "sex", "age_group", "calendar_quarter", "indicator",
  "mean", "se", "median", "mode", "lower", "upper",
  "area_name", "area_level_label", "area_sort_order",
  "age_group_label", "age_group_sort_order",
  "quarter_label", "indicator_label",
  "risk_group", "risk_group_label", "risk_group_sort_order"
)

# `all` is the whole population, and the only group Naomi and Spectrum have.
# Within one sex, SHIPP's groups are mutually exclusive and add up to it: women
# who sell sex are not also counted under non-regular partners, and so on.
RISK_GROUPS <- tibble::tribble(
  ~risk_group, ~risk_group_label, ~risk_group_sort_order,
  "all", "All", 0L,
  "nosex12m", "No sex in the past 12 months", 1L,
  "sexcohab", "One cohabiting or marital partner", 2L,
  "sexnonreg", "Non-regular sexual partner(s)", 3L,
  "sexpaid12m", "Female sex workers", 4L,
  "msm", "Men who have sex with men", 5L,
  "pwid", "Men who inject drugs", 6L
)

# ---- shared -----------------------------------------------------------

read_naomi_meta <- function(naomi_zip) {
  read <- function(member) {
    readr::read_csv(unz(naomi_zip, member), col_types = readr::cols(.default = readr::col_character()))
  }
  list(
    area = read("meta_area.csv") |>
      dplyr::mutate(dplyr::across(c(area_level, area_sort_order), as.integer)),
    age_group = read("meta_age_group.csv") |>
      dplyr::mutate(
        dplyr::across(c(age_group_start, age_group_span), as.numeric),
        age_group_sort_order = as.integer(age_group_sort_order)
      ),
    indicator = read("meta_indicator.csv") |>
      dplyr::mutate(indicator_sort_order = as.integer(indicator_sort_order))
  )
}

period_rows <- function(year, quarter) {
  tibble::tibble(
    calendar_quarter = sprintf("CY%dQ%d", year, quarter),
    quarter_id = as.integer((year - 1900) * 4 + quarter),
    quarter_label = paste(month.name[quarter * 3], year)
  )
}

# The indicators a source brings, labelled as Naomi labels them. Anything Naomi
# does not have must be described in `extra`, or it would be served unlabelled.
indicator_rows <- function(ids, meta, extra = NULL) {
  known <- meta$indicator |>
    dplyr::filter(indicator %in% ids) |>
    dplyr::select(indicator, indicator_label, description, indicator_sort_order)
  if (!is.null(extra)) {
    extra <- extra |>
      dplyr::mutate(indicator_sort_order = max(meta$indicator$indicator_sort_order) + dplyr::row_number())
    known <- dplyr::bind_rows(known, extra)
  }
  missing <- setdiff(ids, known$indicator)
  if (length(missing) > 0) {
    stop("no label for indicator(s): ", paste(missing, collapse = ", "))
  }
  known
}

# Join the label and sort-order columns on, as extract_indicators.py does for Naomi.
label_facts <- function(facts, meta, periods, indicators) {
  out <- facts |>
    dplyr::left_join(
      meta$area |> dplyr::select(area_id, area_level, area_name, area_level_label, area_sort_order),
      by = "area_id"
    ) |>
    dplyr::left_join(
      meta$age_group |> dplyr::select(age_group, age_group_label, age_group_sort_order),
      by = "age_group"
    ) |>
    dplyr::left_join(periods |> dplyr::select(calendar_quarter, quarter_label), by = "calendar_quarter") |>
    dplyr::left_join(indicators |> dplyr::select(indicator, indicator_label), by = "indicator") |>
    dplyr::left_join(RISK_GROUPS, by = "risk_group") |>
    dplyr::mutate(
      se = NA_real_, median = NA_real_, mode = NA_real_,
      lower = NA_real_, upper = NA_real_
    )
  unlabelled <- out |>
    dplyr::filter(is.na(area_name) | is.na(age_group_label) | is.na(quarter_label) | is.na(risk_group_label))
  if (nrow(unlabelled) > 0) {
    stop(nrow(unlabelled), " rows have an area, age group, period or risk group Naomi does not define")
  }
  out |> dplyr::select(dplyr::all_of(FACT_COLUMNS))
}

# R integers become int64, matching what polars writes for the Naomi source, so
# DuckDB reads one type per column across every partition.
as_arrow <- function(df) {
  fields <- lapply(df, function(column) {
    if (is.integer(column)) arrow::int64() else if (is.numeric(column)) arrow::float64() else arrow::utf8()
  })
  arrow::arrow_table(df, schema = do.call(arrow::schema, fields))
}

write_source <- function(tables, country, source, out_dir) {
  for (name in names(tables)) {
    table <- tables[[name]] |> dplyr::mutate(country = country, source = source)
    arrow::write_dataset(
      as_arrow(table),
      file.path(out_dir, name),
      partitioning = c("country", "source"),
      existing_data_behavior = "delete_matching",
      basename_template = "part-{i}.parquet"
    )
  }
}

# ---- Spectrum ---------------------------------------------------------

# Core indicator set only. To add: ART initiations, PMTCT, child HIV.
SPECTRUM_INDICATORS <- list(
  population = SpectrumUtils::dp.output.bigpop,
  plhiv = SpectrumUtils::dp.output.hivpop,
  art_current = SpectrumUtils::dp.output.artpop,
  infections = SpectrumUtils::dp.output.incident.hiv,
  aids_deaths = SpectrumUtils::dp.output.deaths.hiv
)

SPECTRUM_NEW_INDICATORS <- tibble::tribble(
  ~indicator, ~indicator_label, ~description,
  "aids_deaths", "AIDS deaths", "Number of AIDS-related deaths during the year"
)

# Spectrum's open-ended top age, which SpectrumUtils labels "80+".
SPECTRUM_OPEN_AGE <- 80L

# Which quarter a Spectrum year's values belong to. Spectrum 6.2 moved from
# mid-year to calendar-year projections; this is the rule Naomi itself applies
# (via eppasm) when it reads a PJNZ. Compared as a decimal, as Spectrum numbers
# its versions: 6.19 came before 6.2.
spectrum_quarter <- function(dp) {
  version <- as.numeric(dp$Data[which(dp$Tag == "<ValidVers MV>") + 2])
  if (length(version) != 1 || is.na(version)) {
    stop("cannot read the Spectrum version from the DP file")
  }
  if (version >= 6.2) 4L else 2L
}

# Every Naomi age group, as the single-year Spectrum ages that make it up.
spectrum_age_map <- function(meta) {
  map <- meta$age_group |>
    dplyr::select(age_group, start = age_group_start, span = age_group_span) |>
    dplyr::cross_join(tibble::tibble(age = 0:SPECTRUM_OPEN_AGE)) |>
    dplyr::filter(age >= start, age < start + span)
  # The open age is everyone 80 and over, so only an open-ended group may hold it.
  bad <- map |> dplyr::filter(age == SPECTRUM_OPEN_AGE, is.finite(span))
  if (nrow(bad) > 0) {
    stop("age groups cannot be built from Spectrum's 80+: ", paste(bad$age_group, collapse = ", "))
  }
  map |> dplyr::select(age_group, age)
}

read_spectrum <- function(pjnz, meta) {
  dp <- SpectrumUtils::read.module.data(pjnz, "DP")
  first_year <- SpectrumUtils::dp.inputs.first.year(dp)
  final_year <- SpectrumUtils::dp.inputs.final.year(dp)
  quarter <- spectrum_quarter(dp)
  ages <- spectrum_age_map(meta)
  national <- meta$area$area_id[meta$area$area_level == 0]

  by_sex <- dplyr::bind_rows(lapply(names(SPECTRUM_INDICATORS), function(indicator) {
    SPECTRUM_INDICATORS[[indicator]](
      dp,
      direction = "long", first.year = first_year, final.year = final_year
    ) |>
      # Some outputs carry "Male+Female" as well; `both` is summed below instead,
      # so every indicator gets it the same way.
      dplyr::filter(Sex %in% c("Male", "Female")) |>
      dplyr::transmute(
        indicator = indicator,
        sex = tolower(Sex),
        age = as.integer(sub("+", "", Age, fixed = TRUE)),
        year = as.integer(Year),
        value = Value
      )
  }))
  both <- by_sex |>
    dplyr::group_by(indicator, age, year) |>
    dplyr::summarise(value = sum(value), .groups = "drop") |>
    dplyr::mutate(sex = "both")

  facts <- dplyr::bind_rows(by_sex, both) |>
    dplyr::inner_join(ages, by = "age", relationship = "many-to-many") |>
    dplyr::group_by(indicator, sex, age_group, year) |>
    dplyr::summarise(mean = sum(value), .groups = "drop") |>
    dplyr::mutate(
      area_id = national,
      calendar_quarter = sprintf("CY%dQ%d", year, quarter),
      risk_group = "all"
    )

  periods <- period_rows(first_year:final_year, quarter)
  indicators <- indicator_rows(names(SPECTRUM_INDICATORS), meta, SPECTRUM_NEW_INDICATORS)
  list(
    facts = label_facts(facts, meta, periods, indicators),
    dim_period = periods,
    dim_indicator = indicators,
    dim_risk_group = RISK_GROUPS |> dplyr::filter(risk_group == "all")
  )
}

# ---- SHIPP ------------------------------------------------------------

SHIPP_SHEETS <- list(
  female = list(sheet = "All outputs - F", groups = c("nosex12m", "sexcohab", "sexnonreg", "sexpaid12m")),
  male = list(sheet = "All outputs - M", groups = c("nosex12m", "sexcohab", "sexnonreg", "msm", "pwid"))
)
SHIPP_COUNTS <- c("population", "plhiv", "infections", "susceptible")
SHIPP_INDICATORS <- c("population", "plhiv", "infections", "prevalence", "incidence")

# The Naomi estimates a SHIPP workbook was built from, which is the period its
# figures describe. It is usually an earlier Naomi round than the one served.
shipp_quarter <- function(path, country) {
  inputs <- openxlsx::read.xlsx(path, sheet = "Model inputs", colNames = FALSE)
  quarter <- inputs[[3]][inputs[[1]] %in% country]
  if (length(quarter) != 1 || !grepl("^CY[0-9]{4}Q[1-4]$", quarter)) {
    stop("cannot find ", country, "'s Naomi quarter in the SHIPP 'Model inputs' sheet")
  }
  quarter
}

# One sex's sheet as long counts by district, five-year band and risk group.
read_shipp_sheet <- function(path, sex, spec, bands, country) {
  raw <- openxlsx::read.xlsx(path, sheet = spec$sheet)
  raw <- raw[raw$iso3 %in% country, ]
  columns <- as.vector(outer(SHIPP_COUNTS, spec$groups, paste, sep = "_"))
  missing <- setdiff(c(columns, paste0("incidence_", spec$groups)), names(raw))
  if (length(missing) > 0) {
    stop("SHIPP sheet '", spec$sheet, "' lacks columns: ", paste(missing, collapse = ", "))
  }

  long <- raw |>
    dplyr::select(area_id, age_group, dplyr::all_of(columns)) |>
    tidyr::pivot_longer(
      dplyr::all_of(columns),
      names_to = c(".value", "risk_group"),
      names_pattern = "^([a-z]+)_(.+)$"
    ) |>
    dplyr::mutate(sex = sex)

  # Rates are recomputed from the counts wherever rows are summed, so check the
  # workbook's own rates are what that computation gives.
  check <- raw |>
    dplyr::filter(age_group %in% bands) |>
    dplyr::select(area_id, age_group, dplyr::all_of(paste0("incidence_", spec$groups))) |>
    tidyr::pivot_longer(-c(area_id, age_group), names_to = "risk_group", names_prefix = "incidence_",
                 values_to = "workbook") |>
    dplyr::inner_join(long, by = c("area_id", "age_group", "risk_group")) |>
    dplyr::filter(susceptible > 0) |>
    dplyr::mutate(error = abs(infections / susceptible - workbook) / pmax(workbook, 1e-12))
  if (max(check$error) > 1e-6) {
    stop("SHIPP sheet '", spec$sheet, "': incidence is not infections / susceptible")
  }

  # The wider age groups are derived, not read: in the male sheet the workbook's
  # own have the wrong MSM population. Say when the two disagree.
  tiles <- attr(bands, "tiles")
  shipped <- long |> dplyr::filter(!age_group %in% bands)
  for (group in intersect(unique(shipped$age_group), names(tiles))) {
    ours <- long |>
      dplyr::filter(age_group %in% tiles[[group]]) |>
      dplyr::group_by(area_id, risk_group) |>
      dplyr::summarise(dplyr::across(dplyr::all_of(SHIPP_COUNTS), sum), .groups = "drop") |>
      tidyr::pivot_longer(dplyr::all_of(SHIPP_COUNTS), names_to = "count", values_to = "ours")
    theirs <- shipped |>
      dplyr::filter(age_group == group) |>
      dplyr::select(area_id, risk_group, dplyr::all_of(SHIPP_COUNTS)) |>
      tidyr::pivot_longer(dplyr::all_of(SHIPP_COUNTS), names_to = "count", values_to = "theirs")
    differ <- dplyr::inner_join(ours, theirs, by = c("area_id", "risk_group", "count")) |>
      dplyr::filter(abs(ours - theirs) > 1e-6 * pmax(abs(ours), 1)) |>
      dplyr::distinct(count, risk_group)
    if (nrow(differ) > 0) {
      message(sprintf(
        "  SHIPP %s %s: workbook differs from the sum of its bands for %s; using the sum",
        sex, group, paste0(differ$count, "_", differ$risk_group, collapse = ", ")
      ))
    }
  }
  long |> dplyr::filter(age_group %in% bands)
}

# Five-year bands in the workbook, and every Naomi age group they tile exactly.
shipp_age_groups <- function(path, meta) {
  in_sheet <- unique(openxlsx::read.xlsx(path, sheet = SHIPP_SHEETS$female$sheet, cols = 2)[[1]])
  groups <- meta$age_group |> dplyr::filter(age_group %in% in_sheet)
  bands <- groups |> dplyr::filter(age_group_span == 5) |> dplyr::arrange(age_group_start)
  if (nrow(bands) == 0) stop("no five-year age bands in the SHIPP workbook")
  low <- min(bands$age_group_start)
  high <- max(bands$age_group_start + bands$age_group_span)
  tiles <- meta$age_group |>
    dplyr::filter(
      age_group_start >= low, age_group_start + age_group_span <= high,
      age_group_start %in% bands$age_group_start,
      (age_group_start + age_group_span) %in% (bands$age_group_start + bands$age_group_span)
    )
  structure(
    bands$age_group,
    tiles = stats::setNames(lapply(seq_len(nrow(tiles)), function(i) {
      bands$age_group[bands$age_group_start >= tiles$age_group_start[i] &
        bands$age_group_start < tiles$age_group_start[i] + tiles$age_group_span[i]]
    }), tiles$age_group)
  )
}

# Each area paired with itself and every area above it.
area_ancestors <- function(areas) {
  pairs <- areas |> dplyr::transmute(area_id, ancestor = area_id)
  frontier <- areas |> dplyr::transmute(area_id, ancestor = parent_area_id) |> dplyr::filter(!is.na(ancestor))
  while (nrow(frontier) > 0) {
    pairs <- dplyr::bind_rows(pairs, frontier)
    frontier <- frontier |>
      dplyr::inner_join(
        areas |> dplyr::select(ancestor = area_id, parent = parent_area_id),
        by = "ancestor"
      ) |>
      dplyr::transmute(area_id, ancestor = parent) |>
      dplyr::filter(!is.na(ancestor))
  }
  pairs
}

read_shipp <- function(path, country, meta) {
  quarter <- shipp_quarter(path, country)
  bands <- shipp_age_groups(path, meta)
  tiles <- attr(bands, "tiles")

  bands_long <- dplyr::bind_rows(lapply(names(SHIPP_SHEETS), function(sex) {
    read_shipp_sheet(path, sex, SHIPP_SHEETS[[sex]], bands, country)
  }))
  unknown <- setdiff(bands_long$area_id, meta$area$area_id)
  if (length(unknown) > 0) {
    stop("SHIPP areas not in the Naomi output: ", paste(utils::head(unknown), collapse = ", "))
  }

  # Groups in only one sex's sheet carry over to `both` unchanged, so within any
  # sex the groups still add up to the whole population.
  with_both <- dplyr::bind_rows(
    bands_long,
    bands_long |>
      dplyr::group_by(area_id, age_group, risk_group) |>
      dplyr::summarise(dplyr::across(dplyr::all_of(SHIPP_COUNTS), sum), .groups = "drop") |>
      dplyr::mutate(sex = "both")
  )

  age_map <- dplyr::bind_rows(lapply(names(tiles), function(group) {
    tibble::tibble(age_group = group, band = tiles[[group]])
  }))
  counts <- with_both |>
    dplyr::rename(band = age_group) |>
    dplyr::inner_join(age_map, by = "band", relationship = "many-to-many") |>
    dplyr::inner_join(area_ancestors(meta$area), by = "area_id", relationship = "many-to-many") |>
    dplyr::group_by(area_id = ancestor, age_group, sex, risk_group) |>
    dplyr::summarise(dplyr::across(dplyr::all_of(SHIPP_COUNTS), sum), .groups = "drop")

  facts <- counts |>
    dplyr::mutate(
      prevalence = plhiv / population,
      incidence = dplyr::if_else(susceptible > 0, infections / susceptible, NA_real_)
    ) |>
    dplyr::select(-susceptible) |>
    tidyr::pivot_longer(dplyr::all_of(SHIPP_INDICATORS), names_to = "indicator", values_to = "mean") |>
    dplyr::filter(!is.na(mean)) |>
    dplyr::mutate(calendar_quarter = quarter)

  year <- as.integer(substr(quarter, 3, 6))
  periods <- period_rows(year, as.integer(substr(quarter, 8, 8)))
  indicators <- indicator_rows(SHIPP_INDICATORS, meta)
  list(
    facts = label_facts(facts, meta, periods, indicators),
    dim_period = periods,
    dim_indicator = indicators,
    dim_risk_group = RISK_GROUPS |> dplyr::filter(risk_group %in% facts$risk_group)
  )
}

# ---- main -------------------------------------------------------------

parse_args <- function(args) {
  valid <- grepl("^--[a-z-]+=.+$", args)
  if (!all(valid)) {
    stop(
      "Usage: Rscript extract_spectrum_shipp.R --country=<ISO3> --naomi=<zip> ",
      "--out-dir=<dir> [--spectrum=<pjnz>] [--shipp=<xlsx>]"
    )
  }
  stats::setNames(as.list(sub("^--[a-z-]+=", "", args)), sub("^--([a-z-]+)=.*$", "\\1", args))
}

main <- function(args) {
  opts <- parse_args(args)
  for (required in c("country", "naomi", "out-dir")) {
    if (is.null(opts[[required]])) stop("--", required, " is required")
  }
  meta <- read_naomi_meta(opts$naomi)
  national <- meta$area |> dplyr::filter(area_level == 0)
  if (nrow(national) != 1 || toupper(national$area_id) != opts$country) {
    stop(opts$naomi, " is not the Naomi output for ", opts$country)
  }

  if (!is.null(opts$spectrum)) {
    spectrum <- read_spectrum(opts$spectrum, meta)
    write_source(spectrum, opts$country, "spectrum", opts[["out-dir"]])
    message(sprintf("%s: %s spectrum rows for %s", basename(opts$spectrum),
                    format(nrow(spectrum$facts), big.mark = ","), opts$country))
  }
  if (!is.null(opts$shipp)) {
    shipp <- read_shipp(opts$shipp, opts$country, meta)
    write_source(shipp, opts$country, "shipp", opts[["out-dir"]])
    message(sprintf("%s: %s shipp rows for %s", basename(opts$shipp),
                    format(nrow(shipp$facts), big.mark = ","), opts$country))
  }
}

if (sys.nframe() == 0) {
  main(commandArgs(trailingOnly = TRUE))
}
