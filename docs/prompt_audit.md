# Course metadata prompt audit (v3.6 → draft v3.7)

**Scope.** This audit covers the LLM prompts the course metadata pipeline uses to extract course-level fields, read against the agreed field list (groups A–D). It is read-only: no existing code or prompt was changed. The draft replacement is a new, unwired module, **`prompts_v37.py`**. It keeps v3.6's template, single call and public API, and adds the fixes and new fields from this audit (§6.0). It can replace v3.6 by changing one import each in `meta_gen.py` and `validation.py`, which still import `prompts_v36`.

**Updates (Nov 2026):**

- Group D is confirmed as a valid employee group. It is no longer listed as a contradiction (§2.1, §3.1 issue 6, Q1).
- The pipeline stays a **single LLM call** per course (§4). A chained multi-call design was drafted and dropped.

**Path convention.** Unless stated otherwise, code paths are relative to `igot-course-metadata/`. Data paths (`input/`, `runs/`, `OneDrive_2026-11-06/`) are relative to the repo root.

**Evidence used.** Source code; the masters in `input/masters/`; the v3.6 sample run in `runs/sample10/` (10 courses, prompts dumped); the stakeholder-annotated output `runs/single/courses/do_11381269163759206411.json`; the portal export `OneDrive_2026-11-06/course_metadata.xlsx` (5,603 courses); the v3.5 field review `all_courses_flattened_fields.csv` (5,130 records).

---

## 1. Inventory of current prompts

### 1.1 LLM calls in the live pipeline

| # | Prompt | Where | Model / params | Inputs | Output and parsing |
|---|---|---|---|---|---|
| P1 | **Main metadata prompt** (one call per course) | System rules: `prompts_v36.py:198-773`. LO sub-rules: `:776-789`. Static prefix (rules + masters): `build_static_prefix` `:1207-1234`. Per-course tail: `build_course_section` `:1245-1302`. Called from `meta_gen.py:495-554` (`LLMClient.generate`), invoked at `meta_gen.py:834`. | `gemini-3.1-pro-preview` @ `global` (`meta_gen.py:85-90`). temperature 0.1 (`:97`), max_output_tokens 32,768 (`:98`), timeout 300 s (`:101`), 3 attempts (`:95`). | Rules; full compacted KCM (114 pairs, ~116k chars); full SGOS (11 sectors / 92 subsectors / 460 themes, ~82k chars); evidence_tier; output_language; duration_seconds; 150 designation candidates (id + name); transcript (≤200k chars); PDF summaries (≤120k chars). **No portal metadata** (v3.6 design, `prompts_v36.py:8-11`). | JSON mode with `response_schema = METADATA_SCHEMA`: `FULL_METADATA_SCHEMA` `:817-1120`, pruned by `field_config.json` at `:1184-1186`. `json.loads` (`meta_gen.py:539`); a decode error counts as transient and is retried (`meta_gen.py:378-379`). Repair-then-report pass in `validation.validate_and_repair` (`validation.py:294-574`). |
| P2 | **PDF chunk summary** (one call per ≤12k-char chunk, ≤40 per course; one call per scanned PDF) | `SUMMARY_INSTRUCTIONS` `pdf_summarizer.py:46-59`. Text parts `:206-209`; native-PDF parts `:221-227`. Call: `:115-149`. | `PDF_SUMMARY_MODEL`, defaulting to the main model (`meta_gen.py:125`). temperature 0.1, max_output_tokens 8,192 (`pdf_summarizer.py:42`). | Instructions + one chunk of PDF text, or the PDF bytes. | Free text. The sentinel `NO_CONTENT` is dropped. A failed chunk becomes `""` silently (`:157-162`). Results are cached by content hash plus `SUMMARY_PROMPT_VERSION = "s1"`. |
| E1 | Designation shortlist (embedding, not generation) | Index build `designations.py:161-189`; retrieval `:228-266`; query text `:327-334`. | `text-embedding-005`, 768-d (`designations.py:53-54`). | **Designation names only** vs. a query made of the first 2,000 transcript chars + first 3,000 PDF-summary chars (truncated to 8,000). Union with a lexical pass (30). | Top 150 `{id, name}` go into P1. The model's picks are validated against the master (`designations.py:270-299`). |

### 1.2 Not live, for reference only

- `prompts_v35.py` is the v3.5 baseline. It passes `course_metadata` and `authoritative_facts` to the model. No current module imports it.
- `legacy/meta_gen_v3.4.py:83-384` holds the v3.4 prompt (`gemini-2.5-pro`). It is superseded.

### 1.3 Field configuration that shapes P1

`field_config.json` decides which fields P1 is asked to generate:

- **portal:** `CourseName`, `CourseDescription`, `Duration`, `LearningOutcomes`
- **code:** `Language`, `LearningLevel`, `RubricScoring.TotalScore/Classification`, `DomainCompetencies`, `ReferenceResources`, …
- **off:** `explain.primary_area_reason`, `explain.sto_reason`, `explain.designation_inference`, `TranscriptAnalysis.CompetencySignals`, …

