# CSRankings Query

`csrankings_query.py` is a self-contained Python CLI for querying CSRankings publication counts for a university, filtered by explicit conference venue names.

It fetches raw CSRankings data from GitHub, caches responses locally, fuzzy-matches institution names, and reports publication totals plus global rank for the selected venues.

The public website at [csrankings.org](https://csrankings.org/) provides interactive access to these rankings in the browser. This script is a CLI-oriented way to query the underlying published data.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) to run the script with inline dependencies

The script declares its dependencies internally, so there is no separate install step for local use.

## Run It

**1. `uvx`** (recommended):

```sh
uvx --from 'git+https://github.com/vzaliva/CSRankings-query' \
  csrankings-query Stanford --conferences=NeurIPS,ICML,ICLR
```

**2. `uv tool install`**:

```sh
uv tool install --from 'git+https://github.com/vzaliva/CSRankings-query' csrankings-query
csrankings-query Stanford --conferences=NeurIPS,ICML,ICLR
```

## Local Usage

For local development in a checked-out repository:

```bash
uv run csrankings_query.py <university_name> \
  --conferences=<conf1>,<conf2>[,<conf3>...] \
  [--json] \
  [--refresh] \
  [--country=US] \
  [--max-matches=1] \
  [--threshold=SCORE]
```

Example:

```bash
uv run csrankings_query.py Stanford --conferences=NeurIPS,ICML,ICLR
```

JSON output:

```bash
uv run csrankings_query.py Stanford --conferences=NeurIPS,ICML,ICLR --json
```

Country-filtered output:

```bash
uv run csrankings_query.py CMU --country=US --conferences=POPL,OOPSLA,ICFP
```

Fail fast on ambiguous short names:

```bash
uv run csrankings_query.py CMU --conferences=POPL,OOPSLA,ICFP --max-matches=1
```

## Arguments

- `university_name`: Free-text institution name, fuzzy-matched against CSRankings affiliations.
- `--conferences`: Comma-separated venue shorthand names such as `NeurIPS,ICML,ICLR`.
- `--json`: Emit JSON to stdout instead of plain text.
- `--refresh`: Clear cached responses and fetch all remote data again.
- `--country`: Optional country filter, as an ISO alpha-2 code such as `US` or a country name such as `Switzerland`.
- `--max-matches`: Optional maximum number of matched affiliations to allow before exiting with an ambiguity error.
- `--threshold`: Minimum fuzzy-match score from `0` to `100`. Default is `50`.

`--country` narrows the affiliation candidates before fuzzy matching. Ranking remains global across all institutions for the selected venues.

## Data Sources

The script fetches data from the CSRankings GitHub repository and does not scrape `csrankings.org` directly.

- Faculty CSVs: `csrankings-a.csv` through `csrankings-z.csv`
- Publication counts: `generated-author-info.csv`
- Compatibility fallback for publication counts: `generated/author-info.json` when available
- Institution country metadata: `institutions.csv` and `countries.csv`
- Venue mapping: `generated/venue_to_area.json` when available
- Fallback venue mapping source: `csrankings.ts` by parsing the `areadict` object

## How Ranking Works

For each matched institution:

1. Faculty are selected from the publication dataset by exact `affiliation`.
2. Publication counts are summed only for the requested conference names.
3. The institution score is compared against every other institution scored with the same conference set.
4. Results are sorted descending to compute the global rank.

Conference matching is case-insensitive.

## Fuzzy Matching Behavior

- Institution matching uses `rapidfuzz` against all unique affiliation values.
- All matches at or above `--threshold` are returned.
- If multiple affiliations exceed the threshold, the script emits one result per matched institution.
- If the best match is below `80` but above the threshold, the script warns on `stderr` and continues.
- If nothing meets the threshold, the script exits with code `1`.

## Caching

- Cache directory: `~/.cache/csrankings/`
- Cache key: `sha256(url).cache`
- TTL: 24 hours
- `--refresh` deletes known cache entries before refetching

If a non-required fetch fails and no cache is available, the script warns and skips that source. If the publication data cannot be fetched from either `generated-author-info.csv` or `generated/author-info.json`, the script exits because scoring depends on it.

## Output

Default output is plain text. Use `--json` for machine-readable output.

Each result includes the matched institution's country. Plain-text output shows the country name, while JSON output uses the ISO alpha-2 country code when available.

Warnings are written to `stderr` so JSON output on `stdout` stays clean.
