"""
Pydantic models for the target course-metadata output record.

The shape follows the stakeholder draft record
runs/single/courses/do_11381269163759206411.json: the same keys, key casing and
key order, and nothing else (extra keys are rejected). Values are checked
against the same label sets as prompts.py (v3.7).

Where the draft left a decision open, the choice made here is noted inline:
- LearningMode ("#To check"): validated against prompts.LEARNING_MODES,
  which is configurable until the label set is agreed.
- DomainCompetencies ("check to see if multiple sub-sectors has to be
  included"): a list, so one or more entries are allowed, each with its own
  confidence and rationale as the note asks.
- Prerequisites.Recommended_by (written `instructor/AI`): one of "instructor"
  or "AI".
- FunctionalCompetencies / BehaviouralCompetencies are empty in the draft, so
  their item shape is not shown. ASSUMED here: Theme + SubTheme with the same
  confidence / rationale keys as DomainCompetencies.
- ReferenceResources is empty in the draft ("list of all the references in the
  PDF and audio transcripts"). ASSUMED here: a list of citation strings.

meta_gen.py writes records in the v3.6/v3.7 pipeline shape. from_pipeline_record()
converts one of those into this format (see its docstring for the mapping).

Usage:
    CourseMetadataRecord.model_validate(record_dict)
    from_pipeline_record(pipeline_record_dict)            # convert + validate
    python src/scripts/v3.7/output_models.py --schema                     # print the JSON Schema
    python src/scripts/v3.7/output_models.py record.json                  # validate a record file
    python src/scripts/v3.7/output_models.py --convert in.json out.json   # pipeline record -> this format
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prompts import LEARNING_MODES

Confidence = Annotated[float, Field(ge=0.0, le=1.0, description="0-1")]
NonEmptyStr = Annotated[str, Field(min_length=1)]

EmployeeGroup = Literal["Group A", "Group B", "Group C", "Group D"]
BloomLevel = Literal["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]
CompetencyArea = Literal["Domain", "Functional", "Behavioural"]


class _Strict(BaseModel):
    """Exact keys only: unknown keys are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ sub-objects
class Prerequisites(_Strict):
    Recommended_by: Literal["instructor", "AI"] = Field(
        description='"instructor" if the course states its prerequisites, "AI" if inferred.'
    )
    Confidence: Confidence
    Rationale: NonEmptyStr
    Suggested_topics: List[NonEmptyStr] = Field(default_factory=list)


class PrimaryCompetencyArea(_Strict):
    confidence: Confidence
    name: CompetencyArea
    rationale: NonEmptyStr


class KcmCompetency(_Strict):
    """ASSUMED shape (the draft's KCM lists are empty)."""

    Theme: NonEmptyStr
    SubTheme: NonEmptyStr
    confidence: Confidence
    rationale: NonEmptyStr


class DomainCompetency(_Strict):
    Subsector: NonEmptyStr
    Sector: NonEmptyStr
    confidence: Confidence
    rationale: NonEmptyStr


class STOLevel(_Strict):
    Level: Literal["Strategic", "Tactical", "Operational"]
    Confidence: Confidence
    Rationale: NonEmptyStr
    CounterSignals: List[str] = Field(default_factory=list)
    EvidencePhrases: List[str] = Field(default_factory=list)
    PositiveSignalsMatched: List[str] = Field(default_factory=list)


class PriorKnowledgeDeclared(_Strict):
    InstructorDeclared: bool


class BloomTaxonomyEntry(_Strict):
    Level: BloomLevel
    Confidence: Confidence
    Rationale: NonEmptyStr


class Designation(_Strict):
    Confidence: Confidence
    DesignationId: NonEmptyStr
    Name: NonEmptyStr
    Rationale: NonEmptyStr


