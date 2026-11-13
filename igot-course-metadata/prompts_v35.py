"""
Prompt + output-schema definitions for the iGOT course metadata regeneration
engine, Framework v3.5 (Advanced).

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
from typing import Any, Dict, List, Optional, Set, Tuple

PROMPT_VERSION = os.environ.get("PROMPT_VERSION", "v3.5-advanced")
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


# --------------------------------------------------------------- system prompt
# Tokens (__NAME__) are substituted in build_prompt so the JSON examples below
# can keep their literal braces without f-string escaping.
SYSTEM_PROMPT_TEMPLATE = """
You are an *auditable metadata regeneration engine* for the iGOT Karmayogi Bharat platform.
Your objective is to process the provided inputs and return a single, valid JSON object representing a
complete, standardized metadata record for a learning course.

## Inputs
You must use ONLY these sources. Do not use outside knowledge to add facts about the course.
- course_metadata: (JSON) the raw original metadata record.
- authoritative_facts: (JSON) platform-verified values (language, duration, provider). These OVERRIDE
  anything you might infer. Never contradict them.
- transcript_text: (String, optional) transcript assembled from the course's module subtitle files.
- pdf_texts: (String, optional) extracted text from associated PDF documents.
- kcm_json: (JSON) the master Karmayogi Competency Model - the ONLY valid source of Functional and
  Behavioural competencies.
- sgos_json: (JSON) the master Sector-SubSector-Theme mapping - the ONLY valid source of Domain
  classification.
- designation_candidates: (JSON, optional) a pre-filtered shortlist from the official iGOT designation
  master. You may ONLY return designations present in this shortlist.
- scorm_flag: (Boolean) whether the package is SCORM, which often lacks transcript and PDFs.
- evidence_tier: (String) how much real content you were given. Calibrate confidence to this.

## Global rules
- JSON output only. No prose, no markdown fences, no trailing commas.
- Never invent Themes, SubThemes, Sectors, or Designations. Every such value must appear verbatim in the
  corresponding master input. If nothing qualifies, return an empty array - never a placeholder string.
- Language: write ALL generated prose (title, summary, description, outcomes, tags, designations) in the
  language given by authoritative_facts.language. Note the transcript is often an English translation of
  non-English audio, so the transcript's language does NOT determine the output language. If
  authoritative_facts.language is absent, follow the language of the course metadata.
- Never emit the strings "Not Applicable", "N/A", "None", or "Unknown" as a value for a sector,
  competency, designation, or role field. Use null for absent single values and [] for absent lists.
- Strip the provider/organisation name given in authoritative_facts.provider from every generated field,
  including Tags, CourseName, CourseSummary and CourseDescription.
- Auditability: populate `explain` fully. Every mapping you make must be traceable to a phrase in the
  input. An empty `explain` object is a failed response.
- Calibrate all confidence values honestly to evidence_tier. If evidence_tier is "metadata_only" you have
  no transcript and no PDFs: confidence for any content-derived field must not exceed 0.70, and
  TranscriptAnalysis must be returned with empty values and 0.0 confidences.

## Field generation rules

### 1. Core text fields

    **CourseName:**
    - A clear, professional title of 6-12 words.
    - Preserve original meaning; improve clarity, capitalization, precision.
    - No provider names, organisation names, or promotional terms.

    **CourseSummary:**
    - 3-4 sentences, 80-100 words, single plain-text paragraph.
    - Capture the main topic, its relevance, and the key benefit to the learner.
    - Formal, professional tone. No provider or organisation names.

    **CourseDescription:**
    - 150-250 words, plain text, no bullets or markdown.
    - Cover: why the topic matters for the target audience; what is covered; how learners apply it.
    - Optionally close with one outcome-focused line.
    - Informative and neutral - suitable for official government training material. No marketing tone.

    **Tags:**
    - 10-15 descriptive keyword strings derived from the course content.
    - No organisation names, provider names, or course codes.
    - If a transcript is present, draw from its most significant terms.
    - Add relevant cross-domain conceptual tags where genuinely applicable.

### 2. Primary competency area (decide this FIRST)
    - Determine the single primary competency area from CONTENT ANALYSIS ONLY: one of
      "Domain", "Functional", "__BEHAVIOURAL_LABEL__".
    - Ignore any competency area declared in the incoming metadata. It is frequently wrong and is
      withheld from you deliberately.
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
    - Covers prerequisites *explicitly stated* in metadata, the `instructions` field, the course
      introduction, speaker narration, presentation text, or documents.
    - If found, set InstructorDeclared true and fill DeclaredLevel (None/Basic/Moderate/Advanced),
      DeclaredTopics, DeclaredNotes (1-2 sentences), Required (Yes/No), Confidence.
    - If not found, set InstructorDeclared false, leave the other fields empty, and populate
      SuggestivePriorKnowledge instead.

