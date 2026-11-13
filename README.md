# iGOT Course Metadata Enrichment

Regenerates standardised metadata for iGOT Karmayogi Bharat courses with Vertex AI Gemini. Each course's
output is grounded in its own transcripts and PDFs and in three master datasets:

- **KCM**: Functional and Behavioural competencies.
- **SGOS**: Domain sectors, subsectors and themes.
- **iGOT designation master**: the designations allowed in `Targetroles`.

> **Scope of this README: the prompt up to v3.5 (`v3.5-advanced`).** It covers the v3.4 → v3.5 changes and
> how the v3.5 pipeline works. Later versions are out of scope. Note that `meta_gen.py` and `validation.py`
> now import `prompts_v36`, so a plain run no longer uses the v3.5 prompt. `prompts_v35.py` is kept
> unchanged as the baseline. For v3.6 → v3.7, see [docs/prompt_audit.md](docs/prompt_audit.md).

---

## Contents

- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [How a course is processed (v3.5)](#how-a-course-is-processed-v35)
- [Prompt changes: v3.4 → v3.5](#prompt-changes-v34--v35)
- [The v3.5 prompt, section by section](#the-v35-prompt-section-by-section)
- [Output contract (v3.5)](#output-contract-v35)
- [Editing the prompt](#editing-the-prompt)
- [Known gaps at v3.5](#known-gaps-at-v35)
- [Further reading](#further-reading)

---

## Repository layout

```text
igot-course-metadata/        the code; run everything from here
  meta_gen.py                worker: discovery, queue, Gemini calls, persistence, CLI
  prompts_v35.py             v3.5 system prompt, response schema, rubric weights, prompt assembly
  course_io.py               reads course folders: nested transcripts, PDFs, placeholder filtering
  validation.py              master-membership checks, rubric recomputation, repairs
  designations.py            designation embedding index and shortlist retrieval
  api_fetcher.py             --api mode: fetch course content from the Karmayogi Learning API
  data/designation_index.npz prebuilt designation embedding index
  tools/                     inventory, index builder, export, cost estimate, HTML review page
  run_*.ps1                  Windows wrappers for the SCORM, non-SCORM and ad-hoc course sets
  legacy/meta_gen_v3.4.py    the v3.4 implementation, kept for diffing only (do not run)
  README.md                  detailed operations guide (data census, cost, resume, exports)
input/masters/               KCM, SGOS, designation and STO masters
docs/                        prompt audit (v3.6 → v3.7, outside this README's scope)
logs/, runs/                 run logs, outcome CSVs and sample run outputs
SETUP.md                     step-by-step setup for a fresh machine (Windows/PowerShell)
```

Never commit `.env`, `.creds/` or the service-account JSON.

---

## Quick start

Full instructions, including Windows paths, are in [SETUP.md](SETUP.md).

```bash
cd igot-course-metadata
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.12+
cp .env.example .env
```

Fill in these values in `.env`:

| Variable | Value |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account JSON with the Vertex AI User role |
| `GOOGLE_PROJECT_ID` | The GCP project that account belongs to |
| `DB_DSN` | A writable Postgres database (plain libpq DSN). Tables are created on first run |
| `KARMAYOGI_API_KEY` | Needed only for `--api` mode |

`ENRICHMENT_DSN` (read-only `course_metadata_v3`) is optional. Without it, language and duration are
inferred.

Every command is a **dry run** until you add `--execute`. A dry run makes no AI calls and no database writes.

```bash
python meta_gen.py --api --course-id do_123 --limit 1             # dry run
python meta_gen.py --api --course-id do_123 --execute             # real run, one course
python meta_gen.py --api --course-id-file ids.txt --execute       # one id per line
python meta_gen.py --limit 10 --execute                           # local folders (COURSES_BASE_PATH)
python tools/review_report.py --limit 50 --out review.html        # HTML page for reviewers
```

These commands run the **current** prompt (v3.6), not v3.5. You can't switch back to v3.5 by changing the
import alone. v3.5's `build_course_section()` takes `current_metadata`, `authoritative_facts` and raw PDF text.
v3.6's takes `pdf_summaries`, `output_language` and `duration_seconds`. Existing v3.5 records stay in
`course_metadata_regenerated` under `llm_prompt_version = 'v3.5-advanced'`.

---

## How a course is processed (v3.5)

1. **Read metadata.** The pipeline loads `metadata.json`. It removes `competencies_v6` from what the model
   sees, because the area must be judged from content. The value is kept for audit.
2. **Extract content.** Transcripts are collected from module subfolders, not the course root. The
   course-root VTT is a placeholder in every non-SCORM course. PDFs are found two levels down and read
   with PyMuPDF. Scanned or legacy-font PDFs are attached as files instead of extracted text.
3. **Assign an evidence tier:** `transcript`, `thin_transcript`, `pdf_only`, `links_only` or
   `metadata_only`. The tier caps the confidence the model may report.
4. **Gather authoritative facts.** Language, duration and provider come from `course_metadata_v3` where
   available. These values override the model.
5. **Shortlist designations.** The course text is embedded and matched against the 19,936-row master.
   The top 150 candidates go to the model, which may choose only from them.
6. **Generate** the record with one Gemini call that uses a structured response schema.
7. **Validate and repair** in `validation.py`. The model's `TotalScore` is never trusted. Invalid KCM pairs,
   SGOS paths and designations are dropped. Every correction is recorded in `explain.validation_issues`.
8. **Persist** the result to `course_metadata_regenerated`, keyed on `(course_id, llm_prompt_version)`. v3.4 and
   v3.5 rows coexist.

---

## Prompt changes: v3.4 → v3.5

### Inputs the model now receives

| Input | Change |
|---|---|
| `authoritative_facts` | **New.** Platform-verified language, duration and provider. The prompt says they override inference |
| `evidence_tier` | **New.** Tells the model how much real content it has. `metadata_only` caps content-derived confidence at 0.70 and forces an empty `TranscriptAnalysis` |
| `designation_candidates` | **New.** A shortlist of 150 from the designation master. `Targetroles` must come from it, verbatim |
| `transcript_text` / `pdf_texts` | Now actually populated. v3.4 read only the course root and sent a placeholder string as the transcript |
| `course_metadata` | `competencies_v6` is withheld, and the prompt tells the model to ignore any declared area |
| `kcm_json` | Reduced by `compact_competency_master()` to one entry per Theme/SubTheme pair with its two definitions (from ~815 KB) |

### New global rules

- **Output language** follows `authoritative_facts.language`. The transcript is often an English
  translation of non-English audio, so its language is not the course language.
- **No placeholder strings.** `"Not Applicable"`, `"N/A"`, `"None"` and `"Unknown"` are banned as values. Use `null` or `[]`.
  In v3.4, 214 of 368 rows had `"Not Applicable"` in sector fields.
- **The provider name is stripped** from every generated field, including Tags.
- **`explain` must be fully populated.** An empty audit trail counts as a failed response.

### Section-level changes

| Area | v3.4 | v3.5 |
|---|---|---|
| **Primary competency area** | Implied | Its own section, **decided first** from content only. Exactly one of Domain / Functional / Behavioural, with `reason` and `confidence` |
| **KCM mapping** | "Use KCM" | Hard rules learned in production: copy values character for character, **no cross-mixing** a theme from one entry with a sub-theme from another, **no field swapping**, **no cross-category** mapping. Main arrays need ≥ 0.85 confidence; 0.70–0.85 goes to `SuggestiveCompetencies`; anything lower is omitted |
| **Domain / SGOS** | Sector fields required, but forbidden for non-Domain courses, so the model wrote `"Not Applicable"` | SGOS is the only Domain source (KCM has no Domain entries). Sector, SubSector and SubSectorTheme are nullable and must form a real SGOS path. The SGOS choice is mirrored into `DomainCompetencies` |
| **Target employee groups** | `{RoleBands, Designations}` object; designations as bare strings | A flat array of 1–3 values from the closed set **Group A / B / C / D**, using Indian government seniority criteria (A/B gazetted and senior officers; C/D supporting, clerical and operational staff) and decided by content depth, not subject alone |
| **Target roles** | — | **New `Targetroles`.** 3–6 `{DesignationId, Name, Confidence, Rationale}` objects taken only from the shortlist. `[]` if nothing fits; never invented |
| **STO level** | — | **New section 10, `STOLevel`.** Exactly one of Strategic / Tactical / Operational, judged within the mapped competency theme and sub-theme by what the learner will *do*. Includes an evidence hierarchy, a rationale contract, counter-signals, verbatim evidence phrases and a final self-check. Independent of `LearningLevel` |
| **Transcript analysis** | Schema flattened the requested evidence to strings, so it was discarded | Typed: `CognitiveMarkers` {Verb, ExamplePhrase, EstimatedLevel 1–6, Confidence}; `CompetencySignals` with `TriggerPhrases` (≥ 0.70 only); empty when there is no transcript |
| **Rubric** | Weights LO5/PK5/BT25/CC35/ERO10/EOL20/**TAA0**; bands 55/75; model's total trusted | Spec weights **LO10/PK5/BT10/CC15/ERO20/EOL25/TAA15**; bands ≤45 Beginner, ≤75 Intermediate, otherwise Advanced. Zero-base scoring kept. `TotalScore`, `Classification` and `LearningLevel` are recomputed in Python |
| **Duration** | Model's guess | Rendered from `authoritative_facts.duration_seconds`; inferred only when that is null |
| **Reference resources** | — | **New.** Only URLs present in the inputs; never fabricated |
| **SCORM** | One env flag for the whole run | Read per course. Missing content goes in `explain.missing_info`, and affected confidences are capped at 0.70 |
| **`explain`** | Sub-objects untyped, so Gemini returned `{}` in 368 of 368 rows | Fully typed: `primary_area_reason`, `sgos_reason`, `competency_mapping_triggers`, `mapping_reasons`, `designation_inference`, `sto_reason`, `learning_outcomes_validation`, `missing_info`, `evidence_tier_used` and others |

### Prompt layout for caching

The system rules and both masters form a **byte-identical prefix** on every request (`build_static_prefix`).
Per-course data comes strictly after it (`build_course_section`). v3.4 put course data first, which made
prefix caching impossible. On a full run, caching the prefix is worth about 55% of spend.

### Deliberately unchanged

- **Learning outcomes stay unlabelled** (5–6 clean, measurable outcomes). The spec's `Why:`/`How:`/`What:` labels
  are available with `LEARNING_OUTCOME_STYLE=why_how_what`.
- **"Behavioural" keeps the British spelling** used in the KCM master. Use `BEHAVIOURAL_SPELLING=Behavioral`
  if a consumer needs the spec spelling.
- **The v3.4 zero-base rubric evidence rules are kept.**

---

## The v3.5 prompt, section by section

`SYSTEM_PROMPT_TEMPLATE` in [prompts_v35.py](igot-course-metadata/prompts_v35.py):

| § | Section | Produces |
|---|---|---|
| — | Inputs, Global rules | Source restrictions, language, placeholders, provider stripping, confidence calibration |
| 1 | Core text fields | `CourseName` (6–12 words), `CourseSummary` (80–100 words), `CourseDescription` (150–250 words), `Tags` (10–15) |
| 2 | Primary competency area | `PrimaryCompetencyArea` |
| 3 | Functional and Behavioural competencies | `FunctionalCompetencies`, `BehaviouralCompetencies`, `SuggestiveCompetencies` |
| 4 | Domain competencies and SGOS | `Sector`, `SubSector`, `SubSectorTheme`, `DomainCompetencies` |
| 5 | Prior knowledge declared | `PriorKnowledgeDeclared` |
| 6 | Suggestive prior knowledge | `SuggestivePriorKnowledge` (only if nothing declared and confidence ≥ 0.75) |
| 7 | Learning outcomes | `LearningOutcomes` |
| 8 | Transcript analysis | `TranscriptAnalysis` |
| 9 | Target groups and roles | `TargetEmployeeGroups`, `Targetroles` |
| 10 | STO level classification | `STOLevel` |
| 11 | Rubric scoring | `RubricScoring`, `LearningLevel` |
| 12 | Learning mode and duration | `LearningMode`, `Duration` |
| 13 | Reference resources | `ReferenceResources` |
| 14 | SCORM and missing content | Rules for thin-evidence courses |
| 15 | Explain object | `explain` |
| 16 | Output | One JSON object, UTF-8, ISO 8601 timestamps |

---

## Output contract (v3.5)

Top-level keys: `Do_ID`, `CourseName`, `CourseSummary`, `CourseDescription`, `LearningOutcomes`, `LearningLevel`,
`LearningMode`, `Duration`, `Language`, `Sector`, `SubSector`, `SubSectorTheme`, `DomainCompetencies`,
`PrimaryCompetencyArea`, `FunctionalCompetencies`, `BehaviouralCompetencies`, `SuggestiveCompetencies`, `Tags`,
`TargetEmployeeGroups`, `Targetroles`, `STOLevel`, `PriorKnowledgeDeclared`, `SuggestivePriorKnowledge`,
`RubricScoring`, `TranscriptAnalysis`, `ReferenceResources`, `EmbeddingVectorID`, `Version`, `GeneratedOn`,
`Generator`, `explain`.

Branch rules, enforced in validation:

| Primary area | Sector / SubSector / SubSectorTheme | DomainCompetencies | Functional | Behavioural |
|---|---|---|---|---|
| `Domain` | from SGOS | mirrors the SGOS choice | `[]` | `[]` |
| `Functional` | `null` | `[]` | from KCM | `[]` |
| `Behavioural` | `null` | `[]` | `[]` | from KCM |

Example `Targetroles` entry. Every id is checked against the designation master:

```json
{ "DesignationId": "DESG-001578", "Name": "Food Analyst", "Confidence": 0.7,
  "Rationale": "Directly responsible for analyzing food samples, aligning with the course's focus on grain analysis." }
```

For more real v3.4 vs v3.5 output, see
[igot-course-metadata/README.md#output-examples](igot-course-metadata/README.md#output-examples).

---

## Editing the prompt

All prompt text lives in `prompts_v35.py`, not in the worker, the validator or the extraction code.

| What | Where |
|---|---|
| System prompt | `SYSTEM_PROMPT_TEMPLATE` |
| Learning-outcome variants | `_LO_RULES_UNLABELLED`, `_LO_RULES_WHW` |
| Response schema | `METADATA_SCHEMA` |
| Rubric weights and bands | `RUBRIC_WEIGHTS`, `BEGINNER_MAX`, `INTERMEDIATE_MAX`. The prompt text and the Python recomputation both read these constants, so the two cannot drift apart |
| Assembly | `build_static_prefix()` (invariant) + `build_course_section()` (per course) |

Env overrides that change the output (see `.env.example`):

| Variable | Default |
|---|---|
| `PROMPT_VERSION` | `v3.5-advanced`. A new value starts a separate generation alongside the old one |
| `RUBRIC_WEIGHTS` | blank = `LO:10,PK:5,BT:10,CC:15,ERO:20,EOL:25,TAA:15` (must sum to 100) |
| `RUBRIC_BEGINNER_MAX` / `RUBRIC_INTERMEDIATE_MAX` | `45` / `75` |
| `LEARNING_OUTCOME_STYLE` | `unlabelled` (or `why_how_what`) |
| `BEHAVIOURAL_SPELLING` | `Behavioural` (or `Behavioral`) |

Keep `build_static_prefix()` identical across courses. Anything course-specific in it breaks caching.

---

## Known gaps at v3.5

- **About 41% of non-SCORM courses are `metadata_only`.** They have no transcript or PDF, so confidence is capped
  at 0.70 and transcript analysis is empty.
- **Role bands cannot be validated.** The designation master has only `id,name`, so Group A/B/C/D stays model-inferred.
- **STO level is defined in the prompt only.** `input/masters/sto_competencies.json` exists, but v3.5 does not pass
  it to the model.
- **Prompt caching is intermittent.** Hits have been seen, but most calls report zero cached tokens.
- **`EmbeddingVectorID` is declared but never populated.**
- **312 courses are missing from `course_metadata_v3`.** They fall back to inferred language and duration.

---

## Further reading

| Document | Covers |
|---|---|
| [SETUP.md](SETUP.md) | Fresh-machine setup and the first run |
| [igot-course-metadata/README.md](igot-course-metadata/README.md) | Full v3.5 operations guide: input data census, validation table, cost and throughput, resume, exports, review page |
| [igot-course-metadata/legacy/README.md](igot-course-metadata/legacy/README.md) | v3.4 defects, with line references |
| [igot-course-metadata/.env.example](igot-course-metadata/.env.example) | Every configuration variable, documented |
| [docs/prompt_audit.md](docs/prompt_audit.md) | v3.6 → v3.7 prompt audit (after v3.5) |