After pruning, P1's schema has 18 top-level properties: `BehaviouralCompetencies`, `CourseSummary`, `FunctionalCompetencies`, `LearningMode`, `PrimaryCompetencyArea`, `PriorKnowledgeDeclared`, `RubricScoring`, `STOLevel`, `Sector`, `SubSector`, `SubSectorTheme`, `SuggestiveCompetencies`, `SuggestivePriorKnowledge`, `Tags`, `TargetEmployeeGroups`, `Targetroles`, `TranscriptAnalysis`, `explain`.

### 1.4 Chunking and truncation

| Stage | Limit | Behaviour |
|---|---|---|
| Transcript assembly | 400k chars (`course_io.py:44, 315-318`) | Modules are appended in order. **Later modules are dropped** once the cap is hit. |
| Transcript in P1 | 200k chars (`meta_gen.py:130`, `prompts_v36.py:1237-1242`) | Head truncation. |
| PDF text | 12k-char chunks, ≤40 chunks per course, round-robin across documents (`pdf_summarizer.py:33-40, 178-199`) | Chunks beyond 40 are listed as "not summarised". |
| PDF summaries in P1 | 120k chars (`meta_gen.py:131`) | Head truncation. |
| Designation query | first 2k transcript chars + 3k summary chars (`designations.py:333`) | Only the **opening** of the course drives the shortlist. |

**Measured size.** The static prefix is 232,858 chars (about 58k tokens). The rules alone are 35.9k chars, and the STO section is 19.2k of that (54%). Sample10 prompt tokens ran 57.8k–97.6k per course. Implicit cache hits occurred on 1 of 9 calls (`runs/sample10/run.log`).

---

## 2. Field mapping: target vs current

### 2.1 Group A — LLM extraction

| Target field | Current (v3.6) | Gap |
|---|---|---|
| **summary** | `CourseSummary`: 3-4 sentences, 80-100 words (`prompts_v36.py:245-248`). Sample10 averages 80 words. | **Major.** No author details, topics, skills or takeaways. It is a single short paragraph built from content only, with no title or outcomes, so the course embedding has little to work with. |
| **tutor name** | — | **Missing.** `api_fetcher.py:340-354` does not save `creatorContacts` or `author`. The stakeholder draft adds `AuthorName`. |
| **target_employee_group** | `TargetEmployeeGroups` (`:338-352`). Allows **Group A/B/C/D**. | Group D is valid (confirmed Nov 2026). No per-group confidence or rationale. The schema has no enum (`:880`) and validation accepts any non-placeholder string (`validation.py:420-426`). The repo text lumps A with B and C with D; nothing separates A from B. |
| **target_designations_applicable_for** | `Targetroles` (`:352-356`) | **The instruction header was lost in v3.6.** v3.5 says "Targetroles: 3-6 specific designations, chosen ONLY from `designation_candidates`" (`prompts_v35.py:348-350`). v3.6 begins mid-sentence at "DesignationId and Name exactly as given" (`prompts_v36.py:352-353`), so the field has no count and no lead instruction. |
| **target_designations_not_applicable_for** | — | **Missing.** The stakeholder draft has `Targetrolesexclusions: []`. |
| **target_responsibilities_fulfilled** | — | **Missing.** The closest thing is the `ExpectedRoleOutcome` rubric number (`:725-728`). |
| **competency area** | `PrimaryCompetencyArea` {name, reason, confidence} (`:256-261`) | The existing portal value is stripped (`course_io.py:486`) and used only for a post-hoc agreement flag (`meta_gen.py:854-859`). An invalid value is **silently defaulted to Domain** (`validation.py:325-328`). |
| **sector / sub-sector** | `Sector`, `SubSector`, **`SubSectorTheme`** (`:290-305`) | v3.6 maps **three** levels. The target list and the portal's existing values have **two**: for Domain courses, portal "themes" are SGOS sectors (3,577/3,591) and "sub-themes" are subsectors. There is no confidence or rationale per field; the reason sits in `explain.sgos_reason`. |
| **competency theme / sub-theme** | `FunctionalCompetencies` / `BehaviouralCompetencies` (`:263-288`) | Main arrays have **no confidence or rationale**; only `SuggestiveCompetencies` has them. |
| **difficulty level** | `LearningLevel` = rubric classification **computed in code** (`validation.py:446-485`). Labels: Beginner / Intermediate / Advanced. | The portal labels are Beginner (4,289) / Intermediate (1,148) / Advanced (60) / **Expert (1)** / None (105). The field list says basic/intermediate/advanced: **flagged**. The rubric's LO and BT parameters (20% of the weight) score outcomes the model never sees (see 3.1, issue 2). |
| **pre-requisites** | `PriorKnowledgeDeclared` + `SuggestivePriorKnowledge` (`:307-321`), with different level enums | Split into two objects; the stakeholder draft merges them into one `Prerequisites` with `Recommended_by`. `SuggestedLearningPath` must use "titles present in the inputs", and **v3.6 inputs contain no titles**. There is no skills/tools/knowledge split. |
| **STO level** | `STOLevel` (`:359-702`) | Thorough, but anchored to "the mapped Theme/Subtheme", which Domain courses don't have. **`input/masters/sto_competencies.json`** (111 sub-themes × 3 levels) exists, but no call passes it, even though `:406` says its definitions override. Invalid values are **silently defaulted to Operational** (`validation.py:491-495`). |
| **references** | `ReferenceResources` is `code`: URL regex over course files (`meta_gen.py:845-852`) | Captures URLs only. Cited acts, rules, reports and books are not captured. The stakeholder note asks for "all the references in the PDF and audio transcripts". |
| **course mode** | `LearningMode` LLM-generated: Self-paced / Instructor-led / Blended (`:746-747`) | **Enum contradicts** Online / Offline / Blended. v3.5 output was 99.6% "Self-paced". The portal already has `learningMode` ("Self Paced") and `courseCategory` ("Course", "Moderated Course") in the API cache. "Moderated Course" is not in the field list's category list: **flagged**. |
| **Bloom's taxonomy tags** | No course-level field. Fragments: `TranscriptAnalysis.CognitiveMarkers` (level 1-6 per verb), `RubricScoring.BloomTaxonomy` (0-100 average over outcome verbs), `explain.learning_outcomes_validation.BloomVerbsUsed` | **Missing** as specified ("all tags with score and rationale"). The stakeholder draft has `BloomTaxonomy: [{Level, Confidence, Rationale}]`. |

