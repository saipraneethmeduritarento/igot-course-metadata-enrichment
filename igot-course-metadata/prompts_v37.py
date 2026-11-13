"""
Prompt + output-schema definitions for the iGOT course metadata regeneration
engine, v3.7. Same template, single call and public API as prompts_v36.py (a
copy of it with the changes below), so it can replace v3.6 by changing the
`import prompts_v36 as P` lines in meta_gen.py and validation.py. NOT WIRED IN
YET: both still import prompts_v36.

v3.7 CHANGES (from the prompt audit, docs/prompt_audit.md at the repo root)
---------------------------------------------------------------------------
Fixes to v3.6 text:
- Section 9: the Targetroles instruction lost in v3.6 is restored, and the
  literal "__COURSE_LANGUAGE__" (never substituted) now reads output_language.
  render_system_prompt() raises if any __TOKEN__ is left unsubstituted.
- Section 6: SuggestedLearningPath asked for "titles present in the inputs",
  but no titles are given in v3.6. It now asks for prerequisite topics.
- Section 11: the LO and BT rubric scores now read objectives stated in the
  CONTENT. LearningOutcomes is portal-sourced (field_config.json) and is not
  passed, so v3.6 scored outcomes the model could not see.
- Section 10.2: says which Theme/SubTheme anchors STO for a Domain course (the
  SGOS SubSector/SubSectorTheme), which v3.6 left unstated.
New / tightened fields (from the agreed field list and the stakeholder draft
record in runs/single/courses/do_11381269163759206411.json):
- CourseSummary is longer and written for embedding-based matching.
- TargetResponsibilitiesFulfilled: job duties the course equips learners for.
- AuthorName: tutor/speaker name only as stated in the content.
- BloomTaxonomy: the Bloom levels the content exercises, each with confidence
  and rationale.
- Targetrolesexclusions: shortlisted designations judged NOT applicable.
- ReferencesCited: acts, circulars, reports, books and portals the content
  cites (ReferenceResources stays the code-extracted URL lists).
- TargetEmployeeGroups and LearningMode are enum-checked by the schema.
  Groups: A / B / C / D (Group D kept, confirmed Nov 2026).
- Optional STO master: build_static_prefix(kcm, sgos, sto_json) adds the
  per-sub-theme STO level labels from sto_competencies.json. meta_gen.py does
  not pass it yet.
Unchanged on purpose: section layout and numbering, the STO framework text,
rubric weights and bands, 0-1 confidences, field_config.json handling, and
build_static_prefix / build_course_section / build_prompt signatures (the new
sto_json argument is optional).

v3.6 CHANGES (from the Nov 2026 field review)
---------------------------------------------
- The model no longer sees the course's platform metadata (title, description,
  keywords, instructions, organisation, declared competencies). Input is the
  transcript, PDF chunk summaries, the masters and the output schema only, so
  the model cannot echo the existing text back.
- Title, description and duration are copied from the portal, Language comes
  from the transcription source language, and code-computed fields are filled
  in Python. Which fields the model generates is driven by field_config.json;
  METADATA_SCHEMA below is the full schema pruned to the model's fields.
- The "##check##" review notes were removed from the prompt text (they were
  being sent to the model). They are preserved in prompts_v35.py.

Separated out of meta_gen.py so the contract can be versioned and reviewed on
its own. v3.4 is superseded; where v3.4-advanced-extended behaved *better* than
the written spec, that behaviour is preserved deliberately and the reason is
noted inline (see NOTES below).

NOTES ON DELIBERATE DIVERGENCE FROM THE WRITTEN v3.5 SPEC
---------------------------------------------------------
1. Learning Outcomes are NOT prefixed with literal "Why:" / "How:" / "What:"
   labels. Spec section 8 asks for the labels; the v3.4 implementation
   overrode this and produced clean learner-facing outcomes instead. Labelled
   strings would be a visible regression on the platform, so outcomes stay
   unlabelled and the why/how/what coverage is asserted in
   `explain.learning_outcomes_validation` instead. Flip LO_STYLE to change.

2. Rubric weights follow the v3.5 spec table (which sums to 100), not the v3.4
   code's weights (which zeroed TargetAudienceAlignment). Weights and bands are
   constants here and TotalScore is recomputed in Python, never trusted from
   the model.

3. TranscriptAnalysis keeps the richer per-item structure the v3.4 *prompt*
   asked for. The v3.4 *schema* flattened these to arrays of strings, so the
   requested evidence was silently discarded on every call.

4. `explain` sub-objects are fully typed. In v3.4 they were declared as bare
   {"type": "object"} with no properties, which made Gemini emit `{}` -- all
   368 rows of the previous run have an empty audit trail as a result.

5. Sector / SubSector / SubSectorTheme are nullable and NOT required. In v3.4
   they were required while the prompt forbade populating them for
   Functional/Behavioural courses, so 58% of rows carry the string
   "Not Applicable" in a field typed as a sector name.

6. The competency master (kcm_competencies.json) reaches the model in reduced
   form: one entry per Theme/SubTheme pair with its two definitions, and nothing
   else. See compact_competency_master() for what is dropped and why.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

PROMPT_VERSION = os.environ.get("PROMPT_VERSION", "v3.7")
SCHEMA_VERSION = PROMPT_VERSION
GENERATOR_NAME = os.environ.get(
    "GENERATOR_NAME", "iGOT Metadata Regeneration Engine (Vertex AI / Gemini)"
)

# "Behavioural" (British) matches the KCM master data's `type` values; the v3.5
# spec document writes "Behavioral". The KCM spelling wins by default so output
# keys and master-data values agree. Set BEHAVIOURAL_SPELLING=Behavioral if a
# downstream consumer needs the spec form.
BEHAVIOURAL_LABEL = os.environ.get("BEHAVIOURAL_SPELLING", "Behavioural")
BEHAVIOURAL_KEY = f"{BEHAVIOURAL_LABEL}Competencies"

# "unlabelled" keeps the v3.4 behaviour (clean learner-facing outcomes);
# "why_how_what" follows the literal v3.5 spec section 8. See NOTES above.
LO_STYLE = os.environ.get("LEARNING_OUTCOME_STYLE", "unlabelled")

# Cadre bands for TargetEmployeeGroups. Group D is kept (confirmed Nov 2026).
EMPLOYEE_GROUPS: List[str] = ["Group A", "Group B", "Group C", "Group D"]

# LearningMode labels. Default is the v3.6 set. The agreed field list says
# Online / Offline / Blended and the portal stores "Self Paced"; which set is
# final is still open (docs/prompt_audit.md, section 7). Override with
# LEARNING_MODES="Online,Offline,Blended".
LEARNING_MODES: List[str] = [
    m.strip()
    for m in os.environ.get(
        "LEARNING_MODES", "Self-paced,Instructor-led,Blended"
    ).split(",")
    if m.strip()
]

BLOOM_LEVELS: List[str] = [
    "Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create",
]

# ---------------------------------------------------------------- rubric config
# v3.5 spec section 10. Weights are percentages and sum to 100. Overridable as
# RUBRIC_WEIGHTS="LO:10,PK:5,BT:10,CC:15,ERO:20,EOL:25,TAA:15".
_DEFAULT_WEIGHTS: Dict[str, int] = {
    "LearningObjectives": 10,
    "PriorKnowledge": 5,
    "BloomTaxonomy": 10,
    "ComplexityOfContent": 15,
    "ExpectedRoleOutcome": 20,
    "ExtentOfLearning": 25,
    "TargetAudienceAlignment": 15,
}
_WEIGHT_ALIASES = {
    "LO": "LearningObjectives", "PK": "PriorKnowledge", "BT": "BloomTaxonomy",
    "CC": "ComplexityOfContent", "ERO": "ExpectedRoleOutcome",
    "EOL": "ExtentOfLearning", "TAA": "TargetAudienceAlignment",
}


def _load_weights() -> Dict[str, int]:
    raw = os.environ.get("RUBRIC_WEIGHTS", "").strip()
    if not raw:
        return dict(_DEFAULT_WEIGHTS)
    weights = dict(_DEFAULT_WEIGHTS)
    for part in raw.split(","):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = _WEIGHT_ALIASES.get(key.strip().upper(), key.strip())
        if key in weights:
            weights[key] = int(value)
    total = sum(weights.values())
    if total != 100:
        raise ValueError(f"RUBRIC_WEIGHTS must sum to 100, got {total}: {weights}")
    return weights


RUBRIC_WEIGHTS: Dict[str, int] = _load_weights()

# Spec lists "<=45 Beginner / 46-75 Intermediate / 75 Advanced"; the 75 overlap
# is resolved in favour of Intermediate.
BEGINNER_MAX = int(os.environ.get("RUBRIC_BEGINNER_MAX", "45"))
INTERMEDIATE_MAX = int(os.environ.get("RUBRIC_INTERMEDIATE_MAX", "75"))

RUBRIC_KEYS: List[str] = list(RUBRIC_WEIGHTS)


def compute_total_score(rubric: Dict[str, Any]) -> int:
    """Weighted total, computed in Python. Models are unreliable at arithmetic."""
    total = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        try:
            value = float(rubric.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        total += max(0.0, min(100.0, value)) * (weight / 100.0)
    return int(round(total))


def classify(total_score: int) -> str:
    if total_score <= BEGINNER_MAX:
        return "Beginner"
    if total_score <= INTERMEDIATE_MAX:
        return "Intermediate"
    return "Advanced"


def _rubric_formula() -> str:
    parts = [f"({k}*{w/100:.2f})" for k, w in RUBRIC_WEIGHTS.items()]
    return "TotalScore = " + " + ".join(parts)


def _rubric_table() -> str:
    rows = [f"    | {k:<24} | {w:>6} |" for k, w in RUBRIC_WEIGHTS.items()]
    head = f"    | {'Parameter':<24} | {'Weight':>6} |\n    |{'-'*26}|{'-'*8}|"
    return head + "\n" + "\n".join(rows)


# ------------------------------------------------------------ competency master
def compact_competency_master(master: Any) -> List[Dict[str, Any]]:
    """
    The competency master as the model sees it: one entry per Theme/SubTheme
    pair, carrying only the two definitions the mapping decision is made from.

    Reduction matters: the raw file is ~815 KB, most of it text the model is
    never asked to reason about, and it sits in the static prefix of every
    single call.
    """
    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    for row in master or []:
        if not isinstance(row, dict):
            continue
        ctype = str(row.get("type") or "").strip()
        theme = str(row.get("theme") or "").strip()
        if not ctype or not theme:
            continue
        sub = str(row.get("sub_theme") or "").strip()
        key = (ctype, theme.lower(), sub.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "type": ctype,
                "theme": theme,
                "theme_description": str(row.get("theme_description") or "").strip(),
                "sub_theme": sub,
                "sub_theme_description": str(
                    row.get("sub_theme_description") or ""
                ).strip(),
            }
        )
    return out


def compact_sto_master(sto_json: Any) -> List[Dict[str, Any]]:
    """
    sto_competencies.json as the model sees it: one entry per Theme/SubTheme
    with the short Proficiency Level Label for each STO level. The long
    Proficiency Level Descriptions are dropped to keep the cached prefix small.

    Names are kept as spelled in the STO master. About 16 of the 114 KCM pairs
    have no STO entry or are spelled differently ('&' vs 'and', -ise vs -ize);
    for those the section 10 framework applies on its own.
    """
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in sto_json or []:
        if not isinstance(row, dict):
            continue
        theme = " ".join(str(row.get("Competency Name") or "").split())
        sub = " ".join(str(row.get("Subtheme") or "").split())
        level = " ".join(str(row.get("Proficiency Level") or "").split())
        label = " ".join(str(row.get("Proficiency Level Label") or "").split())
        if not theme or not sub or not level:
            continue
        entry = grouped.setdefault(
            (theme, sub),
            {
                "type": " ".join(str(row.get("Competency type") or "").split()),
                "theme": theme,
                "sub_theme": sub,
                "levels": {},
            },
        )
        entry["levels"][level] = label
    return list(grouped.values())


# --------------------------------------------------------------- system prompt
# Tokens (__NAME__) are substituted in build_prompt so the JSON examples below
# can keep their literal braces without f-string escaping.
SYSTEM_PROMPT_TEMPLATE = """
You are an *auditable metadata regeneration engine* for the iGOT Karmayogi Bharat platform.
Your objective is to process the provided inputs and return a single, valid JSON object representing a
complete, standardized metadata record for a learning course.