# ---------------------------------------------------------------- the record
class CourseMetadataRecord(_Strict):
    """One course's metadata record, in the stakeholder draft's format."""

    Do_ID: NonEmptyStr
    Duration: NonEmptyStr
    GeneratedOn: datetime
    Generator: NonEmptyStr
    GeneratorVersion: NonEmptyStr
    GeneratorModel: NonEmptyStr
    LearningLevel: Literal["Beginner", "Intermediate", "Advanced"]
    LearningMode: NonEmptyStr = Field(
        description="One of prompts.LEARNING_MODES (label set still open)."
    )
    AuthorName: Optional[str] = None
    CourseSummary: NonEmptyStr
    Prerequisites: Prerequisites
    PrimaryCompetencyArea: PrimaryCompetencyArea
    BehaviouralCompetencies: List[KcmCompetency] = Field(default_factory=list)
    FunctionalCompetencies: List[KcmCompetency] = Field(default_factory=list)
    DomainCompetencies: List[DomainCompetency] = Field(default_factory=list)
    STOLevel: STOLevel
    PriorKnowledgeDeclared: PriorKnowledgeDeclared
    ReferenceResources: List[NonEmptyStr] = Field(
        default_factory=list,
        description="ASSUMED: citation strings for references in the PDFs and transcripts.",
    )
    BloomTaxonomy: List[BloomTaxonomyEntry] = Field(default_factory=list)
    TargetEmployeeGroups: List[EmployeeGroup] = Field(min_length=1, max_length=4)
    Targetroles: List[Designation] = Field(default_factory=list)
    Targetrolesexclusions: List[Designation] = Field(default_factory=list)

    @field_validator("LearningMode")
    @classmethod
    def _known_learning_mode(cls, value: str) -> str:
        if value not in LEARNING_MODES:
            raise ValueError(f"must be one of {LEARNING_MODES}")
        return value

    @field_validator("TargetEmployeeGroups")
    @classmethod
    def _unique_groups(cls, value: List[str]) -> List[str]:
        if len(set(value)) != len(value):
            raise ValueError("groups must not repeat")
        return value

    @field_validator("BloomTaxonomy")
    @classmethod
    def _unique_bloom_levels(cls, value: List[BloomTaxonomyEntry]) -> List[BloomTaxonomyEntry]:
        levels = [entry.Level for entry in value]
        if len(set(levels)) != len(levels):
            raise ValueError("each Bloom level may appear once")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "CourseMetadataRecord":
        # Same branch rule as the prompt and validation.py: only the primary
        # area's competency list may be filled.
        area = self.PrimaryCompetencyArea.name
        filled = {
            "Domain": bool(self.DomainCompetencies),
            "Functional": bool(self.FunctionalCompetencies),
            "Behavioural": bool(self.BehaviouralCompetencies),
        }
        wrong = [name for name, has in filled.items() if has and name != area]
        if wrong:
            raise ValueError(
                f"PrimaryCompetencyArea is {area}, but {', '.join(wrong)}Competencies is not empty"
            )
        # A designation is either applicable or excluded, never both.
        overlap = {d.DesignationId for d in self.Targetroles} & {
            d.DesignationId for d in self.Targetrolesexclusions
        }
        if overlap:
            raise ValueError(f"in both Targetroles and Targetrolesexclusions: {sorted(overlap)}")
        # Recommended_by and InstructorDeclared say the same thing twice.
        declared = self.Prerequisites.Recommended_by == "instructor"
        if declared != self.PriorKnowledgeDeclared.InstructorDeclared:
            raise ValueError(
                "Prerequisites.Recommended_by and PriorKnowledgeDeclared.InstructorDeclared disagree"
            )
        return self


def json_schema() -> dict:
    """JSON Schema of the record, e.g. for documentation or other consumers."""
    return CourseMetadataRecord.model_json_schema()