### 2.2 Extracted today but not on the field list

`Tags`; `TranscriptAnalysis.KeywordsExtracted`; `TranscriptAnalysis.LearningTone`; `TranscriptAnalysis.CognitiveMarkers`; `RubricScoring` (seven sub-scores); `SuggestiveCompetencies.*`; `SubSectorTheme`; `explain.*` (sgos_reason, competency_mapping_triggers, mapping_reasons, learning_outcomes_validation, missing_info, …).

Two of these overlap group C:

- `Tags` regenerates something close to the portal `keywords`. The v3.5 review found Tags and KeywordsExtracted share terms in 1,915 of 2,186 cases.
- `explain.learning_outcomes_validation` validates outcomes that, in v3.6, come from the portal and are not shown to the model.

### 2.3 Group B — language (code)

`language.resolve_course_language` (`language.py:115-154`) takes a majority vote over per-video transcription source languages, falling back to the portal. It is already code, not LLM. **Gap:** the distinct-language list is computed but only the single winner is written to `Language` (`validation.py:228-238`). The field list asks for a list when one language isn't possible. For multi-language videos (`transcription_source_lang_multi.csv`), only the first language is used.

### 2.4 Group C — available context vs what P1 receives

| Group-C field | Fetched by the pipeline? | In the P1 prompt? |
|---|---|---|
| course_id | yes | no (not needed) |
| title (`name`) | yes | **no**; copied to output |
| description | yes | **no**; copied to output |
| learning outcome (`instructions`) | yes | **no**; copied to output |
| provider / organisation | yes | **no**; used only to drop provider tags |
| keywords | yes | **no** |
| courseCategory | yes (`meta_gen.py:487`) | **no**; stored, unused |
| course duration | yes | yes, as `duration_seconds`, used only for the ExtentOfLearning score |
| courseTypeCounts | no (module count is computed from folders and not passed) | no |
| creatorContacts | **no** | no |
| channel, parentCollections, publisher IDs, isExternal, source | **no** | no |
| (portal `learningMode`, not on the list) | yes | no |

**Missing but useful:** title, description, learning outcomes, keywords, courseCategory, provider/organisation, and module/resource counts (for summary, responsibilities, course mode and difficulty). creatorContacts is needed for tutor name.

### 2.5 Group C/D values generated by the LLM that should come from data

- **Course mode (`LearningMode`)** is LLM-generated, while the portal has `learningMode` and `courseCategory`.
- **Duration category (group D)** is effectively re-derived by the LLM through the ExtentOfLearning rubric bands (`prompts_v36.py:729-733`), which bucket duration.
- **Tags** largely re-create the portal `keywords`.
- **Title, description and duration** were LLM-generated in v3.5 but are correctly portal-sourced in v3.6.

---

## 3. Per-prompt findings (ranked by impact)

### 3.1 P1 — main metadata prompt

