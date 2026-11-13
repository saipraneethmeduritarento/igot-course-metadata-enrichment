"""
Prompt + output-schema definitions for the iGOT course metadata regeneration
engine, v3.7. Same template, single call and public API as
src/scripts/v3.6/prompts.py (a copy of it with the changes below). meta_gen.py
and validation.py in this folder import it as `prompts`.

The prompt TEXT is in src/prompts/v3.7.yaml; this module holds the response
schema, the rubric/label constants and the code that fills the text in.

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
  being sent to the model). They are preserved in src/prompts/v3.5.yaml.

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

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


# --------------------------------------------------------------- prompt text
# All prompt text lives in src/prompts/v3.7.yaml; this module only fills it in.
# Tokens (__NAME__) are substituted below so the JSON examples in the text can
# keep their literal braces.
PROMPTS_FILE = Path(__file__).resolve().parents[2] / "prompts" / "v3.7.yaml"
with open(PROMPTS_FILE, encoding="utf-8") as _fh:
    PROMPTS: Dict[str, Any] = yaml.safe_load(_fh)

_TOKEN = re.compile(r"__([A-Z][A-Z0-9_]*)__")


def fill(template: str, values: Dict[str, str]) -> str:
    """
    Substitute __NAME__ tokens in one pass. Inserted text (transcripts, masters)
    is not re-scanned, and tokens not in `values` are left as they are.
    """
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), template)


SYSTEM_PROMPT_TEMPLATE: str = PROMPTS["system_prompt"]


_LO_RULES_UNLABELLED: str = PROMPTS["learning_outcome_rules"]["unlabelled"]

_LO_RULES_WHW: str = PROMPTS["learning_outcome_rules"]["why_how_what"]

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
    sto_block = ""
    if sto_json:
        sto_block = fill(PROMPTS["sto_block"], {
            "STO_JSON": json.dumps(
                compact_sto_master(sto_json), indent=1, ensure_ascii=False, sort_keys=True
            ),
        })
    return fill(PROMPTS["static_prefix"], {
        "SYSTEM_PROMPT": render_system_prompt(),
        "KCM_HEADER": fill(PROMPTS["kcm_header"], {"BEHAVIOURAL_LABEL": BEHAVIOURAL_LABEL}),
        "KCM_JSON": json.dumps(master, indent=2, ensure_ascii=False, sort_keys=True),
        "SGOS_JSON": json.dumps(sgos_json, indent=2, ensure_ascii=False, sort_keys=True),
        "STO_BLOCK": sto_block,
    })


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
    return fill(PROMPTS["course_section"], {
        "EVIDENCE_TIER": str(evidence_tier),
        "OUTPUT_LANGUAGE": output_language or "English",
        "DURATION_SECONDS": duration,
        "DESIGNATION_CANDIDATES": json.dumps(designation_candidates, indent=2, ensure_ascii=False),
        "TRANSCRIPT": _truncate_transcript(transcript, transcript_char_limit),
        "PDF_SUMMARIES": _truncate(pdf_summaries, pdf_char_limit, "PDF summaries"),
        "SCHEMA_VERSION": SCHEMA_VERSION,
    })


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
