# Cloud results sink — do you have a bucket? (default is local)

A `run`'s results are written **locally** by default — under this session's workspace, where
the report parser, the analyzer, and the trend store can all find them. That is the right
choice for almost everyone, and it is what happens when you say nothing.

A user with their OWN cloud bucket can instead have results uploaded to **GCS (`gs://…`)** or
**S3 (`s3://…`)**. This is an **opt-in** convenience, not a default — never silently send a
user's benchmark results to a cloud bucket.

## The judgment — when to opt in to a cloud sink

- **Default: LOCAL.** Omit `output` (or set it to `local`). Results stay under this session.
  Do this whenever the user hasn't asked for a bucket. Never guess a bucket URI.
- **Opt in to `gs://`/`s3://` ONLY when the user explicitly says so** — e.g. "upload the
  results to my GCS bucket", "put them in `s3://acme-benchmarks/…`", "I have a bucket for this".
  If the conversation hints at sharing/retaining results off-box but no bucket is named, **ask**:
  > "Do you have a GCS or S3 bucket you'd like results uploaded to? If not, results stay local
  > under this session."
  Don't fabricate a bucket name; only emit a URI the user actually gave you.
- **Scope: `run` only (for the MVP).** A cloud sink is wired for `execute_llmdbenchmark` `run`.
  `experiment` (DoE sweeps) and `results` stay LOCAL here — don't try to point those at a bucket.

## How to set it (mechanism — for grounding, not decisions)

- LOCAL (default): `execute_llmdbenchmark(subcommand="run", …)` with no `output` — the tool
  defaults `flags["output"]` to `"local"` and anchors the report under the session workspace.
- CLOUD (opt-in): `execute_llmdbenchmark(subcommand="run", flags={"output": "gs://my-bucket/prefix"})`
  — or `"s3://my-bucket/prefix"`. The URI is passed verbatim to the CLI's `-r/--output`.
- `-r/--output` is a **destination KEYWORD**, not a filesystem path: it is `local`,
  `gs://bucket/…`, or `s3://bucket/…`. Passing an absolute path makes the run fail
  ("Unknown output destination: …"). The allowlist value constraint `results_sink`
  (`security/allowlist.yaml`) permits exactly those three shapes on `run`'s `-r/--output`.

## Credentials and the upload (important)

- The **upload itself runs INSIDE the benchmark CLI subprocess** (`gcloud storage cp` /
  `aws s3 cp`), not in this agent. It uses the **user's OWN cloud credentials** already
  configured in their environment (e.g. an ADC / `gcloud auth`, or AWS env/credentials). The
  agent does NOT provision, hold, or forward cloud credentials, and never sees the bucket
  contents. (The upload helper internals are the deferred Phase 47 — not built here.)
- If the user opts in but their credentials aren't set up, the **upload** can fail at the end
  even though the benchmark itself ran fine. Treat that like an access/credentials problem
  (tell them to configure `gcloud`/`aws` auth for that bucket), not a benchmark failure — the
  results still exist; only the off-box copy didn't land.

## Reading the results

When you send results to a cloud bucket, **say so** in your summary and name the destination
(`gs://…`/`s3://…`) so the user knows where to find them. The metrics are identical to a local
run — only the storage location changed.