| Rank | Issue | Evidence | Impact |
|---|---|---|---|
| 1 | **The fields that place the course in vector space are thin or missing.** The summary is 80-100 words of generic prose. There is no responsibilities field, and no topics, skills or takeaways. | `:245-248`; sample10 summaries are 73-89 words and start "This course provides…" | Directly limits recommendation quality. This is the main goal. |
| 2 | **The model is asked to score things it cannot see.** Outcomes are portal-sourced and not passed, yet the rubric scores LearningObjectives ("+10 per measurable outcome") and BloomTaxonomy ("per outcome verb"). The STO evidence hierarchy ranks "learning objectives / outcomes" first and allows "course description" as evidence. `SuggestedLearningPath` needs "titles present in the inputs". | `:712-720`, `:396-405`, `:640-646`, `:319-320`; `field_config.json` | LO + BT carry 20% of the rubric that sets **difficulty** (`LearningLevel`), so those scores are ungrounded. STO evidence is weaker than it needs to be. |
| 3 | **Designation mapping is weak.** The lead instruction line was lost in v3.6. The shortlist embeds designation **names** against the **first 2k chars** of the transcript. The recommender embeds designations from roles-and-responsibilities context. There is no not-applicable output. | `:352-353` vs `prompts_v35.py:348-350`; `designations.py:170-174, 327-334` | Roles are picked from a noisy 150-name list with no count guidance, and no negative signal is produced. |
| 4 | **One monolithic call with both masters every time.** A ~58k-token prefix; STO is 54% of the rules; KCM and SGOS are sent whatever the area. The competency area is "decided first" only by instruction, inside the same generation as everything else. | `build_static_prefix`; sizes in §1.4 | Attention is spread across 18 fields; cost is high; the 1-in-9 cache hit rate means the prefix is mostly re-billed. **Decision: keep one call** and mitigate within it (§4.2: reasoning order, `propertyOrdering`, explicit caching). |
| 5 | **Silent defaults bias the distribution.** An invalid area becomes Domain; an invalid STO becomes Operational; the KCM branch is emptied without retry. | `validation.py:325-328, 491-495, 404-407` | Errors look like plausible values, so they are hard to detect in aggregate. |
| 6 | **Label sets contradict the agreed field list.** LearningMode enum; three-level SGOS; difficulty labels. (Group D was listed here; it is now confirmed valid.) | §2.1 | Outputs cannot be compared with existing values without remapping. |
| 7 | **Literal token sent to the model.** "Output role and designation prose in `__COURSE_LANGUAGE__`" is never substituted. | `:357`; `render_system_prompt` `:1189-1204` | Minor confusion; also shows there is no check for unsubstituted tokens. |
| 8 | **Inconsistent value/confidence/rationale.** PrimaryCompetencyArea, STOLevel, Targetroles and Suggestive* carry confidence; Sector, main competency arrays, groups, LearningMode and Tags do not. Confidence is a 0-1 float, but the field list asks for %. | schema `:817-1120` | Low-confidence results cannot be filtered or routed uniformly. |
| 9 | **The evidence-tier quirk discards real transcript evidence.** A course with a transcript under 2,000 chars **plus** a PDF is labelled `pdf_only`. The prompt then says "there is no transcript" and validation clears CognitiveMarkers. | `course_io.py:428-439`; `prompts_v36.py:238-239`; `validation.py:523-534` | A small population, but the effect is systematic. |
| 10 | **Head truncation drops later modules.** | `course_io.py:315-318`; `_truncate` | Long multi-module courses are summarised from their first modules only. |
| 11 | **Mixed-area courses have no path.** There is exactly one area, with no secondary. Domain-vs-Functional ties (e.g. sector-specific procurement) are forced. | `:259` | Theme/sub-theme or sector is simply absent for the losing area. |
| 12 | **Grounding is good, with gaps.** "Use ONLY these sources", no placeholders, and verbatim master values are all solid. But there is no instruction to return null for the summary or groups when evidence is thin; groups require "at least one". | `:204-205, :227-239, :339` | Minor. |

**Strengths to keep:**

- Master-membership rules plus code validation (KCM pair, SGOS path, designation id).
- Rubric arithmetic is done in code.
- Typed `explain` objects.
- The confidence cap for thin evidence.
- The STO framework itself: clear definitions, cautions and a rationale contract.

### 3.2 P2 — PDF chunk summary

| Rank | Issue | Evidence |
|---|---|---|
| 1 | It does not extract the references cited in documents (only URLs), the named authors/speakers (needed for the tutor fallback and author details), or the duties the document prepares the reader for. | `pdf_summarizer.py:46-59` |
| 2 | A failed chunk becomes `""` with only a log warning. The main prompt is not told a part is missing. | `:157-162` |
| 3 | Plain prose output: the later prompt cannot tell which sentences are references or stated objectives. | — |

### 3.3 E1 — designation shortlist

| Rank | Issue | Evidence |
|---|---|---|
| 1 | It embeds names, not roles and responsibilities. The production recommender's designation vectors (R&R context, `gemini-embedding-2`) are not reused (`designations.py:20-32`), so P1's candidates come from a different space than the one recommendations use. | `designations.py:20-32, 170-174` |
| 2 | The query is the opening of the course, not a description of it. | `designations.py:333` |