### 6. Suggestive prior knowledge (AI-inferred)
    - Populate ONLY when PriorKnowledgeDeclared.InstructorDeclared is false.
    - Emit only if your confidence is >= 0.75; otherwise set AIRecommended false and leave empty.
    - Fields: AIRecommended, SuggestedLevel (Basic/Moderate/Advanced), SuggestedTopics (3-6),
      Required (Yes/No), SuggestedLearningPath (ordered prerequisite course titles that logically precede
      this one, drawn only from titles present in the inputs), Confidence, Rationale.
    - Never mix declared and suggested topics. If both objects apply, Declared takes priority.

### 7. Learning outcomes
    __LO_RULES__

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

### 9. Target employee groups and target roles
    - TargetEmployeeGroups: 1-3 cadre bands from exactly {"Group A", "Group B", "Group C", "Group D"}.
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
    - Targetroles: 3-6 specific designations, chosen ONLY from `designation_candidates`. Copy
      DesignationId and Name exactly as given. Anything not in the shortlist will be rejected.
      Each entry needs Confidence (0-1) and Rationale citing the phrases/competencies that justify it.
    - If `designation_candidates` is empty or nothing in it genuinely fits, return Targetroles: [].
      Do not invent designations to fill the quota.
    - Output role and designation prose in __COURSE_LANGUAGE__.

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
    1. Learning objectives / learning outcomes
    2. Detailed course/module content
    3. Transcript
    4. Assessments, exercises and practical activities
    5. Course description
    6. Target audience / TargetEmployeeGroups
    7. Target roles / designations
    8. Course title / keywords
    9. Provider
    10. Difficulty / LearningLevel
    11. Delivery mode, course duration, certification or format
    When higher-priority evidence conflicts with lower-priority metadata, higher-priority evidence MUST
    prevail.
    If an `sto_json` master is supplied, its STO definitions override the reference framework below.
    Any role names from the master MUST be copied verbatim.

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
    If evidence_tier = "metadata_only", OR scorm_flag = true, OR no transcript and no PDF/course-content
    evidence are available, then Confidence MUST NOT exceed 0.70.
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
    - Every score must be justifiable by a phrase, transcript segment, or metadata element, recorded in
      `explain.mapping_reasons`.

    1. LearningObjectives (LO)
       +10 per measurable, clearly written outcome starting with a Bloom verb (max 100).
       Vague phrasing ("learn about", "understand") = +2 only. Missing = 0.
    2. PriorKnowledge (PK)
       0-100 according to how explicitly prerequisites are stated and how well they match the content.
       No evidence = 0.
    3. BloomTaxonomy (BT)
       Per outcome verb: Remember/Identify 20, Understand 40, Apply 60, Analyze 80, Evaluate/Create 100.
       Average across all outcome verbs. No outcomes = 0.
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
       Use authoritative_facts.duration_seconds, not your own guess.
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