# ------------------------------------------------- pipeline record -> this format
def _conf(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _key(*parts: Any) -> tuple:
    return tuple(" ".join(str(p or "").lower().split()) for p in parts)


def _triggers(rec: Dict[str, Any]) -> Dict[tuple, Dict[str, Any]]:
    out = {}
    for t in (rec.get("explain") or {}).get("competency_mapping_triggers") or []:
        if isinstance(t, dict):
            out[_key(t.get("Category"), t.get("Theme"), t.get("SubTheme"))] = t
    return out


def _kcm_items(rec: Dict[str, Any], category: str, key: str) -> List[Dict[str, Any]]:
    triggers = _triggers(rec)
    items = []
    for pair in rec.get(key) or []:
        if not isinstance(pair, dict):
            continue
        t = triggers.get(_key(category, pair.get("Theme"), pair.get("SubTheme"))) or {}
        phrases = [p for p in t.get("TriggerPhrases") or [] if p]
        items.append({
            "Theme": pair.get("Theme"),
            "SubTheme": pair.get("SubTheme"),
            # Main-array pairs are >= 0.85 by the prompt rule; that floor is used
            # only when the model recorded no trigger for the pair.
            "confidence": _conf(t.get("Confidence"), 0.85),
            "rationale": (
                "Trigger phrases: " + "; ".join(phrases) if phrases
                else "No trigger phrases recorded by the model for this pair."
            ),
        })
    return items


def _prerequisites(rec: Dict[str, Any]) -> Dict[str, Any]:
    declared = rec.get("PriorKnowledgeDeclared") or {}
    suggested = rec.get("SuggestivePriorKnowledge") or {}
    if declared.get("InstructorDeclared"):
        return {
            "Recommended_by": "instructor",
            "Confidence": _conf(declared.get("Confidence")),
            "Rationale": declared.get("DeclaredNotes") or "Prerequisites stated in the course content.",
            "Suggested_topics": list(declared.get("DeclaredTopics") or []),
        }
    return {
        "Recommended_by": "AI",
        "Confidence": _conf(suggested.get("Confidence")),
        "Rationale": suggested.get("Rationale")
        or "No prerequisites stated, and none inferred with enough confidence.",
        "Suggested_topics": list(suggested.get("SuggestedTopics") or []),
    }


def _domain_items(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not (rec.get("Sector") and rec.get("SubSector")):
        return []
    area = rec.get("PrimaryCompetencyArea") or {}
    t = _triggers(rec).get(_key("Domain", rec.get("SubSector"), rec.get("SubSectorTheme"))) or {}
    return [{
        "Subsector": rec["SubSector"],
        "Sector": rec["Sector"],
        "confidence": _conf(t.get("Confidence"), _conf(area.get("confidence"))),
        "rationale": (rec.get("explain") or {}).get("sgos_reason") or area.get("reason") or "",
    }]


# The code-extracted URL lists also hold the course's own videos and files on
# the iGOT content store. Those are the course itself, not references.
_OWN_CONTENT_MARKERS = ("/content-store/",)


def _references(rec: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for r in rec.get("ReferencesCited") or []:
        if isinstance(r, dict) and r.get("Citation"):
            citation, url = r["Citation"], r.get("URL")
            refs.append(f"{citation} ({url})" if url and url not in citation else citation)
    urls = rec.get("ReferenceResources") or {}
    if isinstance(urls, dict):
        for key in ("AssignmentsAndPracticeLinks", "ExtendedLearning"):
            for url in urls.get(key) or []:
                if not url or any(m in url for m in _OWN_CONTENT_MARKERS):
                    continue
                if not any(url in ref for ref in refs):
                    refs.append(url)
    return list(dict.fromkeys(refs))


def _designations(items: Any) -> List[Dict[str, Any]]:
    out = []
    for d in items or []:
        if isinstance(d, dict):
            out.append({
                "Confidence": _conf(d.get("Confidence")),
                "DesignationId": d.get("DesignationId"),
                "Name": d.get("Name"),
                "Rationale": d.get("Rationale"),
            })
    return out


def from_pipeline_record(
    rec: Dict[str, Any], generator_model: Optional[str] = None
) -> CourseMetadataRecord:
    """
    Convert one meta_gen.py record (v3.7 shape; v3.6 works but has no
    AuthorName / BloomTaxonomy / Targetrolesexclusions / ReferencesCited) into
    the stakeholder format, then validate it.

    Mapping, where it is not a straight copy:
    - GeneratorVersion <- Version. GeneratorModel <- `generator_model` or the
      META_GEN_MODEL env var (the record itself does not store the model).
    - Prerequisites <- PriorKnowledgeDeclared when the instructor declared
      them (Recommended_by "instructor"), else SuggestivePriorKnowledge ("AI").
    - PrimaryCompetencyArea.rationale <- .reason.
    - Functional/BehaviouralCompetencies: confidence and rationale come from
      explain.competency_mapping_triggers for the same pair; with no trigger
      the confidence is the prompt's 0.85 main-array floor and the rationale
      says no trigger was recorded.
    - DomainCompetencies <- Sector + SubSector, rationale explain.sgos_reason.
    - ReferenceResources <- ReferencesCited citations, then the code-extracted
      URLs not already listed.
    - Targetrolesexclusions: any designation also in Targetroles is dropped.
    Dropped because the format has no place for them: TargetResponsibilitiesFulfilled,
    Tags, SubSectorTheme, rubric scores, TranscriptAnalysis, explain.
    """
    area = rec.get("PrimaryCompetencyArea") or {}
    roles = _designations(rec.get("Targetroles"))
    role_ids = {d["DesignationId"] for d in roles}
    seen_bloom: set = set()
    bloom = []
    for b in rec.get("BloomTaxonomy") or []:
        if isinstance(b, dict) and b.get("Level") not in seen_bloom:
            seen_bloom.add(b.get("Level"))
            bloom.append({
                "Level": b.get("Level"),
                "Confidence": _conf(b.get("Confidence")),
                "Rationale": b.get("Rationale"),
            })
    sto = rec.get("STOLevel") or {}
    out = {
        "Do_ID": rec.get("Do_ID"),
        "Duration": rec.get("Duration"),
        "GeneratedOn": rec.get("GeneratedOn"),
        "Generator": rec.get("Generator"),
        "GeneratorVersion": rec.get("Version"),
        "GeneratorModel": generator_model or os.environ.get("META_GEN_MODEL") or "unknown",
        "LearningLevel": rec.get("LearningLevel"),
        "LearningMode": rec.get("LearningMode"),
        "AuthorName": rec.get("AuthorName"),
        "CourseSummary": rec.get("CourseSummary"),
        "Prerequisites": _prerequisites(rec),
        "PrimaryCompetencyArea": {
            "confidence": _conf(area.get("confidence")),
            "name": area.get("name"),
            "rationale": area.get("reason") or area.get("rationale"),
        },
        "BehaviouralCompetencies": _kcm_items(rec, "Behavioural", "BehaviouralCompetencies"),
        "FunctionalCompetencies": _kcm_items(rec, "Functional", "FunctionalCompetencies"),
        "DomainCompetencies": _domain_items(rec),
        "STOLevel": {
            "Level": sto.get("Level"),
            "Confidence": _conf(sto.get("Confidence")),
            "Rationale": sto.get("Rationale"),
            "CounterSignals": list(sto.get("CounterSignals") or []),
            "EvidencePhrases": list(sto.get("EvidencePhrases") or []),
            "PositiveSignalsMatched": list(sto.get("PositiveSignalsMatched") or []),
        },
        "PriorKnowledgeDeclared": {
            "InstructorDeclared": bool(
                (rec.get("PriorKnowledgeDeclared") or {}).get("InstructorDeclared")
            )
        },
        "ReferenceResources": _references(rec),
        "BloomTaxonomy": bloom,
        "TargetEmployeeGroups": list(dict.fromkeys(rec.get("TargetEmployeeGroups") or [])),
        "Targetroles": roles,
        "Targetrolesexclusions": [
            d for d in _designations(rec.get("Targetrolesexclusions"))
            if d["DesignationId"] not in role_ids
        ],
    }
    return CourseMetadataRecord.model_validate(out)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--convert":
        with open(sys.argv[2], encoding="utf-8") as fh:
            converted = from_pipeline_record(json.load(fh))
        with open(sys.argv[3], "w", encoding="utf-8") as fh:
            fh.write(converted.model_dump_json(indent=2))
        print(f"OK: {converted.Do_ID} -> {sys.argv[3]}")
    elif len(sys.argv) == 2 and sys.argv[1] == "--schema":
        print(json.dumps(json_schema(), indent=2))
    elif len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as fh:
            record = CourseMetadataRecord.model_validate(json.load(fh))
        print(f"OK: {record.Do_ID}")
    else:
        sys.exit(
            "usage: python src/scripts/v3.7/output_models.py --schema | <record.json> | --convert <in.json> <out.json>"
        )