## Inputs
You must use ONLY these sources. Do not use outside knowledge to add facts about the course.
You are deliberately NOT given the course's existing title, description, keywords or other platform
metadata: every generated field must be derived from the course CONTENT below.
- transcript_text: (String, optional) transcript assembled from the course's video subtitle files. It is
  usually an English translation of the original audio.
- pdf_summaries: (String, optional) summaries of the course's PDF documents, produced chunk by chunk
  from the full document text.
- output_language: (String) the language the course is delivered in. Write generated prose in it.
- duration_seconds: (Number or null) the platform-recorded course duration. Use it only for the
  ExtentOfLearning rubric score.
- kcm_json: (JSON) the master Karmayogi Competency Model - the ONLY valid source of Functional and
  Behavioural competencies.
- sgos_json: (JSON) the master Sector-SubSector-Theme mapping - the ONLY valid source of Domain
  classification.
- sto_json: (JSON, optional) for each KCM Theme/SubTheme, a one-line label of what the capability looks
  like at the Operational, Tactical and Strategic level. Used only for STOLevel (section 10).
- designation_candidates: (JSON, optional) a pre-filtered shortlist from the official iGOT designation
  master. You may ONLY return designations present in this shortlist.
- evidence_tier: (String) how much real content you were given: "transcript" (substantial transcript),
  "thin_transcript" (short transcript), or "pdf_only" (documents only). Calibrate confidence to this.

## Global rules
- JSON output only. No prose, no markdown fences, no trailing commas.
- Produce ONLY the fields present in the response schema. Some rules below describe fields that are
  filled outside this call; if a field is not in the response schema, ignore its rule.
- Never invent Themes, SubThemes, Sectors, or Designations. Every such value must appear verbatim in the
  corresponding master input. If nothing qualifies, return an empty array - never a placeholder string.
- Language: write ALL generated prose (summary, outcomes, tags, rationales) in output_language. The
  transcript is often an English translation of non-English audio, so the transcript's language does
  NOT determine the output language.
- Never emit the strings "Not Applicable", "N/A", "None", or "Unknown" as a value for a sector,
  competency, designation, or role field. Use null for absent single values and [] for absent lists.
- Do not name the course provider or organisation in generated text.
- Auditability: populate `explain` fully. Every mapping you make must be traceable to a phrase in the
  transcript or PDF summaries. An empty `explain` object is a failed response.
- Calibrate all confidence values honestly to evidence_tier. With "thin_transcript" or "pdf_only",
  confidence for content-derived judgements must not exceed 0.80. With "pdf_only" there is no
  transcript: TranscriptAnalysis must be returned with empty values and 0.0 confidences.

## Field generation rules

### 1. Core text fields

    **CourseSummary:**
    - This text is embedded and matched against the roles and responsibilities of government
      designations to recommend the course. Write it for a reader deciding which job roles the course
      serves: describe what is taught and what the learner can do afterwards, not how good the course is.
    - 6-8 sentences, 140-200 words, single plain-text paragraph. Do not start with "This course".
        - Sentence 1: the subject and its specific public-service context (scheme, act, system, process
          or sector) as taught.
        - Next 2-3 sentences: the specific concepts, procedures, laws, tools or systems covered, in the
          order taught.
        - Next 2-3 sentences: what the learner learns to do, in job terms (verb + object).
        - Last sentence: the work situations where this is used.
    - Use the concrete domain terms from the content. Expand each acronym once, e.g. "National
      Cybercrime Reporting Portal (NCRP)".
    - No marketing words ("comprehensive", "in-depth", "essential", "empowering", "holistic"). No talk
      about the course itself ("this module", "the video", "the presenter explains"). No provider or
      organisation names. Formal, professional tone.
    - Keep to the depth actually taught: an awareness course teaches learners to recognise and report,
      not to investigate or design.

    **TargetResponsibilitiesFulfilled:**
    - Embedded next to CourseSummary and matched to designation duties. A responsibility is a duty a
      government employee performs on the job that this course equips them to do better.
    - 4-10 items, most central first. Each is written like a line in a job description: verb + object +
      context or purpose, 8-20 words. Example (cyber-fraud course): "Recognise common online financial
      fraud patterns and report suspected cases through the national cybercrime helpline."
    - Derive each from what the content actually teaches. Keep to the scope taught (a portal walkthrough
      supports "process applications on portal X", not "design the application process").
    - No two items may describe the same duty. Never name designations, cadres, services or Group labels.
    - Each item needs Confidence (0-1) and an EvidenceQuote: a verbatim phrase (at most 25 words) from
      the transcript or PDF summaries showing the content teaches it.
    - [] only if the content is too thin to support any duty; say so in `explain.missing_info`.

    **AuthorName:**
    - The name(s) of the tutor, speaker or author exactly as stated in the content (e.g. a speaker
      introducing themselves, or an author line in a document). Several names are joined with ", ".
    - null if the content does not state a name. Never guess a name from general knowledge, and never
      use an organisation name here.

    **Tags:**
    - 10-15 descriptive keyword strings derived from the course content.
    - No organisation names, provider names, or course codes.
    - If a transcript is present, draw from its most significant terms.
    - Add relevant cross-domain conceptual tags where genuinely applicable.