---

## 4. Recommended pipeline structure: one call

**Decision (Nov 2026): keep a single LLM call per course.** v3.7 (`prompts_v37.py`) keeps v3.6's one-call shape. A chained design (four calls: content profile → competencies → levels → designations) was drafted and then dropped in favour of one call.

### 4.1 Flow

```
 code (no LLM)   content extraction · evidence tier · per-module transcript truncation
                 PDF chunk summaries (P2) · designation shortlist (E1)
                          │
                          ▼
 ONE Gemini call static prefix  = v3.7 rules + KCM + SGOS [+ STO level labels]
                                  identical for every course → cacheable
                 course section = tier · language · duration · shortlist ·
                                  transcript · PDF summaries
                 response schema = FULL_METADATA_SCHEMA pruned by field_config.json
                          │
                          ▼
 code            validation (master membership, enums) · rubric total → LearningLevel
                 DomainCompetencies mirror · URL ReferenceResources · Language · provenance
```

### 4.2 Why one call, and how v3.7 works within it

- **What one call keeps:**
  - `meta_gen.py` orchestration, retries, validation and run bookkeeping are unchanged.
  - Switching to v3.7 means changing one import in `meta_gen.py` and one in `validation.py`.
  - One request per course and one cacheable prefix.
- **What one call costs:**
  - Every course carries both masters and all ~23 fields, so the prefix is about 60k tokens (239,643 chars; 293,907 with the STO labels).
  - The competency area is still "decided first" only by instruction, inside the same generation as everything else.
- **How v3.7 handles that inside one call:**
  - The TASK block sets the reasoning order: area first, then CourseSummary and TargetResponsibilitiesFulfilled, then Targetroles judged against those responsibilities.
  - Every new field has a closed shape: enums for groups, mode and Bloom level, and verbatim evidence quotes for responsibilities and Bloom levels.
  - The STO labels per sub-theme are optional, so the size cost can be measured before adopting them (§8, arm A2).
- **Recommended next steps on the same single call. None of these is in the code yet:**
  - **Schema `propertyOrdering`.** Set it so the decision fields (PrimaryCompetencyArea, CourseSummary, TargetResponsibilitiesFulfilled) are generated before Targetroles, STOLevel and RubricScoring. Then the instruction order is also the generation order. Check Gemini's default ordering first.
  - **Explicit context caching** (Vertex cached content) for the static prefix in batch runs. Implicit cache hits were 1 in 9 (§1.4).
- **Code, not LLM. Still recommended, outside the prompt:**
  - Tutor name from portal `creatorContacts` / `author` first, with `AuthorName` from the LLM as a fallback. `api_fetcher.py:340-354` does not save these fields yet.
  - Course mode from `courseCategory` where the mapping is agreed (Q4).
  - Output the list of languages, not only the majority language (`language.py:115-154`).
  - **Designation shortlist (E1):** compare the course against designation roles-and-responsibilities text, not names, using more than the first 2k chars of the transcript (`designations.py:327-334`). In a single call the shortlist is built *before* generation, so it cannot use this run's CourseSummary. Use the transcript + PDF summaries, or the previous run's summary when one exists.
- **Content-only input stays.** As in v3.6, the model sees no portal title, description or outcomes, which avoids echoing them back. Whether adding them as labelled context helps is evaluation arm A3 (§8), not a default.
- **Parsing robustness:**
  - Keep the response schema, which now has enums for groups and mode.
  - Add enum checks in validation.
  - **Replace silent defaults with one targeted re-ask:** re-run the call with the validation error appended. If it still fails, write `null` + `needs_review`, never Domain or Operational (`validation.py:325-328, 491-495`).
  - Keep JSONDecodeError as transient.
  - v3.7's `render_system_prompt` already raises if a `__TOKEN__` is left unsubstituted.
- **Other code fixes:**
  - Tier: label a short transcript plus a PDF as `thin_transcript+pdf`, not `pdf_only` (`course_io.py:427-439`).
  - Surface failed PDF chunks (`pdf_summarizer.py:157-162`) in `explain.missing_info`.

---

## 5. JSON output schema (v3.7, single call)

The response schema is `FULL_METADATA_SCHEMA` in `prompts_v37.py`, pruned by `field_config.json` exactly as in v3.6. The model now generates 23 top-level fields, against v3.6's 18. v3.6 fields keep their shapes, and confidence stays a 0–1 number. The new and changed fields look like this:

