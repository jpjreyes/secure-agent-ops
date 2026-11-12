"""ITBench-AA scoring: precision at full recall over root-cause entities.

A submission is a list of entities {"kind", "name", "namespace"?}. Per run:
  - every ground-truth root-cause group must be hit by some submitted entity, else the run scores 0;
  - otherwise the score is precision = true positives / submitted entities.

Matching is deterministic and uses the ground truth's own definitions:
  - an entity hits a group when the kinds are equal (case-insensitive), the namespaces agree (when both are given),
    and the name full-matches one of the group's `filter` regexes (or, with no filter, equals the group id with any
    trailing "-<n>" removed);
  - `aliases` lists groups that name the same thing (e.g. a pod and its service), so hitting an alias of a root-cause
    group counts as hitting that root cause.
Duplicate entities (same kind/namespace/name) are counted once.

Artificial Analysis normalizes entity names with an LLM before matching; this matcher is stricter, so absolute scores
can be slightly lower than theirs. Every setup we compare uses this same scorer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml


@dataclass
class Group:
    id: str
    kind: str
    namespace: str | None
    filters: list[str]
    root_cause: bool

    def matches(self, e: dict) -> bool:
        if str(e.get("kind", "")).lower() != self.kind.lower():
            return False
        ns = e.get("namespace")
        if ns and self.namespace and ns != self.namespace:
            return False
        name = str(e.get("name", "")).strip()
        if self.filters:
            return any(re.fullmatch(f, name) for f in self.filters)
        return name == re.sub(r"-\d+$", "", self.id)


@dataclass
class Truth:
    groups: list[Group]
    aliases: list[set[str]] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, text: str) -> "Truth":
        g = yaml.safe_load(text)
        g = g.get("spec", g)  # some scenarios wrap the ground truth in a Kubernetes-style object
        groups = [Group(x["id"], x["kind"], x.get("namespace"), x.get("filter") or [], bool(x.get("root_cause")))
                  for x in g.get("groups", [])]
        return cls(groups, [set(a) for a in g.get("aliases") or []])

    def roots(self) -> list[Group]:
        return [g for g in self.groups if g.root_cause]

    def credited_root(self, e: dict) -> str | None:
        """The root-cause group id this entity counts for, or None (a false positive)."""
        hit = {g.id for g in self.groups if g.matches(e)}
        for r in self.roots():
            same = {r.id}.union(*[a for a in self.aliases if r.id in a])
            if hit & same:
                return r.id
        return None


@dataclass
class Score:
    score: float
    recall_ok: bool
    precision: float
    tp: int
    fp: int
    missed: list[str]


def score(truth: Truth, submission: list[dict]) -> Score:
    seen, entities = set(), []
    for e in submission:
        key = (str(e.get("kind", "")).lower(), e.get("namespace") or "", str(e.get("name", "")).strip())
        if key not in seen:
            seen.add(key)
            entities.append(e)
    credited = [truth.credited_root(e) for e in entities]
    tp = sum(c is not None for c in credited)
    fp = len(entities) - tp
    missed = [r.id for r in truth.roots() if r.id not in credited]
    precision = tp / len(entities) if entities else 0.0
    return Score(0.0 if missed else precision, not missed, precision, tp, fp, missed)