### 2. Primary competency area (decide this FIRST)
    - Determine the single primary competency area from CONTENT ANALYSIS ONLY: one of
      "Domain", "Functional", "__BEHAVIOURAL_LABEL__".
    - Decide from the capability the learner gains, not from the subject alone:
        - "Domain": the main capability is specific to a sector's mandate, schemes, laws or systems,
          i.e. it is best described by a path in `sgos_json`.
        - "Functional" / "__BEHAVIOURAL_LABEL__": the main capability is best described by a `kcm_json`
          entry of that `type`.
      If two areas fit, choose the one whose master has the closer match and lower the confidence.
    - Exactly ONE primary area per course. A course must never be assigned multiple primary areas.
    - Record the deciding evidence in `PrimaryCompetencyArea.reason` and a 0-1
      `PrimaryCompetencyArea.confidence`.

### 3. Functional and Behavioural competencies
    - Populate ONLY if the primary area is "Functional" or "__BEHAVIOURAL_LABEL__".
    - Read each theme_description and sub_theme_description; match on meaning, not keyword overlap.
    - Map every pair that is genuinely relevant - typically 2 to 6. Do not pad to reach a count, and do
      not stop early if more genuinely apply.

    KCM-ONLY, and three specific ways this goes wrong. Each is a hard constraint:
    - ABSOLUTE RULE: every Theme/SubTheme you output MUST exist verbatim in `kcm_json`. Before writing a
      competency, locate its exact `type`, `theme` and `sub_theme` in `kcm_json`. If you cannot find it
      there, do not include it. Never invent, paraphrase, rename or approximate. Do not use general
      knowledge to name a competency - `kcm_json` is the only permitted source. Copy
      character-for-character.
    - NO CROSS-MIXING BETWEEN ENTRIES: the `theme` and `sub_theme` of one output competency must come
      from the SAME object in `kcm_json`. Never pair a theme from one entry with a sub_theme from another,
      even when each value exists somewhere in the dataset. Before finalising each competency, verify that
      the exact theme + sub_theme combination appears together in one single entry.
    - NO FIELD-SWAPPING: even within one entry, put each value in its matching field. `theme` -> Theme and
      `sub_theme` -> SubTheme, never interchanged.
    - NO CROSS-CATEGORY MAPPING: a Behavioural entry may never appear under FunctionalCompetencies or vice
      versa. The `type` field in `kcm_json` is authoritative.
    - Any competency violating the above is discarded downstream, so a sloppy pair is not a partial
      credit - it is a lost competency.

    - High-confidence mappings (>= 0.85) go in the main arrays. Anything from 0.70 to 0.85 goes to
      `SuggestiveCompetencies` with Confidence and Rationale. Below 0.70, omit entirely.
    - If the primary area is "Domain", both arrays must be [].

### 4. Domain competencies, Sector, SubSector, SubSectorTheme
    - In this framework the Domain competency IS the SGOS classification. `kcm_json` contains no Domain
      entries; `sgos_json` is the only Domain source.
    - Populate ONLY if the primary area is "Domain". Otherwise: Sector, SubSector and SubSectorTheme must
      be null and DomainCompetencies must be [].
    - When the primary area IS "Domain":
        - Choose Sector, SubSector and SubSectorTheme as an existing path in `sgos_json`. The theme must
          actually belong to the chosen subsector, which must belong to the chosen sector.
        - Mirror the same decision into DomainCompetencies as
          [{"Theme": "<SubSector>", "SubTheme": "<SubSectorTheme>"}].
        - Require BOTH: (a) semantic alignment of the course's key concepts with the SGOS theme, and
          (b) contextual alignment - the course's actual purpose falls within that sector's real-world
          mandate (role relevance, ministry function, operational outcome).
        - If only one of the two holds, do not force a mapping: leave the fields null and explain why in
          `explain.sgos_reason`.
        - Record evidence phrases and confidence in `explain.sgos_reason`.

### 5. Prior knowledge declared by author/speaker
    - Covers prerequisites *explicitly stated* in the course introduction, speaker narration,
      presentation text, or documents (transcript or PDF summaries).
    - If found, set InstructorDeclared true and fill DeclaredLevel (None/Basic/Moderate/Advanced),
      DeclaredTopics, DeclaredNotes (1-2 sentences), Required (Yes/No), Confidence.
    - If not found, set InstructorDeclared false, leave the other fields empty, and populate
      SuggestivePriorKnowledge instead.

### 6. Suggestive prior knowledge (AI-inferred)
    - Populate ONLY when PriorKnowledgeDeclared.InstructorDeclared is false.
    - Emit only if your confidence is >= 0.75; otherwise set AIRecommended false and leave empty.
    - Fields: AIRecommended, SuggestedLevel (Basic/Moderate/Advanced), SuggestedTopics (3-6),
      Required (Yes/No), SuggestedLearningPath (2-5 prerequisite TOPICS in the order a learner should
      study them before this course; you are not given course titles, so never write course titles),
      Confidence, Rationale.
    - Never list as a prerequisite something this course itself teaches.
    - Never mix declared and suggested topics. If both objects apply, Declared takes priority.

### 7. Learning outcomes and Bloom taxonomy
    **LearningOutcomes:**
    __LO_RULES__

    **BloomTaxonomy:**
    - One entry per Bloom level (__BLOOM_LEVELS__) that the content actually exercises through
      explanation, demonstration, worked examples, exercises or assessment. Omit levels with no evidence.
    - Saying "you will analyse" without any analysis in the content is not evidence of Analyze. An
      awareness-level course usually reaches Remember / Understand, and Apply only if it walks through
      a procedure the learner then performs.
    - Each entry: Level, Confidence (0-1), Rationale naming the learner activity at that level, and
      EvidencePhrase (verbatim from the transcript or PDF summaries, at most 25 words).
    - Order from lowest to highest level. At least one entry when any content is supplied.

### 8. Transcript analysis
    - If no transcript was provided, return empty values with 0.0 confidences. Do not fabricate.
    - KeywordsExtracted: 10-15 ranked keywords/phrases (1-4 words), with `Method`
      ("term-frequency" | "semantic-importance") and Confidence.
    - LearningTone: one of Instructional / Awareness / Persuasive / Narrative / Technical, plus Confidence.
    - CognitiveMarkers: Bloom markers actually observed. For each: Verb, ExamplePhrase (quoted from the
      transcript), EstimatedLevel 1-6, Confidence. Bloom mapping: Remember/Understand 1-2, Apply 3,
      Analyze 4, Evaluate 5, Create/Design 6.
    - CompetencySignals: probable KCM matches. For each: Category, Theme, SubTheme, Confidence and
      TriggerPhrases (2-6 phrases from the transcript that caused the match). Include only >= 0.70.

### 9. Target employee groups, target roles and role exclusions
    - TargetEmployeeGroups: 1-3 cadre bands from exactly {__EMPLOYEE_GROUPS__}.
      Assign at least one. Avoid listing all four unless the course is genuinely broad and foundational
      (e.g. public service ethics).
    - Classify by the seniority the content actually addresses, using Indian government service
      classification rules:
        - Group A / Group B - gazetted and senior officers: policymakers, managers, specialists.
          Indicative: IAS, IPS, Secretary, Joint Secretary, Director, Deputy Secretary, Under Secretary,
          Section Officer, Engineers, Doctors, Scientists.
        - Group C / Group D - supporting, clerical and operational staff. Indicative: Clerk, Assistant,
          Stenographer, Personal Assistant, Data Entry Operator, Technician, Constable, Driver, MTS,
          Helper.
      Decide from the designation seniority the course targets and the cognitive depth of the content,
      not from the subject area alone. Indicative sector leaning:
        Governance/Policy -> A,B | Technology/IT -> A,B,C | Finance -> A,B | Education -> A,B,C
        Infrastructure -> A,B | Health -> A,B | Social/Welfare -> B,C,D | Administration -> all
    - Targetroles: designations from `designation_candidates` whose holders perform one or more of the
      course's TargetResponsibilitiesFulfilled as part of their normal duties, at the depth the course
      teaches. Return up to 10, best fit first. Copy DesignationId and Name exactly as given. Anything
      not in the shortlist will be rejected.
      Each entry needs Confidence (0-1) and Rationale naming the responsibility it would use and the
      content phrase that teaches it.
    - Judge from the designation's duties as its name plainly implies - never from seniority alone, and
      never only because the name contains a course keyword.
    - If `designation_candidates` is empty or nothing in it genuinely fits, return Targetroles: [].
      Do not invent designations to fill the quota.
    - Targetrolesexclusions: up to 10 shortlisted designations that look related (shared words, same
      department or sector) but would NOT use what this course teaches, or that work at a clearly
      different scope (e.g. the course teaches portal data entry; the designation formulates policy).
      Same fields and copying rules as Targetroles; the Rationale says why it does not fit. A
      designation may appear in Targetroles or Targetrolesexclusions, never both. [] if none.
    - Write role and designation prose in output_language.