```jsonc
{
  "CourseSummary": "140-200 words, one paragraph, written for embedding",
  "TargetResponsibilitiesFulfilled": [
    {"Responsibility": "Recognise common online financial fraud patterns and report suspected cases through the national cybercrime helpline.",
     "EvidenceQuote": "verbatim ≤25 words", "Confidence": 0.85}
  ],
  "AuthorName": "string | null  (only as stated in the content)",
  "TargetEmployeeGroups": ["Group B", "Group C"],          // enum: Group A/B/C/D, 1-4 items
  "Targetroles": [
    {"DesignationId": "DESG-020905", "Name": "Inspector (Cyber Crime)", "Confidence": 0.9, "Rationale": "string"}
  ],
  "Targetrolesexclusions": [
    {"DesignationId": "DESG-…", "Name": "…", "Confidence": 0.75, "Rationale": "shares vocabulary but …"}
  ],
  "BloomTaxonomy": [
    {"Level": "Understand", "Confidence": 0.9, "Rationale": "string", "EvidencePhrase": "verbatim ≤25 words"}
  ],
  "ReferencesCited": [
    {"Citation": "Information Technology Act, 2000", "Type": "Act or rule", "URL": null, "Source": "transcript"}
  ],
  "LearningMode": "Self-paced | Instructor-led | Blended   (LEARNING_MODES, Q4)",
  "STOLevel": {"Level": "Operational", "Confidence": 0.85, "Rationale": "…",
               "PositiveSignalsMatched": [], "CounterSignals": [], "EvidencePhrases": []}
  // unchanged from v3.6: PrimaryCompetencyArea, Functional/BehaviouralCompetencies,
  // SuggestiveCompetencies, Sector/SubSector/SubSectorTheme, Tags, PriorKnowledgeDeclared,
  // SuggestivePriorKnowledge, RubricScoring, TranscriptAnalysis, explain
}
```

**How this matches the stakeholder draft** (`runs/single/courses/do_11381269163759206411.json`):

| Draft item | v3.7 |
|---|---|
| `AuthorName` | Same name. Taken from the content only; the portal path is not built yet. |
| `BloomTaxonomy` list (Level, Confidence, Rationale) | Same shape, plus `EvidencePhrase`. No numeric score until Q2 is answered. |
| `Targetrolesexclusions` | Same name and shape as `Targetroles`. |
| `ReferenceResources` "list of all references in PDF and transcripts" | New `ReferencesCited`. `ReferenceResources` stays the code-extracted URL lists, so validation is unchanged. |
| Merged `Prerequisites` with `Recommended_by` | **Not adopted.** v3.7 keeps v3.6's `PriorKnowledgeDeclared` (instructor) + `SuggestivePriorKnowledge` (AI), which carry the same distinction and which validation already handles. |
| Multiple sub-sectors | **Open (Q6).** Still one SGOS path. |
| `GeneratorModel`, `GeneratorVersion` | Provenance fields set in code, not by the model. |

---

## 6. Draft improved prompts (DRAFT — not applied)

### 6.0 `prompts_v37.py`: v3.6 template, single call

This is a copy of `prompts_v36.py` with targeted edits. It is not imported by any pipeline module.
- **Unchanged:** section numbering (§1–§16), the STO framework text, rubric weights, 0–1 confidences, `field_config.json` handling, and every public name and signature `meta_gen.py` / `validation.py` use.
- **Checked offline:**
  - It exposes every public name v3.6 does.
  - All schemas use only the Gemini subset.
  - No tokens are left unsubstituted.
  - `validation` and `meta_gen` import with v3.7 in place of v3.6.
  - **No model call was made with it.**
- **Text fixes:**
  - §9: the Targetroles lead instruction is restored, and `__COURSE_LANGUAGE__` now reads output_language. `render_system_prompt` raises if any token is left unsubstituted.
  - §6: SuggestedLearningPath asks for topics, not titles.
  - §11: LO and BT score objectives stated in the content; BT falls back to BloomTaxonomy.
  - §10.2: Domain courses anchor STO on the SGOS SubSector / SubSectorTheme.
  - §2: one-line area definitions tied to the masters.
- **New fields:** `TargetResponsibilitiesFulfilled`, `AuthorName`, `BloomTaxonomy`, `Targetrolesexclusions`, `ReferencesCited`. `CourseSummary` is 140–200 words, written for embedding.
- **Enums:** `TargetEmployeeGroups` (Group A/B/C/D) and `LearningMode` are enforced by the schema. `LearningMode` defaults to v3.6's labels; set the `LEARNING_MODES` environment variable to override (Q4).
- **Optional STO master:** `build_static_prefix(kcm, sgos, sto_json)` appends the per-sub-theme STO level labels (111 entries).
- **Transcript truncation:** each module gets a fair share of the budget, instead of the head being kept and later modules dropped.
- **Size:** the cached prefix is 239,643 chars, against 232,858 for v3.6. It is 293,907 with the STO master.

### 6.1 The two instructions that matter most for embeddings (from `prompts_v37.py` §1)

**CourseSummary**

