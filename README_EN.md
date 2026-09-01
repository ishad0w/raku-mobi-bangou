# ラク・モビ・バンゴウ

[日本語](README.md) | [English](README_EN.md)

**raku-mobi-bangou** collects Japanese mobile-number candidates by four-digit
mask and ranks spoken sound, visual digit structure, and goroawase separately.

## Quick start

Python 3.10 or newer is required; there are no third-party Python dependencies.
Set the HTTPS JSON endpoint that supplies candidates, then run the collector:

```bash
export PHONE_NUMBER_API_URL="https://example.invalid/path"
python3 raku-mobi-bangou.py --rounds 300
```

To collect selected masks only:

```bash
python3 raku-mobi-bangou.py 1111 2222 --rounds 300
```

The URL cannot contain a query or fragment. The CLI appends `mask=XXXX`.
Run `python3 raku-mobi-bangou.py --help` for every option.

## Collection

- Requests are sequential, with a random 1.1–2.0-second delay between real
  requests. Logical requests to the same mask are at least 30 seconds apart.
- A mask with history targets reobserving 90% of its active coverage pool at the
  start.
  The required response count adapts from historical and current observations.
  Collection stops as soon as the target is reached, or at the per-mask
  `--rounds` cap. The default global cap is 5,000 HTTP attempts including
  retries.
- Every mask first receives five fair probes. The remaining budget is allocated
  from its coverage deficit and recent new-phone yield, with periodic probes to
  prevent low-priority masks from being starved.
- Five consecutive empty responses stop a mask. A mask without history is
  considered saturated after at least 15 successful responses in total and 15
  non-empty responses since the last newly observed phone. A warm mask also
  stops for the invocation after 44 sampled phone slots without a new
  observation; this early stop never counts as negative absence evidence.
- A transient transport or decoding failure gets up to three retries after the
  first attempt. The collector honors `Retry-After` and exponential backoff;
  permanent failures are not retried.
- `phoneNumber + id` pairs are deduplicated. Per-mask CSV files are protected by
  a process lock and replaced atomically.

Standard output contains only the start, progress, completion summary, or a fatal
error. Full details and retries go to `run/logs/`. Endpoint URLs, cookies,
headers, and response bodies are not logged.

## History and non-observation

`run/all_numbers.csv` is not cumulative: it contains only phones returned in
the current invocation. When a previous snapshot exists for the same scan
scope, `run/diff.csv` compares the two. `not_observed` means a randomized
response did not include a known phone this time; it does not mean purchased,
reserved, or unavailable.

An absence becomes negative evidence only for a statistically comparable
scheduled full scan. At most one result per mask and day becomes qualified
evidence. Manual and specialized runs can add or reobserve phones but cannot add
absence evidence. Three consecutive qualified misses produce
`possibly_unavailable`. `statistically_stale` requires at least five days since
the last observation, at least five consecutive qualified misses, and a
10,000:1-equivalent evidence threshold accumulated from the mask's conservative
inclusion probability.

Phones are never physically deleted. A stale phone remains as a tombstone in
`run/lifecycle.csv` and returns with its history intact when reobserved. These
states are statistical signals, not authoritative availability decisions from
the endpoint.

## Output

| Path | Contents |
|---|---|
| `csv/XXXX.csv` | Cumulative per-mask `phoneNumber,id` data |
| `run/all_numbers.csv` | Deduplicated phones observed in this invocation |
| `run/diff.csv` | Difference from the previous snapshot for this scope |
| `run/mask_summary.csv` | Coverage, budget, and stop reason by mask |
| `run/lifecycle.csv` | State of every known phone, including tombstones |
| `run/lifecycle_events.csv` | Audit log of state changes and reappearances |
| `run/logs/` | Complete collector and error logs |
| `run/TOP.md` | Sound/readability and visual-structure rankings |
| `run/GOROAWASE.md` | Goroawase ranking |

Actions artifacts retain the collection results, diff and coverage diagnostics,
logs, a ZIP of per-mask CSV files, and ranking outputs for 30 days. A public
Release attaches only `TOP.md`, `GOROAWASE.md`, and `all_numbers.csv`.

## Masks

[masks.txt](masks.txt) accepts two forms:

```text
1235
1122 | いいふうふ（いい夫婦）
```

- `MASK` — collect for ordinary cadence or a numeric pattern
- `MASK | KANA_READING（ORTHOGRAPHY）` — also provide a goroawase reading that
  accounts for all four digits

A mask with a reading still participates in the ordinary-sound ranking. Blank
lines and lines beginning with `#` are ignored.

## Automated releases

[GitHub Actions](.github/workflows/release.yml) runs a full scan daily at
10:10 Asia/Tokyo. A manual dispatch can override the per-mask cap, set a global
request cap from 1 to 9,000, enable `deep_scan` to bypass repeat-pool early
stopping, and provide a comma-separated mask list such as `1111,2222,3322`.
A specialized run uses its own cache scope and publishes a prerelease.
Setting `skip_collection` with a previous `collection_artifact` reruns ranking
against that exact result without calling the endpoint again.

Only phones in the current `run/all_numbers.csv` are eligible for ranking:

- **TOP 30 — sound and readability**: smooth standard digit reading, rhythm,
  auditory clarity, and memorability
- **TOP 30 — visual digit structure**: clear repetition, palindromes, mirrors,
  sequences, and cross-block patterns
- **TOP 30 — goroawase**: Japanese wordplay using readings from
  [masks.txt](masks.txt)
- **TOP 10 — newly found numbers**: ordinary-sound ranking of
  phones absent from the previous snapshot for the same scope; shown in release
  notes only when at least ten candidates exist

A full scan selects 30 entries for each of the three main rankings. A specialized
run uses all available candidates when fewer than 30 exist.

Python builds separate reproducible sound and visual shortlists and limits
overrepresentation by one mask family. Codex ranks candidate IDs only. Python
resolves them against the current snapshot, validates IDs, counts, readings, and
family caps, then renders Markdown. An invalid selection gets up to three
attempts.

Collection, ranking, and publication are separate jobs. If ranking fails,
**Re-run failed jobs** reuses the collection artifact without calling the
endpoint again. A full release uses Tokyo time `YYYY-MM-DD_HH-MM` as both tag
and title, and its body always starts with `# ラク・モビ・バンゴウ`.

State is stored in an Actions cache with schema v3. A missing, corrupt, or
incompatible cache starts from an empty state. The repository contains no phone
number seed.

Pushes and pull requests run unit tests and workflow lint without performing a
network collection.

## GitHub setup

Add `PHONE_NUMBER_API_URL` to the repository's Actions secrets.