### 10. STO LEVEL CLASSIFICATION - STRATEGIC / TACTICAL / OPERATIONAL

    10.1 MANDATORY STO CLASSIFICATION
    For every course, assign exactly ONE STO level.
    STOLevel.Level MUST be exactly one of: "Strategic", "Tactical", "Operational".
    Never return: null; []; "Unknown"; "Not Applicable"; "Mixed"; "Tactical/Operational";
    "Strategic/Tactical"; any hybrid, combined, or non-canonical value.
    If evidence is limited, incomplete, or ambiguous, you MUST still select the single best-fit STO
    level and reduce the confidence accordingly.

    10.2 CORE CLASSIFICATION PRINCIPLE
    STO classification MUST be performed in the context of the mapped:
    Competency Theme -> Competency Subtheme -> Course Content
    The classification question is:
    "At what Strategic, Tactical, or Operational level does this course develop the capability
    represented by this specific Competency Theme/Subtheme?"
    Do NOT classify the course solely as a standalone subject.
    Which Theme/Subtheme to use:
    - Functional / __BEHAVIOURAL_LABEL__ course: the highest-confidence pair in the main competency array.
    - Domain course: SubSector as the Theme and SubSectorTheme as the Subtheme.
    - If nothing was mapped, classify from the course content alone and say so in the rationale.

    Critical rule
    Competency Theme/Subtheme provides the capability context; course content determines the STO depth.
    In other words:
    - Theme tells you the broader capability domain.
    - Subtheme tells you the specific capability area.
    - Course content tells you the depth at which that capability is actually taught.
    - Target audience/role provides contextual validation only.
    The same Competency Theme/Subtheme can legitimately contain courses at Operational, Tactical, and
    Strategic levels.

    Example
    Competency Theme: Financial Management
    Subtheme: Budget Management
    Possible courses:
    - "Budget Data Entry and Reporting" -> Operational
    - "Budget Monitoring and Resource Allocation" -> Tactical
    - "Fiscal Policy and Institutional Financial Reform" -> Strategic
    Therefore, NEVER assign the STO level based only on the Theme/Subtheme name.

    10.3 EVIDENCE HIERARCHY
    Use evidence in the following order of priority:
    1. Learning objectives / learning outcomes stated in the content
    2. Detailed course/module content (transcript and PDF summaries)
    3. Assessments, exercises and practical activities described in the content
    4. Target audience / TargetEmployeeGroups
    5. Target roles / designations
    6. Difficulty / LearningLevel
    When higher-priority evidence conflicts with lower-priority evidence, higher-priority evidence MUST
    prevail.
    If `sto_json` is supplied and has an entry for the mapped Theme/Subtheme, compare the course with
    that entry's Operational, Tactical and Strategic labels first: they describe this specific
    capability at each level and override the general framework below where the two disagree. Quote
    the matching label in PositiveSignalsMatched. With no matching entry, use the framework below.

    10.4 WHAT MUST BE CLASSIFIED
    Classify the capability the learner is expected to perform after completing the course, not merely
    the subject the course discusses.
    Always ask:
    "What does this course enable the learner to DO within this specific Competency Theme/Subtheme?"
    Identify the strongest: Verb + Object
    Examples: apply procurement rules; enter beneficiary data; maintain records; monitor scheme
    performance; analyse implementation gaps; coordinate corrective action; allocate programme
    resources; design policy frameworks; formulate institutional reforms; shape long-term governance
    direction.
    The learner's expected action is more important than the course title or subject terminology.

    10.5 STO FRAMEWORK

    OPERATIONAL
      Core definition
      Operational courses primarily enable learners to execute, apply or follow established processes,
      rules, tools, procedures or services.
      Focus: Execution & Compliance
      Decision scope: task, transaction, procedure, process or point of service
      Time horizon: immediate / short-term
      Thinking style: concrete, practical, procedural and real-time problem solving
      Typical learner actions: apply; execute; follow; use; operate; enter; record; document; report;
        process; comply; deliver; maintain
      Positive signals: implementation; documentation; compliance; reporting; portal usage; data entry;
        service delivery; beneficiary interaction; step-by-step procedures; form/register filling;
        tool or portal walkthrough; record maintenance; application of established rules/SOPs;
        routine process execution
      Negative signals: policy formulation; institutional design; system transformation; long-term
        governance strategy; vision setting; reform architecture; system-level decision making

    TACTICAL
      Core definition
      Tactical courses primarily enable learners to manage, analyse, coordinate, monitor, adapt or
      improve implementation.
      Focus: Implementation Refinement & Coordination
      Decision scope: team, programme, scheme, unit or implementation process
      Time horizon: medium-term / programme level
      Thinking style: analytical, adaptive and practical
      Typical learner actions: analyse; monitor; evaluate; coordinate; supervise; optimise; allocate;
        troubleshoot; review; improve; adapt; manage; course-correct
      Positive signals: monitoring; evaluation; resource allocation; stakeholder coordination;
        bottleneck resolution; process improvement; data-driven adjustment; supervision; review
        meetings; gap analysis; scheme-level planning; team management; implementation optimisation;
        risk mitigation; corrective action; programme-level planning
      Negative signals: routine data entry; basic procedural execution; pure compliance; routine
        service delivery; basic reporting; long-term policy architecture; institutional reform design;
        system-level vision setting

    STRATEGIC
      Core definition
      Strategic courses primarily enable learners to design, formulate, reshape or influence policies,
      institutions, systems or long-term direction.
      Focus: Vision, Policy & Institutional Design
      Decision scope: organisation, institution, system, policy, regulatory framework or cross-sector
        ecosystem
      Time horizon: long-term / system level
      Thinking style: conceptual, predictive, systemic and abstract
      Typical learner actions: design; formulate; establish; transform; reform; shape; strategise;
        govern; architect; institutionalise; anticipate; set direction
      Positive signals: vision setting; policy design; policy formulation; institutional strengthening;
        reform agenda; strategic alignment; resource mobilisation; systems transformation; cross-sector
        convergence; regulatory framework design; inter-ministerial convergence; institutional
        architecture; long-term governance strategy; system-level transformation; policy reform
      Negative signals: routine implementation; basic monitoring; administrative data entry;
        procedural application; basic compliance; routine service delivery; routine reporting

    10.6 DECISION PROCEDURE
    Follow these steps in order.

    Step 1 - Understand the Competency Theme
    Identify the broader capability domain represented by the Theme.
    Use it to establish the semantic context.
    Do not assign STO from the Theme alone.

    Step 2 - Understand the Competency Subtheme
    Identify the specific capability represented by the Subtheme.
    The Subtheme is the primary semantic anchor for interpreting what the course is contributing to.
    Do not assign STO from the Subtheme name alone.

    Step 3 - Identify the Course Contribution
    Determine exactly what capability the course develops within that Theme/Subtheme.
    Ask: "What capability does the learner gain in this specific Subtheme after completing this
    course?"

    Step 4 - Extract Learner Actions
    Identify the strongest actions taught by the course.
    Focus on: what learners perform; what learners analyse; what learners manage; what learners
    improve; what learners design; what decisions learners make.

    Step 5 - Determine Decision Scope
    Classify the dominant scope of the learner's responsibility:
    - Operational: task / transaction / procedure / service
    - Tactical: team / programme / scheme / implementation process
    - Strategic: organisation / institution / system / policy / cross-sector direction

    Step 6 - Determine Thinking Depth
    - Operational: apply established approaches.
    - Tactical: analyse, manage, coordinate, adapt or improve existing approaches.
    - Strategic: design, formulate, anticipate, reshape or transform approaches.

    Step 7 - Determine Relevant Time Horizon
    Use the learner's actual decision/planning horizon:
    - Immediate / short-term -> supports Operational
    - Medium-term / programme level -> supports Tactical
    - Long-term / system level -> supports Strategic
    Do NOT use course duration as the time horizon.
    A course lasting six months is not automatically Strategic.
    Likewise, mentioning a 10-year government vision does not make a course Strategic unless the
    learner is actually trained to work at that long-term/system level.

    Step 8 - Check Positive and Counter Signals
    Identify: strongest evidence supporting the selected level; meaningful evidence suggesting another
    level; evidence that argues against the selected level.
    Do not treat the absence of a signal as a counter-signal.

    Step 9 - Determine the Dominant STO Level
    Select the level with the strongest overall alignment across: Competency Theme; Competency
    Subtheme; course capability; learner actions; decision scope; thinking depth; relevant time
    horizon.
    The dominant learning purpose must determine the final classification.
    Do NOT select the highest level merely because one section, phrase or module sounds more senior.

    10.7 MIXED-LEVEL COURSES
    A course may contain content associated with more than one STO level.
    When this happens:
    Classify according to the dominant capability that the course actually develops within the mapped
    Competency Subtheme.
    Do NOT automatically select the highest level mentioned.

    Example
    A course may: introduce a national policy; explain implementation; demonstrate a reporting portal;
    discuss future reforms.
    If learners are primarily trained to apply the policy and complete implementation activities,
    classify as: Operational.
    The mention of future reforms does not make it Strategic.
    Similarly, if learners primarily analyse implementation performance, identify gaps and coordinate
    corrective action, classify as: Tactical.
    Only classify as Strategic when learners are actually trained to design, formulate, reshape or
    influence policy, institutions, systems or long-term direction.

    10.8 CRITICAL CLASSIFICATION CAUTIONS

    10.8.1 Subject Is Not STO Level
    A course about policy, law, governance, schemes, national missions or institutional reform is NOT
    automatically Strategic.
    Classify based on what the learner is trained to do.
    - Apply established policy/rules -> Operational
    - Monitor/analyse/improve implementation -> Tactical
    - Design/formulate/reform policy or institutions -> Strategic
    Handling a policy is not the same as shaping the policy.

    10.8.2 Awareness Is Not Strategic
    Awareness, orientation, introduction or understanding of a strategic topic does not automatically
    make a course Strategic.
    Classify according to the depth of capability actually taught.

    10.8.3 Designation Is Not STO Level
    Designation seniority is weak evidence.
    Never assume: senior designation = Strategic; junior designation = Operational.
    The same designation can perform different types of work.
    If designation conflicts with clear course-content evidence: keep the content-derived STO level.

    10.8.4 Formal Language Is Not STO Level
    Do not infer Strategic simply because the course uses: ministry terminology; national mission
    terminology; policy terminology; executive language; institutional terminology; words such as
    "transformation", "strategy", "vision" or "reform".
    The learner must actually be trained to perform Strategic-level activities.

    10.8.5 Difficulty Is Not STO Level
    STOLevel is independent of LearningLevel.
    Do NOT use Beginner = Operational; Intermediate = Tactical; Advanced = Strategic as an automatic
    mapping.

    10.8.6 Provider Is Not STO Level
    Do not infer STO from: provider reputation; government provider; university/provider type;
    certification; course format; delivery mode; course duration.

    10.9 TARGET AUDIENCE AND ROLE VALIDATION
    TargetEmployeeGroups and TargetRoles may be used as contextual validation.
    Typical patterns may be:
    - Strategic -> often senior / Group A
    - Tactical -> often Group A / Group B
    - Operational -> often Group C / Group D
    These are not classification rules.
    If target audience or designation conflicts with strong course-content evidence: content-derived
    classification takes precedence.
    Mention the divergence in the rationale only when it is materially relevant.

    10.10 INDEPENDENCE FROM OTHER CLASSIFICATIONS
    STO classification is independent of: LearningLevel; difficulty; RubricScoring.Classification;
    delivery mode; course duration; certification; provider; course format.
    Do not allow these attributes to determine STO level.

    10.11 CONFIDENCE CALIBRATION
    STOLevel.Confidence must reflect the quality, specificity, completeness and consistency of
    evidence.
    If evidence_tier is "thin_transcript" or "pdf_only", Confidence MUST NOT exceed 0.80.
    Higher confidence requires clear evidence of: learner actions; decision scope; thinking depth;
    substantive course content; alignment with the mapped Competency Theme/Subtheme.
    Do not assign high confidence merely because the course title, designation or provider suggests a
    particular level.

    10.12 MANDATORY STO RATIONALE / REASON
    For every course, Rationale is MANDATORY.
    The rationale MUST explain why this specific STO level was suggested for this specific course.
    It must connect: Competency Theme -> Competency Subtheme -> Course Capability -> Learner Action ->
    Decision Scope -> STO Level
    The rationale MUST include:
    1. Competency context - how the course contributes to the specific Theme/Subtheme.
    2. Learner capability - what the learner is actually enabled to do.
    3. Decision scope - task, programme/team, or system/policy level.
    4. Reason for selected STO - why the evidence supports Strategic, Tactical or Operational.
    5. Distinction from adjacent level, where useful - why it is not the next higher/lower level.
    Rationale must NOT: simply repeat "Strategic", "Tactical" or "Operational"; rely only on the course
    title; rely only on designation; make generic claims; invent evidence; assume seniority from formal
    terminology.

    Good rationale
    "Within the Scheme Monitoring subtheme, the course enables learners to analyse implementation data,
    identify performance gaps and coordinate corrective actions. These activities operate at
    programme-level implementation and require analytical decision making to improve execution, which
    supports a Tactical classification rather than Operational routine execution or Strategic policy
    design."

    Another good rationale
    "Within the Scheme Administration subtheme, the course focuses on using the prescribed portal,
    entering beneficiary information and completing established reporting procedures. The learner is
    therefore being trained primarily for task-level execution and compliance, supporting an
    Operational classification rather than Tactical programme management."

    10.13 EVIDENCE PHRASES
    EvidencePhrases MUST contain actual verbatim phrases from the supplied course inputs that
    materially contributed to the classification.
    Prefer evidence from: learning objectives; learning outcomes; transcript; modules; assessments;
    practical activities; course description.
    Do not invent phrases.
    Do not rely only on the title when substantive course evidence exists.

    10.14 OUTPUT FORMAT
    Return exactly the following JSON structure, as the STOLevel member of the single output object:
        "STOLevel": {
          "Level": "Strategic",
          "Confidence": 0.86,
          "Rationale": "Within the mapped Competency Theme/Subtheme, the course enables learners to design policy frameworks and shape institutional reform. These activities operate at a system-level and long-term decision scope, supporting a Strategic classification rather than Tactical implementation management or Operational execution.",
          "PositiveSignalsMatched": [
            "policy framework design",
            "institutional reform",
            "system-level decision making"
          ],
          "CounterSignals": [
            "No material evidence that the course is limited to routine implementation or programme-level monitoring."
          ],
          "EvidencePhrases": [
            "design policy frameworks",
            "institutional reform"
          ]
        }

    Field requirements
    - Level: must be exactly "Strategic", "Tactical" or "Operational".
    - Confidence: must be a number between 0.0 and 1.0.
    - Rationale: mandatory. Must explain why the selected STO level is appropriate for this course
      within the mapped Competency Theme/Subtheme.
    - PositiveSignalsMatched: list the strongest signals supporting the selected STO level.
    - CounterSignals: list meaningful evidence against the selected level or evidence supporting an
      alternative level. If there are no meaningful counter-signals, return []. Do not invent
      counter-signals.
    - EvidencePhrases: must contain actual verbatim phrases from the supplied inputs.

    10.15 FINAL SELF-CHECK
    Before returning the classification, verify:
    - What is the Competency Theme?
    - What is the Competency Subtheme?
    - What capability does the course contribute to that Subtheme?
    - What will the learner actually DO?
    - Is the dominant action executing, managing/improving, or designing/shaping?
    - What is the decision scope?
    - What is the relevant learner decision/planning horizon?
    - Which STO level has the strongest overall evidence?
    - Am I incorrectly using designation, title, provider, difficulty or course duration as the primary
      evidence?
    - Is the selected level consistent with the course's actual depth?
    - Does the rationale clearly explain why this specific STO level was selected?
    - Does the rationale distinguish the selected level from the nearest alternative where necessary?
    - Are all EvidencePhrases actually present in the supplied input?
    - Is exactly ONE STO level returned?
    If the rationale cannot clearly answer "Why this STO level for this course?", revise the
    classification explanation before returning the result.

    CORE RULE
    Theme/Subtheme provides the capability context. Course content determines the capability depth.
    Learner actions determine the dominant level. Decision scope validates the level. The rationale
    must explain why that level is appropriate.