> This text is embedded and matched against the roles and responsibilities of government designations to recommend the course. Write it for a reader deciding which job roles the course serves: describe what is taught and what the learner can do afterwards, not how good the course is.
> - 6-8 sentences, 140-200 words, single plain-text paragraph. Do not start with "This course". [Then: context, topics in teaching order, what the learner can do in job terms, where it is used.]
> - No marketing words … No talk about the course itself … Keep to the depth actually taught.

**TargetResponsibilitiesFulfilled**

> A responsibility is a duty a government employee performs on the job that this course equips them to do better.
> - 4-10 items, most central first. Each is written like a line in a job description: verb + object + context or purpose, 8-20 words.
> - Derive each from what the content actually teaches. Keep to the scope taught …
> - Never name designations, cadres, services or Group labels.
> - Each item needs Confidence (0-1) and an EvidenceQuote …

**Design choices behind these two:**

- **One paragraph, fixed sentence order.** `CourseSummary` stays a string, so existing consumers keep working. The sentence plan makes every course's text cover the same ground.
- **Duty-statement style.** It mirrors job descriptions, so course duties sit close to designation duties in embedding space. The exact style should copy the designation R&R template (Q8).
- **Provider and author names stay out of the summary.** They move the vector without saying which roles a course serves. The author goes in `AuthorName`.
- **Course embedding text (recommendation).** Embed `CourseSummary` + the `Responsibility` strings with Confidence ≥ 0.6, in one fixed template. This would be a small code change in the embedding step, not in the prompt.

---

## 7. Open questions / definitions needed

Items still open for `prompts_v37.py`. Where v3.7 had to pick a default, it is a named constant or environment variable that can be changed without editing the prompt.

