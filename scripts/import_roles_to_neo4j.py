#!/usr/bin/env python3
"""
Import roles from roles/ into Neo4j.

Steps:
  1. Read timeline/events.json → build Event nodes
  2. Read all JSON files under roles/
  3. Convert Traditional Chinese -> Simplified Chinese via zhconv
  4. Group same characters with Union-Find (name / alias cross-reference)
  5. Merge grouped roles (deduplicate personality / traits / abilities / events)
  6. Optimize & condense personality / traits / abilities with Qwen API
  7. Write Character nodes into Neo4j (idempotent MERGE)
  8. Write PARTICIPATED_IN relationships with per-character participation
     properties (status, decision, result) on the relationship itself

Graph model:
  (:Character)-[r:PARTICIPATED_IN {status, decision, result}]->(:Event)

Dependencies:
    pip install zhconv pyyaml openai neo4j
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    import zhconv
except ImportError:
    print("ERROR: zhconv not installed.  Run: pip install zhconv", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed.  Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai not installed.  Run: pip install openai", file=sys.stderr)
    sys.exit(1)

try:
    from neo4j import GraphDatabase
except ImportError:
    print("ERROR: neo4j not installed.  Run: pip install neo4j", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration — loaded from config.yaml
# ---------------------------------------------------------------------------
ROLES_DIR = Path(__file__).parent.parent / "roles"
EVENTS_FILE = Path(__file__).parent.parent / "timeline" / "events.json"

_config_path = Path(__file__).parent.parent / "config.yaml"
with open(_config_path, encoding="utf-8") as _fh:
    _config = yaml.safe_load(_fh)

NEO4J_URI = _config["neo4j"]["uri"]
NEO4J_USER = _config["neo4j"]["user"]
NEO4J_PASSWORD = _config["neo4j"]["password"]
PROJECT_ID = _config["project_id"]

QWEN_MODEL = _config["qwen"]["model"]
QWEN_BASE_URL = _config["qwen"]["base_url"]
# Rate-limit guard between Qwen calls (seconds)
QWEN_DELAY = _config["qwen"]["delay"]

client = OpenAI(api_key=_config["api_key"], base_url=QWEN_BASE_URL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sc(value):
    """Recursively convert a value to Simplified Chinese."""
    if isinstance(value, str):
        return zhconv.convert(value, "zh-hans")
    if isinstance(value, list):
        return [sc(v) for v in value]
    if isinstance(value, dict):
        return {k: sc(v) for k, v in value.items()}
    return value


def norm(name: str) -> str:
    """Normalize a character name for comparison."""
    return zhconv.convert(name.strip(), "zh-hans")


# ---------------------------------------------------------------------------
# 1. Read roles
# ---------------------------------------------------------------------------

def read_roles() -> list[dict]:
    roles: list[dict] = []
    for path in sorted(ROLES_DIR.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            data = sc(data)          # convert entire structure to simplified
            data["_source"] = path.stem
            roles.append(data)
        except Exception as exc:
            print(f"  WARN: could not read {path.name}: {exc}", file=sys.stderr)
    return roles


# ---------------------------------------------------------------------------
# 2. Union-Find grouping
# ---------------------------------------------------------------------------

def build_groups(roles: list[dict]) -> list[list[dict]]:
    n = len(roles)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Build lookup tables
    name_idx: dict[str, int] = {}
    alias_idx: dict[str, int] = {}
    for i, r in enumerate(roles):
        name_idx[norm(r["name"])] = i
        for a in r.get("aliases", []):
            alias_idx[norm(a)] = i

    # Merge by cross-reference
    for i, r in enumerate(roles):
        nm = norm(r["name"])
        # This role's name appears as an alias elsewhere
        if nm in alias_idx:
            union(i, alias_idx[nm])
        for a in r.get("aliases", []):
            na = norm(a)
            # An alias matches another role's primary name
            if na in name_idx:
                union(i, name_idx[na])
            # An alias matches another role's alias
            if na in alias_idx and alias_idx[na] != i:
                union(i, alias_idx[na])

    groups: dict[int, list[dict]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(roles[i])

    return list(groups.values())


# ---------------------------------------------------------------------------
# 3. Merge a group into one role dict
# ---------------------------------------------------------------------------

def merge_group(group: list[dict]) -> dict:
    if len(group) == 1:
        r = group[0]
        r.pop("_source", None)
        return r

    # Pick the most descriptive member as the primary
    primary = max(
        group,
        key=lambda r: len(r.get("summary", "")) + len(r.get("description", "")),
    )
    primary_name = norm(primary["name"])

    # Collect all names / aliases
    all_names: set[str] = set()
    for r in group:
        all_names.add(norm(r["name"]))
        for a in r.get("aliases", []):
            all_names.add(norm(a))
    aliases = sorted(all_names - {primary_name})

    # Deduplicated merge for list fields (preserve insertion order)
    def merge_list(field: str) -> list:
        seen: dict[str, None] = {}
        for r in group:
            for item in r.get(field, []):
                seen[item] = None
        return list(seen)

    # Merge descriptions and summaries
    descs = list(
        dict.fromkeys(r.get("description", "") for r in group if r.get("description"))
    )
    summaries = list(
        dict.fromkeys(r.get("summary", "") for r in group if r.get("summary"))
    )

    # Merge events – deduplicate by event_id
    events_map: dict[str, dict] = {}
    for r in group:
        for e in r.get("events", []):
            eid = e.get("event_id", "")
            if eid and eid not in events_map:
                events_map[eid] = e
    events = sorted(events_map.values(), key=lambda e: e.get("event_id", ""))

    return {
        "name": primary_name,
        "title": primary_name,
        "aliases": aliases,
        "description": " / ".join(descs),
        "summary": "\n".join(summaries),
        "personality": merge_list("personality"),
        "traits": merge_list("traits"),
        "abilities": merge_list("abilities"),
        "events": events,
    }


# ---------------------------------------------------------------------------
# 4. Optimize personality / traits / abilities with Claude
# ---------------------------------------------------------------------------

def optimize_fields(role: dict) -> dict:
    personality = role.get("personality", [])
    traits = role.get("traits", [])
    abilities = role.get("abilities", [])

    if not personality and not traits and not abilities:
        return role

    prompt = f"""你是西游记角色分析专家。请对以下角色的三类标签进行精简优化：

