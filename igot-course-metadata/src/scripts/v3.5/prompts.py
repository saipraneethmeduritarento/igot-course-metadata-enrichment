"""
Prompt + output-schema definitions for the iGOT course metadata regeneration
engine, Framework v3.5 (Advanced).

meta_gen.py and validation.py in this folder import it as `prompts`. The
prompt TEXT is in src/prompts/v3.5.yaml.

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


# --------------------------------------------------------------- prompt text
# All prompt text lives in src/prompts/v3.5.yaml; this module only fills it in.
# Tokens (__NAME__) are substituted below so the JSON examples in the text can
# keep their literal braces.
PROMPTS_FILE = Path(__file__).resolve().parents[2] / "prompts" / "v3.5.yaml"
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
    return fill(PROMPTS["static_prefix"], {
        "SYSTEM_PROMPT": render_system_prompt(),
        "KCM_HEADER": fill(PROMPTS["kcm_header"], {"BEHAVIOURAL_LABEL": BEHAVIOURAL_LABEL}),
        "KCM_JSON": json.dumps(master, indent=2, ensure_ascii=False, sort_keys=True),
        "SGOS_JSON": json.dumps(sgos_json, indent=2, ensure_ascii=False, sort_keys=True),
    })


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
    return fill(PROMPTS["course_section"], {
        "COURSE_ID": str(course_id),
        "AUTHORITATIVE_FACTS": json.dumps(authoritative_facts, indent=2, ensure_ascii=False),
        "EVIDENCE_TIER": str(evidence_tier),
        "SCORM_FLAG": "true" if scorm_flag else "false",
        "COURSE_METADATA": json.dumps(current_metadata, indent=2, ensure_ascii=False),
        "DESIGNATION_CANDIDATES": json.dumps(designation_candidates, indent=2, ensure_ascii=False),
        "TRANSCRIPT": _truncate(transcript, transcript_char_limit, "transcript"),
        "PDF_TEXTS": _truncate(pdf_snippets, pdf_char_limit, "PDF text"),
        "SCHEMA_VERSION": SCHEMA_VERSION,
    })


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