| # | Question | Why it blocks | Where it shows up |
|---|---|---|---|
| Q1 | **Group A / B / C / D criteria.** Group D is confirmed valid (Nov 2026). The repo only has "A/B = gazetted senior officers; C/D = supporting staff" (`prompts_v36.py:341-348`). Nothing separates A from B, or C from D. | Groups cannot be validated; the designation master has no group column (`README.md:360, 889`). | `EMPLOYEE_GROUPS`, prompt §9 |
| Q2 | **Bloom tag score scale.** The field list says "score", but the repo has only a 1-6 marker level and a 0-100 rubric over outcome verbs. v3.7's `BloomTaxonomy` therefore has Level + Confidence + Rationale and **no score**. | A score field cannot be added until the scale is defined. | `BloomTaxonomy` schema, prompt §7 |
| Q3 | **Difficulty label set**: basic/intermediate/advanced (field list) vs Beginner/Intermediate/Advanced/**Expert** (portal). How do the dimension scores combine into a label? v3.6 uses a 7-part weighted rubric whose ERO/TAA parts depend on designations. | Cannot compare with the existing values or pick the rule. | `classify`, `BEGINNER_MAX` / `INTERMEDIATE_MAX`, `LearningLevel` enum |
| Q4 | **Course mode labels and mapping**: Online/Offline/Blended vs v3.6 Self-paced/Instructor-led/Blended vs portal `learningMode` "Self Paced". What do courseCategory "Course" and "**Moderated Course**" (not on the field-list category list) map to? | Only "blended program → Blended" is stated. | `LEARNING_MODES` (default: v3.6 labels) |
| Q5 | **STO definitions source.** Is `input/masters/sto_competencies.json` (or `competencies_level.json`, same content with ids) the authoritative per-sub-theme STO definition? Only 98 of 114 KCM pairs match it, even after normalising "&"/"and" and -ise/-ize. One theme ("Establishment and HR") appears as Behavioural in the level master but Functional in KCM. How should Domain courses (no KCM sub-theme) be anchored? | Decides whether the STO labels are sent, and how mismatched names are handled. v3.7 §10.2 anchors Domain courses on SubSector / SubSectorTheme. | `compact_sto_master`, `build_static_prefix(sto_json=…)` |
| Q6 | **SGOS depth and multiplicity.** Two levels (field list, portal) or three (v3.6)? May a course carry more than one sub-sector? (Stakeholder note on the draft JSON.) | Changes the schema and comparisons. | `Sector` / `SubSector` / `SubSectorTheme` (one path), prompt §4 |
| Q7 | **KCM structure for themes vs portal values.** Portal theme/sub-theme strings for Functional/Behavioural courses need checking against `kcm_competencies.json` spellings before agreement can be measured. | Baseline comparison. | §8 |
| Q8 | **Designation embedding text.** What template, model and language does the recommender use to embed designation roles-and-responsibilities, and can the shortlist (and the prompt's `designation_candidates`) carry that R&R text? The batch job currently has only `id,name` (`input/masters/igot_designations.csv`). | CourseSummary / responsibilities style should mirror it; Targetroles quality depends on it. | `designations.py`, prompt §1 and §9 |
| Q9 | **Language of embedded fields.** v3.7 writes prose in the course language (as v3.6). Should embedded fields be English for all courses? | Embedding consistency vs learner-facing text. | `output_language` in `build_course_section` |
| Q10 | **creatorContacts / author shape** in the Learning API, and whether `author` is a person or an organisation. | Needed before a portal-first tutor name can be built; v3.7 only reads it from content. | `AuthorName` |
| Q11 | **Competency-area tie-break policy** for sector-specific courses that teach a generic function (e.g. procurement in railways): Domain or Functional? v3.7 picks the area with the closer master match and lowers confidence. | Area agreement depends on it. | prompt §2 |
| Q12 | **Where portal "existing values" for sector/sub-sector live.** `course_metadata.xlsx` has competency area / themes / sub-themes, with Domain themes being SGOS sectors. Is there a separate sector field? | Baseline source. | §8 |

**Contradictions with the field list, flagged and not resolved:**

- ~~Group D present (Q1)~~ resolved: Group D is valid
- difficulty labels (Q3)
- course-mode labels and the "Moderated Course" category (Q4)
- three-level SGOS (Q6)
- author details are part of the summary in the field list, but v3.6 forbids provider names in generated text; v3.7 puts the author in `AuthorName` and keeps it out of `CourseSummary`

---

## 8. Suggested evaluation plan

**Sample.** 60 courses, stratified by:

- existing competency area: Domain 62% / Functional 26% / Behavioural 10% in the portal; sample 24 / 20 / 16 to give Behavioural enough cases
- evidence tier: transcript / thin / pdf_only
- courseCategory: include Moderated Course and any Blended Program
- language: at least 6 non-English
- length: at least 6 multi-module courses over 3 h

Reuse `runs/sample10` as part of it. An SME labels 30 of the 60 as gold.

**Arms:**

| Arm | Prompt | Purpose |
|---|---|---|
| A0 | v3.6 as is | baseline |
| A1 | v3.7 as drafted (content only, no STO master) | main candidate |
| A2 | v3.7 + STO level labels (`build_static_prefix(…, sto_json)`) | do the per-sub-theme labels improve STO accuracy enough to justify +54k chars? |
| A3 | v3.7 + labelled portal context (title, description, outcomes); needs a small change to `build_course_section` | does context help, or cause echoing? |

**Per-field checks:**

| Field | Automatic check | SME check (gold 30) |
|---|---|---|
| summary | word count; banned-word rate ("comprehensive", "This course"); topic count; n-gram overlap with portal description (echo rate) | 1-5 for specificity and faithfulness |
| target_responsibilities_fulfilled | count; % with EvidenceQuote found verbatim in input; duplicate rate | precision (is each a real duty the course supports?), scope correctness |
| competency area | agreement with portal value (confusion matrix) | adjudicate every disagreement: who is right? |
| sector / sub-sector | path valid; agreement with portal sector (Domain) | adjudicate disagreements |
| theme / sub-theme | master-valid rate; Jaccard with portal pairs | precision of the top-ranked pair |
| difficulty | agreement with portal label; distribution shift | adjudicate disagreements |
| STO | distribution; stability across 3 runs | accuracy on gold |
| employee groups | enum-valid rate | accuracy on gold |
| designations | % from master; Targetrolesexclusions count; overlap between the two lists (must be 0) | precision@5 of Targetroles; correctness of exclusions |
| AuthorName, LearningMode | coverage; % AuthorName found verbatim in input | spot-check |
| ReferencesCited, BloomTaxonomy, prerequisites | % citations found in input; URL validity; % BloomTaxonomy EvidencePhrase found in input | spot-check |
| all fields | schema-valid rate, retry rate, cost and latency per course | — |

**Using existing values as the baseline.** Report agreement per field (A1 vs portal) and accuracy on SME-adjudicated disagreements, separately for "existing was right" and "new is right". That shows whether the new prompts beat the platform's own tagging, not only whether they match it. Adopt A3's portal context only if it raises adjudicated accuracy without raising the echo rate.

**Embedding-level test (the main goal).**

1. Build course vectors from A0 (`CourseSummary` only) and A1 (`CourseSummary` + `TargetResponsibilitiesFulfilled`) using the recommender's model.
2. Pick 20 designations with R&R text. For each, have the SME mark which of the 60 courses are relevant (or use completion data by designation holders, if group-D tables carry designation).
3. Compare precision@10 and nDCG@10.
4. Report the mean cosine gap between each course's `Targetroles` and `Targetrolesexclusions`. A wider gap means better separation.

**Stability.** Run A0 and A1 three times on the gold 30, and report per-field agreement across runs for: area, STO, difficulty, groups, top theme pair, and responsibility count.

**Decision rule (proposal).** Adopt v3.7 when all of these hold:

- embedding precision@10 improves;
- adjudicated accuracy for area, sector and theme is no worse than A0;
- responsibilities precision is ≥ 80%;
- schema-valid rate is ≥ 98% after retries;
- cost per course is ≤ 110% of A0 (v3.7's prefix is ~3% larger and its output is longer).