角色：{role['name']}
描述：{role.get('description', '')}

规则：
1. 合并意思相同或高度重叠的条目
2. 删除过于细节化的行为描述，保留本质特征
3. 每条保持简洁（2-8字）
4. 输出简体中文
5. personality 聚焦性格/心理特质
6. traits 聚焦外在特征/标志性物品
7. abilities 聚焦技能/法术/能力

**personality** （原 {len(personality)} 条）：
{json.dumps(personality, ensure_ascii=False)}

**traits** （原 {len(traits)} 条）：
{json.dumps(traits, ensure_ascii=False)}

**abilities** （原 {len(abilities)} 条）：
{json.dumps(abilities, ensure_ascii=False)}

请仅返回如下 JSON，不附加任何说明：
{{
  "personality": [...],
  "traits": [...],
  "abilities": [...]
}}"""

    try:
        resp = client.chat.completions.create(
            model=QWEN_MODEL,
            messages=[
                {"role": "system", "content": "你是西游记角色分析专家，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()

        # Strip markdown code fences if present
        if "```" in text:
            parts = text.split("```")
            # Find the JSON block
            for part in parts:
                stripped = part.strip()
                if stripped.startswith("{") or stripped.startswith("json\n{"):
                    text = stripped.removeprefix("json").strip()
                    break

        optimized = json.loads(text)
        role["personality"] = optimized.get("personality", personality)
        role["traits"] = optimized.get("traits", traits)
        role["abilities"] = optimized.get("abilities", abilities)

    except Exception as exc:
        print(f"    WARN: Claude optimization failed for {role['name']}: {exc}", file=sys.stderr)

    return role


# ---------------------------------------------------------------------------
# 5. Read timeline/events.json
# ---------------------------------------------------------------------------

def read_events() -> dict[str, dict]:
    """Return a dict keyed by event_id from timeline/events.json (simplified)."""
    with open(EVENTS_FILE, encoding="utf-8") as fh:
        raw: list[dict] = json.load(fh)
    events: dict[str, dict] = {}
    for e in raw:
        e = sc(e)
        eid = e.get("event_id", "").strip()
        if eid:
            events[eid] = e
    return events


# ---------------------------------------------------------------------------
# 6. Write to Neo4j
# ---------------------------------------------------------------------------

def write_event_nodes(session, events_by_id: dict[str, dict]) -> None:
    """Create/update Event nodes from timeline/events.json data."""
    for eid, e in events_by_id.items():
        time_obj = e.get("time", {}) or {}
        session.run(
            """
            MERGE (ev:Event {event_id: $eid})
            SET ev.sequence         = $sequence,
                ev.title            = $title,
                ev.description      = $description,
                ev.cause            = $cause,
                ev.consequence      = $consequence,
                ev.location         = $location,
                ev.event_type       = $event_type,
                ev.time_expression  = $time_expression,
                ev.time_type        = $time_type,
                ev.global_order     = $global_order
            """,
            eid=eid,
            sequence=e.get("sequence", 0),
            title=e.get("title", ""),
            description=e.get("description", ""),
            cause=e.get("cause", ""),
            consequence=e.get("consequence", ""),
            location=e.get("location", ""),
            event_type=e.get("event_type", ""),
            time_expression=time_obj.get("expression", ""),
            time_type=time_obj.get("type", ""),
            global_order=time_obj.get("global_order", 0),
        )


def write_event_order(session, events_by_id: dict[str, dict]) -> None:
    """
    Build temporal ordering edges between Event nodes using time.anchor_ref.

    (:Event {event_id: X})-[:FOLLOWS]->(:Event {event_id: anchor_ref})

    Meaning: event X is expressed in time relative to (i.e. comes after) its anchor.
    """
    for eid, e in events_by_id.items():
        anchor = (e.get("time") or {}).get("anchor_ref", "")
        if not anchor or anchor not in events_by_id:
            continue
        session.run(
            """
            MATCH (e:Event  {event_id: $eid})
            MATCH (a:Event  {event_id: $anchor})
            MERGE (e)-[:FOLLOWS]->(a)
            """,
            eid=eid,
            anchor=anchor,
        )


def write_character(session, role: dict) -> None:
    session.run(
        """
        MERGE (c:Character {name: $name})
        SET c.title       = $title,
            c.aliases     = $aliases,
            c.description = $description,
            c.summary     = $summary,
            c.personality = $personality,
            c.traits      = $traits,
            c.abilities   = $abilities,
            c.project_id  = $project_id
        """,
        name=role["name"],
        title=role.get("title", role["name"]),
        aliases=role.get("aliases", []),
        description=role.get("description", ""),
        summary=role.get("summary", ""),
        personality=role.get("personality", []),
        traits=role.get("traits", []),
        abilities=role.get("abilities", []),
        project_id=PROJECT_ID,
    )


def write_participations(session, role: dict, events_by_id: dict[str, dict]) -> None:
    """
    For each event entry in the role, create a PARTICIPATED_IN relationship
    from Character -> Event, with participation-specific properties on the edge.

    Neo4j supports relationship properties natively; status / decision / result
    describe *this character's* participation, not the event itself.
    """
    for entry in role.get("events", []):
        eid = entry.get("event_id", "").strip()
        if not eid or eid not in events_by_id:
            continue
        session.run(
            """
            MATCH (c:Character {name: $char_name})
            MATCH (e:Event {event_id: $eid})
            MERGE (c)-[r:PARTICIPATED_IN]->(e)
            SET r.status   = $status,
                r.decision = $decision,
                r.result   = $result
            """,
            char_name=role["name"],
            eid=eid,
            status=entry.get("event_status", ""),
            decision=entry.get("character_decision", ""),
            result=entry.get("decision_result", ""),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print(f"Project ID : {PROJECT_ID}")
    print(f"Roles dir  : {ROLES_DIR}")
    print(f"Events file: {EVENTS_FILE}")
    print(f"Neo4j URI  : {NEO4J_URI}")
    print("=" * 60)

    # -- Read events
    print("\n[1/6] Reading timeline/events.json...")
    events_by_id = read_events()
    print(f"  {len(events_by_id)} events loaded")

    # -- Read roles
    print("\n[2/6] Reading role files...")
    roles = read_roles()
    print(f"  {len(roles)} files loaded")

    # -- Group
    print("\n[3/6] Grouping same characters...")
    groups = build_groups(roles)
    print(f"  {len(roles)} files → {len(groups)} unique characters")

    # -- Connect Neo4j
    print("\n[4/6] Connecting to Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    print("  Connected")

    # -- Write Event nodes first (so PARTICIPATED_IN MATCH succeeds)
    print(f"\n[5/6] Writing {len(events_by_id)} Event nodes + ordering edges...")
    with driver.session() as session:
        write_event_nodes(session, events_by_id)
        write_event_order(session, events_by_id)
    print("  Done")

    # -- Process each character group
    print(f"\n[6/6] Processing {len(groups)} characters...\n")
    for idx, group in enumerate(groups, 1):
        src_names = " + ".join(r["_source"] for r in group)
        print(f"  [{idx:>3}/{len(groups)}] {src_names}")

        merged = merge_group(group)
        p_before = len(merged.get("personality", []))
        t_before = len(merged.get("traits", []))
        a_before = len(merged.get("abilities", []))

        if p_before + t_before + a_before > 0:
            merged = optimize_fields(merged)
            p_after = len(merged.get("personality", []))
            t_after = len(merged.get("traits", []))
            a_after = len(merged.get("abilities", []))
            print(
                f"         personality {p_before}→{p_after}  "
                f"traits {t_before}→{t_after}  "
                f"abilities {a_before}→{a_after}"
            )
            time.sleep(QWEN_DELAY)

        with driver.session() as session:
            write_character(session, merged)
            write_participations(session, merged, events_by_id)

        print(f"         → saved: {merged['name']}")

    driver.close()

    print(f"\nDone — {len(groups)} characters + {len(events_by_id)} events written to Neo4j.")


if __name__ == "__main__":
    main()
