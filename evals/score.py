"""ITBench-AA scoring: precision at full recall over root-cause entities.

Follows Artificial Analysis' published grading rules (methodology page, ITBench-AA section), with their LLM
normalization step replaced by deterministic rules that encode the same instructions:

  - Submissions are `contributing_factors` with `name: "namespace/Kind/name"`.
  - A submitted entity matches a ground-truth group when namespace agrees, the kind is compatible, and the name
    full-matches one of the group's `filter` regexes (or, with no filter, equals the group id minus a "-<n>" suffix).
  - Kind compatibility: same kind; Deployment ≡ Pod when the workload name corresponds; any chaos-mesh kind
    (Schedule, NetworkChaos, StressChaos, ...) ≡ any other chaos-mesh kind when name and namespace correspond,
    including the objects a Schedule spawns ("<schedule name>-<5 chars>").
  - Alias groups are merged into scoring groups; a scoring group is a root-cause target if any member is a root cause.
    Several predictions in the same scoring group count once.
  - Score per run: 0.0 if any root-cause scoring group is missed, else TP / (TP + FP), where unmatched predictions
    and (distinct) non-root scoring groups count as false positives.

Being rule-based it can be stricter than an LLM judge on unusual spellings; every setup we compare uses it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

CHAOS_KINDS = {"schedule", "networkchaos", "stresschaos", "httpchaos", "jvmchaos", "podchaos", "iochaos",
               "timechaos", "dnschaos", "kernelchaos", "workflow"}
CHAOS_SPAWN = re.compile(r"-[a-z0-9]{5}$")  # chaos objects spawned by a Schedule
POD_SUFFIX = re.compile(r"-[a-z0-9]{6,10}-[a-z0-9]{5}$")  # deployment pods: <workload>-<rs hash>-<pod hash>
FAKE_POD = "-5d8f7c9b6d-abcde"


def parse_entity(e: dict | str) -> dict:
    """Accept {"name": "ns/Kind/name"}, a bare "ns/Kind/name" string, or {"kind","name","namespace"}."""
    if isinstance(e, str):
        e = {"name": e}
    name = str(e.get("name", "")).strip()
    parts = name.split("/")
    if len(parts) == 3 and not e.get("kind"):
        ns, kind, nm = parts
        return {"namespace": None if ns == "cluster" else ns, "kind": kind, "name": nm}
    return {"namespace": e.get("namespace"), "kind": str(e.get("kind", "")), "name": name}


@dataclass
class Group:
    id: str
    kind: str
    namespace: str | None
    filters: list[str]
    root_cause: bool

    def _name_ok(self, name: str) -> bool:
        if self.filters:
            return any(re.fullmatch(f, name) for f in self.filters)
        return name == re.sub(r"-\d+$", "", self.id)

    def matches(self, e: dict) -> bool:
        ns = e.get("namespace")
        if ns and self.namespace and ns != self.namespace:
            return False
        sk, gk, name = e["kind"].lower(), self.kind.lower(), e["name"]
        if sk == gk or (sk in CHAOS_KINDS and gk in CHAOS_KINDS):
            # a Schedule spawns chaos objects named "<schedule>-<5 chars>": either name matches
            return self._name_ok(name) or (gk in CHAOS_KINDS and self._name_ok(CHAOS_SPAWN.sub("", name)))
        if sk == "deployment" and gk == "pod":  # a deployment matches its pods
            return self._name_ok(name + FAKE_POD)
        if sk == "pod" and gk == "deployment":
            return self._name_ok(POD_SUFFIX.sub("", name))
        return False


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

    def scoring_groups(self) -> dict[str, str]:
        """group id → id of its merged scoring group (union of overlapping alias sets)."""
        parent = {g.id: g.id for g in self.groups}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                x = parent[x]
            return x

        for alias in self.aliases:
            ids = sorted(alias)
            for other in ids[1:]:
                parent[find(other)] = find(ids[0])
        return {gid: find(gid) for gid in parent}

    def root_targets(self) -> set[str]:
        sg = self.scoring_groups()
        return {sg[g.id] for g in self.groups if g.root_cause}


@dataclass
class Score:
    score: float
    recall_ok: bool
    precision: float
    tp: int
    fp: int
    missed: list[str]


def score(truth: Truth, submission: list[dict | str]) -> Score:
    sg, roots = truth.scoring_groups(), truth.root_targets()
    hit_groups: list[str] = []
    unmatched = 0
    for raw in submission:
        e = parse_entity(raw)
        matched = [g for g in truth.groups if g.matches(e)]
        if not matched:
            unmatched += 1
            continue
        targets = {sg[g.id] for g in matched}
        hit_groups.append(sorted(targets & roots)[0] if targets & roots else sorted(targets)[0])
    distinct = set(hit_groups)
    tp = len(distinct & roots)
    fp = len(distinct - roots) + unmatched
    missed = sorted(roots - distinct)
    precision = tp / (tp + fp) if tp + fp else 0.0
    return Score(0.0 if missed else precision, not missed, precision, tp, fp, missed)