### 11. Rubric scoring
    - Score each of the seven parameters 0-100 under a strict ZERO-BASE policy: every parameter starts at
      0 and only rises on real, explicit, or confidently inferred evidence from the supplied inputs.
      No default or buffer minimums.
    - Award points only where evidence is clear or your confidence is >= 0.75. Otherwise leave 0.
    - Every score must be justifiable by a phrase from the transcript or PDF summaries, recorded in
      `explain.mapping_reasons`.

    LO and BT score the learning objectives STATED IN THE CONTENT (the transcript or PDF summaries say
    what the learner will be able to do). Outcomes you write yourself do not count.
    1. LearningObjectives (LO)
       +10 per measurable, clearly written objective stated in the content that starts with a Bloom
       verb (max 100). Vague phrasing ("learn about", "understand") = +2 only. None stated = 0.
    2. PriorKnowledge (PK)
       0-100 according to how explicitly prerequisites are stated and how well they match the content.
       No evidence = 0.
    3. BloomTaxonomy (BT)
       Per stated objective verb: Remember/Identify 20, Understand 40, Apply 60, Analyze 80,
       Evaluate/Create 100. Average across all stated objective verbs. If the content states no
       objectives, score the levels in your BloomTaxonomy entries the same way instead, averaged.
    4. ComplexityOfContent (CC)
       +15 moderate domain-term density (5-10 per 1000 words); +30 high (10-20 per 1000);
       +50 advanced analytical content, formulas or policy instruments;
       +70-100 very complex, technical or data-heavy. Purely awareness-level or very short = 0.
    5. ExpectedRoleOutcome (ERO)
       +20 clear job-related tasks; +30-50 maps to 1-3 designations;
       +60-80 maps to 6+ designations with role-level use cases; +100 highly specialised with measurable
       job outcomes. No job/role connection = 0.
    6. ExtentOfLearning (EOL)
       By duration: <30 min +10; 30-90 min +20-40; >90 min +50-70; multi-module >3 hrs +80-100.
       Practical components (exercises, labs, activities) add +10-20. Single short video = 0-10.
       Use duration_seconds from the input, not your own guess. If it is null, estimate from the
       amount of content and lower your confidence.
    7. TargetAudienceAlignment (TAA)
       +10 general public-servant audience is clear; +20-40 matched to a specific cadre or designation at
       confidence >= 0.8; +50-70 content difficulty aligns with designation level;
       +100 sector + role + competency all align. Unspecified or mismatched = 0.

    - Report the seven scores. Also report TotalScore and Classification, computed as:
