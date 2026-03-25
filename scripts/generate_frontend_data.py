import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

try:
    from neo4j import GraphDatabase
except ImportError:
    print("ERROR: neo4j not installed. Run: pip install neo4j pyyaml", file=sys.stderr)
    sys.exit(1)


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
JSON_OUTPUT_PATH = ROOT / "frontend" / "data" / "top_roles.json"
JS_OUTPUT_PATH = ROOT / "frontend" / "data" / "top_roles.js"


with open(CONFIG_PATH, encoding="utf-8") as config_file:
    CONFIG = yaml.safe_load(config_file)

NEO4J_URI = CONFIG["neo4j"]["uri"]
NEO4J_USER = CONFIG["neo4j"]["user"]
NEO4J_PASSWORD = CONFIG["neo4j"]["password"]
PROJECT_ID = CONFIG["project_id"]


def clean_text(value: str) -> str:
    return " ".join((value or "").split())


def event_sort_key(event_id: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"ch(\d+)_(\d+)", event_id.strip(), re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2)), event_id
    return sys.maxsize, sys.maxsize, event_id


def fetch_roles() -> list[dict]:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    query = """
    MATCH (c:Character {project_id: $project_id})
    WITH c, COUNT { (c)--() } AS degree
    ORDER BY degree DESC, c.name ASC
    OPTIONAL MATCH (c)-[r:PARTICIPATED_IN]->(e:Event)
    RETURN
      c.name AS name,
      c.title AS title,
      c.aliases AS aliases,
      c.description AS description,
      c.summary AS summary,
      c.personality AS personality,
      c.traits AS traits,
      c.abilities AS abilities,
      degree AS degree,
      collect(
        CASE
          WHEN e IS NULL THEN null
          ELSE {
            event_id: e.event_id,
            event_name: e.title,
            event_status: r.status,
            character_decision: r.decision,
            decision_result: r.result
          }
        END
      ) AS events
    """
    with driver.session() as session:
        rows = list(session.run(query, project_id=PROJECT_ID))
    driver.close()
    return [row.data() for row in rows]


def build_role_payload(raw: dict, placeholder_image: str) -> dict:
    aliases = [clean_text(alias) for alias in (raw.get("aliases") or []) if clean_text(alias)]

    personality_source = raw.get("personality") or raw.get("traits") or []
    personality = [clean_text(item) for item in personality_source if clean_text(item)]

    raw_events = [
        event
        for event in (raw.get("events") or [])
        if isinstance(event, dict) and event.get("event_id")
    ]
    raw_events.sort(key=lambda event: event_sort_key(str(event.get("event_id", ""))))

    event_items = []
    for event in raw_events:
        if not isinstance(event, dict) or not event.get("event_id"):
            continue
        event_items.append(
            {
                "rank": len(event_items) + 1,
                "event_id": clean_text(event.get("event_id", "")),
                "title": clean_text(event.get("event_name", "")) or clean_text(event.get("event_id", "")),
                "status": clean_text(event.get("event_status", "")),
                "decision": clean_text(event.get("character_decision", "")),
                "result": clean_text(event.get("decision_result", "")),
            }
        )

    return {
        "name": clean_text(raw.get("name", "")),
        "title": clean_text(raw.get("title", "")) or clean_text(raw.get("name", "")),
        "aliases": aliases[:3],
        "description": clean_text(raw.get("description", "")),
        "summary": clean_text(raw.get("summary", "")),
        "event_count": int(raw.get("degree", 0)),
        "personality": personality[:12],
        "image": placeholder_image,
        "events": event_items,
    }


def main() -> None:
    placeholder_image = "./assets/sun-wukong-placeholder.jpg"
    raw_roles = fetch_roles()
    roles = [build_role_payload(role, placeholder_image) for role in raw_roles]

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "neo4j: (:Character)-[:PARTICIPATED_IN]->(:Event)",
        "project_id": PROJECT_ID,
        "total_roles": len(roles),
        "roles": roles,
    }

    JSON_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    JSON_OUTPUT_PATH.write_text(json_text, encoding="utf-8")
    JS_OUTPUT_PATH.write_text(f"export const topRolesData = {json_text};\n", encoding="utf-8")


if __name__ == "__main__":
    main()
