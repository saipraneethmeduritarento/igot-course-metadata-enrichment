# Running the iGOT course metadata generator (v3.5)

This bundle holds the code, the reference master data and a prebuilt designation
index. You supply credentials and course content.

```
igot-course-metadata/          the code (run everything from here)
  src/scripts/<version>/       one runnable engine per version (v3.5, v3.6, v3.7)
  src/prompts/<version>.yaml   the prompt text for each version
  data/designation_index.npz   prebuilt designation embedding index
input/
  masters/                     KCM, SGOS and designation masters
```

Keep `input/` next to `igot-course-metadata/`. The run scripts and the
`.env.example` paths expect that layout.

## 1. Install (Python 3.12+; built on 3.14)

```powershell
cd igot-course-metadata
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Keep the venv at `.venv`: the `run_*.ps1` scripts call `.venv\Scripts\python.exe`.

## 2. Configure

```powershell
copy .env.example .env
```

The master-data paths are already set. Fill in these four values in `.env`:

| Variable | What |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a service-account JSON with the Vertex AI User role |
| `GOOGLE_PROJECT_ID` | The GCP project that service account belongs to |
| `DB_DSN` | A PostgreSQL database you can write to. Tables are created on first run |
| `KARMAYOGI_API_KEY` | Only for `--api` mode: the literal `Bearer <token>` string |

`ENRICHMENT_DSN` is optional. Leave it blank if you don't have the read-only
`course_metadata_v3` database; language and duration are then inferred.

Don't change `DESIGNATION_EMBED_MODEL` / `DESIGNATION_EMBED_DIM`: the bundled
index was built with `text-embedding-005` at 768. Changing either means
rebuilding it with `python src/scripts/tools/build_designation_index.py --force`.

## 3. Get course content

Course content (several GB) isn't in the bundle. Choose one:

- **From the Learning API**: needs only `KARMAYOGI_API_KEY` and course ids.
  ```powershell
  python src/scripts/v3.6/meta_gen.py --api --course-id do_123... --execute
  python src/scripts/v3.6/meta_gen.py --api --course-id-file ids.txt --execute   # one id per line
  ```
- **From local folders**: put the extracted courses under `input/courses/`,
  set `COURSES_BASE_PATH` (and ideally `COURSE_MANIFEST`) in `.env`, or use
  `.\run_courses.ps1 -CourseDir <folder>`, which builds the manifest itself.

## 4. Run

Every command is a dry run (no AI calls, no database writes) until you add
`--execute` / `-Execute`.

```powershell
python src/scripts/v3.6/meta_gen.py --api --course-id do_123... --limit 1                 # dry run
python src/scripts/v3.6/meta_gen.py --api --course-id do_123... --execute --run-dir out   # real
```

`--run-dir out` writes each finished course to `out/courses/<id>.json`. To view
results in a browser, build a review page from the database:

```powershell
python src/scripts/tools/review_report.py --ids-from out/records.jsonl --out review.html
```

The prompt text is in `src/prompts/<version>.yaml`; the output schema is in
`src/scripts/<version>/prompts.py`. `README.md`
covers everything else.