__RUBRIC_TABLE__
      __RUBRIC_FORMULA__
      Classification: TotalScore <= __BEGINNER_MAX__ -> Beginner; <= __INTERMEDIATE_MAX__ -> Intermediate;
      otherwise Advanced.
    - LearningLevel MUST equal RubricScoring.Classification.

### 12. Learning mode
    - LearningMode: one of __LEARNING_MODES__, inferred from what the content says about how it is
      delivered (live sessions, classroom days, self-study videos). Recorded video lectures with no live
      component are self-study.

### 13. Reference resources and cited references
    - ReferenceResources.AssignmentsAndPracticeLinks and .ExtendedLearning: only URLs actually present in
      the supplied inputs. Never fabricate a URL. Empty arrays if none.
    - ReferencesCited: every source the content cites or points learners to - acts, rules,
      notifications, circulars, government orders, reports, standards, frameworks, books, articles,
      portals, websites and helplines.
        - Citation: as stated in the content (name plus number / year when given).
        - Type: the closest of the enum values.
        - URL: only if the URL text appears in the inputs, copied exactly; otherwise null.
        - Source: "transcript" or "pdf".
      Do not add references the content does not mention, however well known. [] if none.

### 14. Limited content
    - If the transcript is short or only PDF summaries are available, still generate all required fields
      from what is supplied, state the limitation explicitly in `explain.missing_info`, and lower the
      affected confidences.

### 15. Explain object (mandatory - this is the audit trail)
    - primary_area_reason: why this competency area, citing evidence.
    - sgos_reason: the SGOS decision, evidence phrases, and confidence - or why no mapping was made.
    - competency_mapping_triggers: one entry per mapped competency with the phrases that triggered it.
    - mapping_reasons: one entry per rubric parameter and per generated field group, with the evidence.
    - designation_inference: one entry per returned designation with its evidence.
    - sto_reason: the STO level decision - the Competency Theme/Subtheme context, the learner
      capability and decision scope that decided it, the evidence, and any divergence from
      TargetEmployeeGroups/Targetroles.
    - learning_outcomes_validation: whether purpose, process and application are each covered, and the
      Bloom verbs used.
    - prior_knowledge_confidence, missing_or_low_confidence_fields, missing_info, evidence_tier_used.

### 16. Output
    - A single valid JSON object, UTF-8, ISO8601 timestamps, no markdown.