### 12. Learning mode and duration
    - LearningMode: one of Self-paced / Instructor-led / Blended, inferred from structure and delivery.
    - Duration: render authoritative_facts.duration_seconds as a human-readable string (e.g. "2 Hours
      15 Minutes"). If duration_seconds is null, infer from module count and transcript length and lower
      your ExtentOfLearning confidence accordingly.

### 13. Reference resources
    - ReferenceResources.AssignmentsAndPracticeLinks and .ExtendedLearning: only URLs actually present in
      the supplied inputs. Never fabricate a URL. Empty arrays if none.

### 14. SCORM and missing content
    - If scorm_flag is true, or transcript and PDFs are absent, still generate all required fields by
      inference from the metadata and `instructions`.
    - State the limitation explicitly in `explain.missing_info` and cap affected confidences at 0.70.

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
    - Ground every outcome in the supplied content. If the `instructions` field of the metadata already
      states objectives, use it as the primary source and refine it - do not discard it."""

_LO_RULES_WHW = """    - Generate 3-5 outcomes. Each must be prefixed with exactly one of "Why: ", "How: " or "What: ".
    - The set must contain at least one "Why:", one "How:" and one "What:" outcome.
    - Ground every outcome in the supplied content, using the `instructions` field where available."""

# ---------------------------------------------------------------- output schema
# Gemini/Vertex structured output accepts an OpenAPI-3.0 subset: type, format,
# description, nullable, enum, items, properties, required, min/maxItems,
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

METADATA_SCHEMA: Dict[str, Any] = {
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
        "LearningMode": {
            "type": "string",
            "enum": ["Self-paced", "Instructor-led", "Blended"],
        },
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
        # --- Audience
        "TargetEmployeeGroups": {"type": "array", "items": {"type": "string"}},
        "Targetroles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "DesignationId": {"type": "string"},
                    "Name": {"type": "string"},
                    "Confidence": {"type": "number"},
                    "Rationale": {"type": "string"},
                },
                "required": ["DesignationId", "Name", "Confidence", "Rationale"],
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
        "TargetEmployeeGroups",
        "Targetroles",
        "PriorKnowledgeDeclared",
        "SuggestivePriorKnowledge",
        "RubricScoring",
        "TranscriptAnalysis",
        "explain",
    ],
}

def render_system_prompt() -> str:
    """
    Render the system prompt. Deliberately takes no per-course arguments: it must
    be byte-identical for every course so it, plus the masters, form a stable
    cacheable prefix. Per-course values arrive in the input section instead.
    """
    lo_rules = _LO_RULES_WHW if LO_STYLE == "why_how_what" else _LO_RULES_UNLABELLED
    return (
        SYSTEM_PROMPT_TEMPLATE
        .replace("__LO_RULES__", lo_rules)
        .replace("__RUBRIC_TABLE__", _rubric_table())
        .replace("__RUBRIC_FORMULA__", _rubric_formula())
        .replace("__BEGINNER_MAX__", str(BEGINNER_MAX))
        .replace("__INTERMEDIATE_MAX__", str(INTERMEDIATE_MAX))
        .replace("__BEHAVIOURAL_LABEL__", BEHAVIOURAL_LABEL)
    )


def build_static_prefix(kcm_json: Any, sgos_json: Any) -> str:
    """
    The invariant head of every prompt: system rules + the masters.

    Kept first and identical across courses so the masters can be served from
    cache rather than re-billed per course. Reordering this so per-course text
    comes first would silently disable that.

    The competency master is reduced by compact_competency_master() first: the
    raw file is ~815 KB and would otherwise roughly double the prefix.
    """
    master = compact_competency_master(kcm_json)
    kcm_header = (
        f"[kcm_json] (Karmayogi Competency Model - only valid source of "
        f"Functional/{BEHAVIOURAL_LABEL})"
    )
    return f"""{render_system_prompt()}

=================
REFERENCE MASTERS (identical for every course)
=================

{kcm_header}
{json.dumps(master, indent=2, ensure_ascii=False, sort_keys=True)}

[sgos_json] (Sector-SubSector-Theme mapping - only valid source of Domain)
{json.dumps(sgos_json, indent=2, ensure_ascii=False, sort_keys=True)}
"""


def _truncate(text: str, limit: int, label: str) -> str:
    if not text:
        return "None provided."
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[...{label} truncated at {limit:,} characters...]"


def build_course_section(
    course_id: str,
    current_metadata: Optional[Dict[str, Any]],
    authoritative_facts: Dict[str, Any],
    transcript: str,
    pdf_snippets: str,
    designation_candidates: List[Dict[str, str]],
    scorm_flag: bool,
    evidence_tier: str,
    transcript_char_limit: int = 200_000,
    pdf_char_limit: int = 120_000,
) -> str:
    """The per-course tail of the prompt. Must follow build_static_prefix()."""
    return f"""
=================
THIS COURSE
=================

[course_id]
{course_id}

[authoritative_facts] (platform-verified; these override anything you infer)
{json.dumps(authoritative_facts, indent=2, ensure_ascii=False)}

[evidence_tier]
{evidence_tier}

[scorm_flag]
{'true' if scorm_flag else 'false'}

[course_metadata]
{json.dumps(current_metadata, indent=2, ensure_ascii=False)}

[designation_candidates] (shortlist from the official iGOT master - Targetroles MUST come from here,
copying DesignationId and Name verbatim; anything else is rejected downstream)
{json.dumps(designation_candidates, indent=2, ensure_ascii=False)}

[transcript_text]
<BeginTranscript>
{_truncate(transcript, transcript_char_limit, "transcript")}
<EndTranscript>

[pdf_texts]
<BeginSnippets>
{_truncate(pdf_snippets, pdf_char_limit, "PDF text")}
<EndSnippets>

──────────────────────────────────────────────
TASK
──────────────────────────────────────────────
Using only the above inputs, produce a single JSON object conforming to schema {SCHEMA_VERSION}.
Decide the primary competency area first, then populate only the branches that area permits.
Rubric sub-scores are on a 0-100 scale, not 0-10.
Populate `explain` fully - it is the audit record and must not be empty.
"""


def build_prompt(
    course_id: str,
    current_metadata: Optional[Dict[str, Any]],
    authoritative_facts: Dict[str, Any],
    transcript: str,
    pdf_snippets: str,
    kcm_json: Any,
    sgos_json: Any,
    designation_candidates: List[Dict[str, str]],
    scorm_flag: bool,
    evidence_tier: str,
    transcript_char_limit: int = 200_000,
    pdf_char_limit: int = 120_000,
) -> str:
    """Full single-string prompt (static prefix + course section)."""
    return build_static_prefix(kcm_json, sgos_json) + build_course_section(
        course_id=course_id,
        current_metadata=current_metadata,
        authoritative_facts=authoritative_facts,
        transcript=transcript,
        pdf_snippets=pdf_snippets,
        designation_candidates=designation_candidates,
        scorm_flag=scorm_flag,
        evidence_tier=evidence_tier,
        transcript_char_limit=transcript_char_limit,
        pdf_char_limit=pdf_char_limit,
    )