"""


_LO_RULES_UNLABELLED = """    - Generate 5-6 clear, measurable outcomes that implicitly cover purpose (why), process (how) and
      application (what).
    - Do NOT prefix outcomes with "Why:", "How:" or "What:" - the labels are recorded separately in
      `explain.learning_outcomes_validation`, not shown to learners.
    - Begin each with a strong action verb (Identify, Apply, Analyze, Evaluate, Develop, Implement,
      Interpret). Avoid "Learn about" / "Understand about".
    - Use professional instructional-design language suitable for government and public-sector learning.
    - Cover a cognitive progression from foundational understanding to applied or strategic capability.
    - Ground every outcome in the supplied content. If the transcript or PDF summaries state learning
      objectives, use them as the primary source and refine them - do not discard them."""

_LO_RULES_WHW = """    - Generate 3-5 outcomes. Each must be prefixed with exactly one of "Why: ", "How: " or "What: ".
    - The set must contain at least one "Why:", one "How:" and one "What:" outcome.
    - Ground every outcome in the supplied content, using objectives stated in it where available."""

# ---------------------------------------------------------------- output schema
# Gemini/Vertex structured output accepts an OpenAPI-3.0 subset: type, format,
# description, nullable, enum, items, properties, required, min/maxItems,
# v3.7: maxItems is NOT used. Combined with this schema's size it makes Vertex
# reject the request with a bare 400 INVALID_ARGUMENT (bisected Nov 2026);
# upper counts are stated in the prompt and checked in output_models.py.
# propertyOrdering, anyOf. Union types (["string","null"]) are NOT accepted --
# use "nullable": True instead.

_COMPETENCY_PAIR = {
    "type": "object",
    "properties": {
        "Theme": {"type": "string"},
        "SubTheme": {"type": "string"},
    },
    "required": ["Theme", "SubTheme"],
}

_SUGGESTIVE_PAIR = {
    "type": "object",
    "properties": {
        "Theme": {"type": "string"},
        "SubTheme": {"type": "string"},
        "Confidence": {"type": "number"},
        "Rationale": {"type": "string"},
    },
    "required": ["Theme", "SubTheme", "Confidence", "Rationale"],
}

_DESIGNATION = {
    "type": "object",
    "properties": {
        "DesignationId": {"type": "string"},
        "Name": {"type": "string"},
        "Confidence": {"type": "number"},
        "Rationale": {"type": "string"},
    },
    "required": ["DesignationId", "Name", "Confidence", "Rationale"],
}

FULL_METADATA_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "Do_ID": {"type": "string", "description": "Unique course identifier."},
        "CourseName": {"type": "string"},
        "CourseSummary": {"type": "string"},
        "CourseDescription": {"type": "string"},
        "LearningOutcomes": {"type": "array", "items": {"type": "string"}},
        "LearningLevel": {
            "type": "string",
            "enum": ["Beginner", "Intermediate", "Advanced"],
        },
        "STOLevel": {
            "type": "object",
            "properties": {
                "Level": {
                    "type": "string",
                    "enum": ["Strategic", "Tactical", "Operational"],
                },
                "Confidence": {"type": "number"},
                "Rationale": {"type": "string"},
                "PositiveSignalsMatched": {"type": "array", "items": {"type": "string"}},
                "CounterSignals": {"type": "array", "items": {"type": "string"}},
                "EvidencePhrases": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["Level", "Confidence", "Rationale"],
        },
        "LearningMode": {"type": "string", "enum": LEARNING_MODES},
        "Duration": {"type": "string"},
        "Language": {"type": "string"},
        # --- Domain classification (SGOS). Null unless primary area is Domain.
        "Sector": {"type": "string", "nullable": True},
        "SubSector": {"type": "string", "nullable": True},
        "SubSectorTheme": {"type": "string", "nullable": True},
        "DomainCompetencies": {"type": "array", "items": _COMPETENCY_PAIR},
        # --- KCM classification
        "PrimaryCompetencyArea": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["Domain", "Functional", BEHAVIOURAL_LABEL],
                },
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["name", "reason", "confidence"],
        },
        "FunctionalCompetencies": {"type": "array", "items": _COMPETENCY_PAIR},
        BEHAVIOURAL_KEY: {"type": "array", "items": _COMPETENCY_PAIR},
        "SuggestiveCompetencies": {
            "type": "object",
            "properties": {
                "Functional": {"type": "array", "items": _SUGGESTIVE_PAIR},
                BEHAVIOURAL_LABEL: {"type": "array", "items": _SUGGESTIVE_PAIR},
                "Domain": {"type": "array", "items": _SUGGESTIVE_PAIR},
            },
        },
        "Tags": {"type": "array", "items": {"type": "string"}},
        # --- Embedding text and tutor (v3.7)
        "TargetResponsibilitiesFulfilled": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Responsibility": {"type": "string"},
                    "EvidenceQuote": {"type": "string"},
                    "Confidence": {"type": "number"},
                },
                "required": ["Responsibility", "EvidenceQuote", "Confidence"],
            },
        },
        "AuthorName": {"type": "string", "nullable": True},
        # --- Audience
        "TargetEmployeeGroups": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "enum": EMPLOYEE_GROUPS},
        },
        "Targetroles": {"type": "array", "items": _DESIGNATION},
        "Targetrolesexclusions": {
            "type": "array",
            "items": _DESIGNATION,
        },
        # --- Bloom levels the content exercises (v3.7)
        "BloomTaxonomy": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Level": {"type": "string", "enum": BLOOM_LEVELS},
                    "Confidence": {"type": "number"},
                    "Rationale": {"type": "string"},
                    "EvidencePhrase": {"type": "string"},
                },
                "required": ["Level", "Confidence", "Rationale"],
            },
        },
        # --- Prior knowledge
        "PriorKnowledgeDeclared": {
            "type": "object",
            "properties": {
                "InstructorDeclared": {"type": "boolean"},
                "DeclaredLevel": {
                    "type": "string",
                    "enum": ["None", "Basic", "Moderate", "Advanced"],
                },
                "DeclaredTopics": {"type": "array", "items": {"type": "string"}},
                "DeclaredNotes": {"type": "string"},
                "Required": {"type": "string", "enum": ["Yes", "No"]},
                "Confidence": {"type": "number"},
            },
            "required": ["InstructorDeclared"],
        },
        "SuggestivePriorKnowledge": {
            "type": "object",
            "properties": {
                "AIRecommended": {"type": "boolean"},
                "SuggestedLevel": {
                    "type": "string",
                    "enum": ["Basic", "Moderate", "Advanced"],
                },
                "SuggestedTopics": {"type": "array", "items": {"type": "string"}},
                "Required": {"type": "string", "enum": ["Yes", "No"]},
                "SuggestedLearningPath": {"type": "array", "items": {"type": "string"}},
                "Confidence": {"type": "number"},
                "Rationale": {"type": "string"},
            },
            "required": ["AIRecommended"],
        },
        # --- Scoring
        "RubricScoring": {
            "type": "object",
            "properties": {
                # minimum/maximum are enforced by the response schema, so a model
                # that drifts onto a 0-10 scale is rejected rather than silently
                # producing scores an order of magnitude low.
                **{
                    k: {"type": "integer", "minimum": 0, "maximum": 100}
                    for k in RUBRIC_KEYS
                },
                "TotalScore": {"type": "integer", "minimum": 0, "maximum": 100},
                "Classification": {
                    "type": "string",
                    "enum": ["Beginner", "Intermediate", "Advanced"],
                },
            },
            "required": RUBRIC_KEYS + ["TotalScore", "Classification"],
        },
        # --- Transcript analysis (typed, so the evidence survives)
        "TranscriptAnalysis": {
            "type": "object",
            "properties": {
                "KeywordsExtracted": {
                    "type": "object",
                    "properties": {
                        "Values": {"type": "array", "items": {"type": "string"}},
                        "Method": {"type": "string"},
                        "Confidence": {"type": "number"},
                    },
                },
                "LearningTone": {
                    "type": "object",
                    "properties": {
                        "Value": {
                            "type": "string",
                            "enum": [
                                "Instructional",
                                "Awareness",
                                "Persuasive",
                                "Narrative",
                                "Technical",
                            ],
                            "nullable": True,
                        },
                        "Confidence": {"type": "number"},
                    },
                },
                "CognitiveMarkers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Verb": {"type": "string"},
                            "ExamplePhrase": {"type": "string"},
                            "EstimatedLevel": {"type": "integer"},
                            "Confidence": {"type": "number"},
                        },
                        "required": ["Verb", "EstimatedLevel", "Confidence"],
                    },
                },
                "CompetencySignals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Category": {
                                "type": "string",
                                "enum": ["Functional", BEHAVIOURAL_LABEL, "Domain"],
                            },
                            "Theme": {"type": "string"},
                            "SubTheme": {"type": "string"},
                            "Confidence": {"type": "number"},
                            "TriggerPhrases": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["Category", "Theme", "Confidence"],
                    },
                },
            },
        },
        "ReferenceResources": {
            "type": "object",
            "properties": {
                "AssignmentsAndPracticeLinks": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "ExtendedLearning": {"type": "array", "items": {"type": "string"}},
            },
        },
        "ReferencesCited": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Citation": {"type": "string"},
                    "Type": {
                        "type": "string",
                        "enum": [
                            "Act or rule",
                            "Government order or circular",
                            "Report or publication",
                            "Standard or framework",
                            "Book or article",
                            "Website, portal or helpline",
                            "Other",
                        ],
                    },
                    "URL": {"type": "string", "nullable": True},
                    "Source": {"type": "string", "enum": ["transcript", "pdf"]},
                },
                "required": ["Citation", "Type", "Source"],
            },
        },
        # --- Audit trail. Typed on purpose: bare {"type":"object"} yields {}.
        "explain": {
            "type": "object",
            "properties": {
                "primary_area_reason": {"type": "string"},
                "sgos_reason": {"type": "string"},
                "sto_reason": {"type": "string"},
                "competency_mapping_triggers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Category": {
                                "type": "string",
                                "enum": ["Functional", BEHAVIOURAL_LABEL, "Domain"],
                            },
                            "Theme": {"type": "string"},
                            "SubTheme": {"type": "string"},
                            "TriggerPhrases": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "Confidence": {"type": "number"},
                        },
                        "required": ["Category", "Theme", "Confidence"],
                    },
                },
                "mapping_reasons": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Field": {"type": "string"},
                            "Reason": {"type": "string"},
                            "EvidenceQuote": {"type": "string"},
                        },
                        "required": ["Field", "Reason"],
                    },
                },
                "designation_inference": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "DesignationId": {"type": "string"},
                            "Name": {"type": "string"},
                            "Evidence": {"type": "string"},
                            "Confidence": {"type": "number"},
                        },
                        "required": ["Name", "Evidence", "Confidence"],
                    },
                },
                "learning_outcomes_validation": {
                    "type": "object",
                    "properties": {
                        "CoversPurpose": {"type": "boolean"},
                        "CoversProcess": {"type": "boolean"},
                        "CoversApplication": {"type": "boolean"},
                        "BloomVerbsUsed": {"type": "array", "items": {"type": "string"}},
                        "Notes": {"type": "string"},
                    },
                },
                "prior_knowledge_confidence": {"type": "number"},
                "missing_or_low_confidence_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "missing_info": {"type": "string"},
                "evidence_tier_used": {"type": "string"},
            },
            "required": ["primary_area_reason", "missing_info"],
        },
        # --- Provenance. Overwritten in Python; declared so the model can fill.
        "EmbeddingVectorID": {"type": "string", "nullable": True},
        "Version": {"type": "string"},
        "GeneratedOn": {"type": "string"},
        "Generator": {"type": "string"},
    },
    "required": [
        "Do_ID",
        "CourseName",
        "CourseSummary",
        "CourseDescription",
        "LearningOutcomes",
        "LearningLevel",
        "STOLevel",
        "LearningMode",
        "Duration",
        "PrimaryCompetencyArea",
        "FunctionalCompetencies",
        BEHAVIOURAL_KEY,
        "DomainCompetencies",
        "Tags",
        "TargetResponsibilitiesFulfilled",
        "AuthorName",
        "TargetEmployeeGroups",
        "Targetroles",
        "Targetrolesexclusions",
        "BloomTaxonomy",
        "ReferencesCited",
        "PriorKnowledgeDeclared",
        "SuggestivePriorKnowledge",
        "RubricScoring",
        "TranscriptAnalysis",
        "explain",
    ],
}

# ---------------------------------------------------------------- field config
# Which fields the model generates. Every top-level or dotted field path in
# FULL_METADATA_SCHEMA defaults to "llm"; field_config.json overrides it with
#   "portal" - copied from the portal (title, description, duration)
#   "code"   - computed or overwritten in Python after the call
#   "off"    - not produced at all
# Only "llm" fields stay in the response schema sent to Gemini.
FIELD_SOURCES = ("llm", "portal", "code", "off")
FIELD_CONFIG_PATH = os.environ.get(
    "FIELD_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "field_config.json"),
)


def load_field_config(path: str = FIELD_CONFIG_PATH) -> Dict[str, str]:
    """{dotted field path: source}. Missing file -> every field is "llm"."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    fields = raw.get("fields", raw) if isinstance(raw, dict) else {}
    out: Dict[str, str] = {}
    for key, value in fields.items():
        if key.startswith("_"):
            continue
        source = str(value).strip().lower()
        if source not in FIELD_SOURCES:
            raise ValueError(f"{path}: field {key!r} has invalid source {value!r}")
        out[key.replace("BehaviouralCompetencies", BEHAVIOURAL_KEY)] = source
    return out


def prune_schema(schema: Dict[str, Any], drop: List[str]) -> Dict[str, Any]:
    """Copy of `schema` without the dotted object paths in `drop`."""
    out = json.loads(json.dumps(schema))
    for path in drop:
        parts = path.split(".")
        node = out
        for part in parts[:-1]:
            node = (node.get("properties") or {}).get(part)
            if not isinstance(node, dict):
                break
        else:
            props = node.get("properties") or {}
            if parts[-1] in props:
                del props[parts[-1]]
                if parts[-1] in (node.get("required") or []):
                    node["required"] = [r for r in node["required"] if r != parts[-1]]
    return out


FIELD_CONFIG: Dict[str, str] = load_field_config()


def field_source(path: str) -> str:
    return FIELD_CONFIG.get(path, "llm")


def model_generates(path: str) -> bool:
    return field_source(path) == "llm"


METADATA_SCHEMA: Dict[str, Any] = prune_schema(
    FULL_METADATA_SCHEMA, [k for k, v in FIELD_CONFIG.items() if v != "llm"]
)


def render_system_prompt() -> str:
    """
    Render the system prompt. Deliberately takes no per-course arguments: it must
    be byte-identical for every course so it, plus the masters, form a stable
    cacheable prefix. Per-course values arrive in the input section instead.
    """
    lo_rules = _LO_RULES_WHW if LO_STYLE == "why_how_what" else _LO_RULES_UNLABELLED
    rendered = (
        SYSTEM_PROMPT_TEMPLATE
        .replace("__LO_RULES__", lo_rules)
        .replace("__RUBRIC_TABLE__", _rubric_table())
        .replace("__RUBRIC_FORMULA__", _rubric_formula())
        .replace("__BEGINNER_MAX__", str(BEGINNER_MAX))
        .replace("__INTERMEDIATE_MAX__", str(INTERMEDIATE_MAX))
        .replace("__BEHAVIOURAL_LABEL__", BEHAVIOURAL_LABEL)
        .replace("__EMPLOYEE_GROUPS__", ", ".join(f'"{g}"' for g in EMPLOYEE_GROUPS))
        .replace("__LEARNING_MODES__", " / ".join(LEARNING_MODES))
        .replace("__BLOOM_LEVELS__", ", ".join(BLOOM_LEVELS))
    )
    # v3.6 sent a literal "__COURSE_LANGUAGE__" to the model; fail loudly instead.
    leftover = sorted(set(re.findall(r"__[A-Z][A-Z_]*__", rendered)))
    if leftover:
        raise ValueError(f"unsubstituted prompt tokens: {leftover}")
    return rendered


def build_static_prefix(
    kcm_json: Any, sgos_json: Any, sto_json: Any = None
) -> str:
    """
    The invariant head of every prompt: system rules + the masters.

    Kept first and identical across courses so the masters can be served from
    cache rather than re-billed per course. Reordering this so per-course text
    comes first would silently disable that.

    The competency master is reduced by compact_competency_master() first: the
    raw file is ~815 KB and would otherwise roughly double the prefix.

    sto_json (input/masters/sto_competencies.json) is optional; when given, its
    short level labels are appended (see compact_sto_master). Without it the
    prompt is v3.6's two-master prefix plus the v3.7 rule changes.
    """
    master = compact_competency_master(kcm_json)
    kcm_header = (
        f"[kcm_json] (Karmayogi Competency Model - only valid source of "
        f"Functional/{BEHAVIOURAL_LABEL})"
    )
    sto_block = ""
    if sto_json:
        sto_block = (
            "\n[sto_json] (STO level labels per KCM Theme/SubTheme - used only for "
            "STOLevel)\n"
            + json.dumps(
                compact_sto_master(sto_json), indent=1, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        )
    return f"""{render_system_prompt()}

=================
REFERENCE MASTERS (identical for every course)
=================

{kcm_header}
{json.dumps(master, indent=2, ensure_ascii=False, sort_keys=True)}

[sgos_json] (Sector-SubSector-Theme mapping - only valid source of Domain)
{json.dumps(sgos_json, indent=2, ensure_ascii=False, sort_keys=True)}
{sto_block}"""


def _truncate(text: str, limit: int, label: str) -> str:
    if not text:
        return "None provided."
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[...{label} truncated at {limit:,} characters...]"


def _truncate_transcript(text: str, limit: int) -> str:
    """
    Fit the transcript into `limit` chars by giving every module a fair share.
    v3.6 kept the head, so long courses lost their later modules entirely.
    Relies on the "## Module: <label>" headers course_io writes; without them
    it falls back to head truncation.
    """
    if not text or len(text) <= limit:
        return _truncate(text, limit, "transcript")
    blocks = [b for b in re.split(r"(?=^## Module: )", text, flags=re.M) if b.strip()]
    if len(blocks) <= 1:
        return _truncate(text, limit, "transcript")
    budget = [0] * len(blocks)
    remaining = limit - 60 * len(blocks)
    open_ = set(range(len(blocks)))
    while remaining > 0 and open_:
        share = max(1, remaining // len(open_))
        for i in sorted(open_):
            give = min(len(blocks[i]) - budget[i], share, remaining)
            budget[i] += give
            remaining -= give
            if budget[i] >= len(blocks[i]):
                open_.discard(i)
            if remaining <= 0:
                break
    parts = []
    for block, size in zip(blocks, budget):
        if size >= len(block):
            parts.append(block.rstrip())
        elif size > 0:
            parts.append(block[:size].rstrip() + "\n[...rest of module truncated...]")
    return "\n\n".join(parts)


def build_course_section(
    transcript: str,
    pdf_summaries: str,
    designation_candidates: List[Dict[str, str]],
    evidence_tier: str,
    output_language: Optional[str],
    duration_seconds: Optional[float],
    transcript_char_limit: int = 200_000,
    pdf_char_limit: int = 120_000,
) -> str:
    """
    The per-course tail of the prompt. Must follow build_static_prefix().

    Content only: the course's existing platform metadata (title, description,
    keywords, instructions, organisation, declared competencies) is deliberately
    NOT passed, so the model cannot echo it back. Title, description and duration
    are copied from the portal in code instead (see field_config.json).
    """
    duration = (
        str(int(round(float(duration_seconds)))) if duration_seconds else "null"
    )
    return f"""
=================
THIS COURSE
=================

[evidence_tier]
{evidence_tier}

[output_language]
{output_language or "English"}

[duration_seconds]
{duration}

[designation_candidates] (shortlist from the official iGOT master - Targetroles MUST come from here,
copying DesignationId and Name verbatim; anything else is rejected downstream)
{json.dumps(designation_candidates, indent=2, ensure_ascii=False)}

[transcript_text]
<BeginTranscript>
{_truncate_transcript(transcript, transcript_char_limit)}
<EndTranscript>

[pdf_summaries]
<BeginPdfSummaries>
{_truncate(pdf_summaries, pdf_char_limit, "PDF summaries")}
<EndPdfSummaries>

──────────────────────────────────────────────
TASK
──────────────────────────────────────────────
Using only the above inputs, produce a single JSON object conforming to the response schema
({SCHEMA_VERSION}). Produce only the fields in the response schema.
Decide the primary competency area first, then populate only the branches that area permits.
Write CourseSummary and TargetResponsibilitiesFulfilled before choosing Targetroles, and judge each
designation against those responsibilities.
Rubric sub-scores are on a 0-100 scale, not 0-10.
Populate `explain` fully - it is the audit record and must not be empty.
"""


def build_prompt(
    kcm_json: Any,
    sgos_json: Any,
    sto_json: Any = None,
    **course_section_kwargs: Any,
) -> str:
    """Full single-string prompt (static prefix + course section)."""
    return build_static_prefix(kcm_json, sgos_json, sto_json) + build_course_section(
        **course_section_kwargs
    )
