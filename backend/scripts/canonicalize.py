"""Pass 2: canonicalize the startup graph + derive cross-startup edges.

No two founders describe the same problem the same way. After ingest_pitch.py
has loaded every pitch, this script:

  1. For each label (Problem, Technology, Industry, CustomerSegment), asks
     OpenAI to cluster near-duplicate nodes ("AI scribe for vets" ==
     "clinical documentation for veterinarians") and merges each cluster into
     one canonical node, repointing all edges. Merged names are kept in an
     `aliases` property so nothing is lost.
  2. Derives the demo-money edges:
       (a:Startup)-[:COMPETES_WITH {via}]->(b)      shared canonical Problem
       (a:Startup)-[:SHARES_STACK_WITH {via}]->(b)  shared Technology

Idempotent — safe to re-run after adding more pitches.

Run (from backend/):  uv run python scripts/canonicalize.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("canonicalize")

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.context_graph_client import connect_neo4j, close_neo4j, execute_cypher  # noqa: E402


# Per-label: which incoming relationship types need repointing on merge.
# (Segment)-[:MENTIONS]-> can point at any of them.
LABELS = {
    "Problem":         {"in_rels": ["SOLVES", "MENTIONS"], "text_prop": "statement"},
    "Technology":      {"in_rels": ["USES", "MENTIONS"], "text_prop": "name"},
    "Industry":        {"in_rels": ["IN_INDUSTRY", "MENTIONS"], "text_prop": "name"},
    "CustomerSegment": {"in_rels": ["TARGETS", "MENTIONS"], "text_prop": "name"},
}

CLUSTER_SYSTEM = (
    "You deduplicate concept names extracted from different startup pitch videos.\n"
    "Group items that refer to the SAME underlying {label} even when worded "
    "differently. Only group true duplicates/near-duplicates — do NOT group items "
    "that are merely related (e.g. 'Healthcare' and 'Dental Care' stay separate; "
    "'AI'/'Artificial Intelligence'/'Machine Learning models' merge). Singleton "
    "items need no cluster. For each cluster, write a clear canonical name."
)


class Cluster(BaseModel):
    canonical_name: str = Field(description="Best canonical name for the cluster")
    member_keys: list[str] = Field(description="The `key` values of ALL items in this cluster (2+)")


class ClusterResult(BaseModel):
    clusters: list[Cluster]


def _norm_key(name: str) -> str:
    return " ".join(name.strip().lower().split())


def cluster_with_openai(label: str, items: list[dict]) -> list[dict]:
    """One OpenAI call: cluster near-duplicate nodes of a label."""
    from openai import OpenAI

    listing = "\n".join(f"- key: {i['key']!r}  text: {i['text']!r}" for i in items)
    client = OpenAI(api_key=settings.openai_api_key or None)
    response = client.responses.parse(
        model=settings.openai_extraction_model,
        reasoning={"effort": settings.openai_reasoning_effort},
        input=[
            {"role": "system", "content": CLUSTER_SYSTEM.replace("{label}", label)},
            {"role": "user", "content": f"{label} nodes extracted from pitch videos:\n\n{listing}"},
        ],
        text_format=ClusterResult,
    )
    if response.output_parsed is None:
        return []
    return [c.model_dump() for c in response.output_parsed.clusters]


async def merge_cluster(label: str, cluster: dict, valid_keys: set[str]) -> None:
    """Merge a cluster's nodes into one canonical node, repointing edges."""
    members = [k for k in cluster.get("member_keys", []) if k in valid_keys]
    if len(members) < 2:
        return
    canonical_name = cluster["canonical_name"].strip()
    canon_key, dup_keys = members[0], members[1:]
    cfg = LABELS[label]

    # Rename the survivor to the canonical name; collect aliases.
    await execute_cypher(
        f"""
        MATCH (c:{label} {{key: $canon}})
        SET c.name = $cname, c.{cfg['text_prop']} = $cname,
            c.aliases = coalesce(c.aliases, []) + $aliases
        """,
        {"canon": canon_key, "cname": canonical_name, "aliases": dup_keys},
        collect=False,
    )
    # Repoint each known incoming rel type from dup -> canonical, then delete dups.
    for rel in cfg["in_rels"]:
        await execute_cypher(
            f"""
            MATCH (n)-[r:{rel}]->(d:{label}) WHERE d.key IN $dups
            MATCH (c:{label} {{key: $canon}})
            MERGE (n)-[:{rel}]->(c)
            DELETE r
            """,
            {"dups": dup_keys, "canon": canon_key}, collect=False,
        )
    await execute_cypher(
        f"MATCH (d:{label}) WHERE d.key IN $dups DETACH DELETE d",
        {"dups": dup_keys}, collect=False,
    )
    log.info("  merged %d %s nodes -> '%s'", len(members), label, canonical_name)


async def canonicalize_label(label: str) -> None:
    cfg = LABELS[label]
    rows = await execute_cypher(
        f"MATCH (n:{label}) RETURN n.key AS key, n.{cfg['text_prop']} AS text ORDER BY key"
    )
    items = [r for r in rows if r.get("key") and r.get("text")]
    if len(items) < 2:
        log.info("%s: %d node(s) — nothing to canonicalize", label, len(items))
        return
    log.info("%s: clustering %d nodes ...", label, len(items))
    clusters = cluster_with_openai(label, items)
    valid = {i["key"] for i in items}
    for c in clusters:
        try:
            await merge_cluster(label, c, valid)
        except Exception as e:
            log.warning("  cluster merge failed (%s): %s", c.get("canonical_name"), e)


SPACE_SYSTEM = (
    "You group startup problem statements into thematic PROBLEM SPACES — broad "
    "market categories, NOT exact duplicates. Two problems share a space when the "
    "startups sell into the same broad market or budget category (e.g. 'manual "
    "medical billing', 'slow insurance claims', and 'early cancer detection' are "
    "all 'Healthcare'; 'bid coordination for contractors' and 'demand forecasting' "
    "are both 'B2B Operations Automation').\n"
    "CONSOLIDATE AGGRESSIVELY: produce roughly one space per 4-6 problems "
    "(20 problems -> ~4-5 spaces, 60 problems -> ~10-13 spaces). "
    "Single-member spaces are a failure — force every problem into the nearest "
    "broad space. Every problem lands in exactly one space. "
    "Space names: short, Title Case, 2-4 words."
)


class Space(BaseModel):
    name: str = Field(description="Short Title Case name of the problem space")
    member_keys: list[str] = Field(description="`key` values of problems in this space")


class SpaceResult(BaseModel):
    spaces: list[Space]


async def build_problem_spaces() -> None:
    """Cluster ALL problems into thematic ProblemSpace hubs (looser than dedup)."""
    from openai import OpenAI

    rows = await execute_cypher(
        "MATCH (p:Problem) RETURN p.key AS key, p.statement AS text ORDER BY key")
    items = [r for r in rows if r.get("key") and r.get("text")]
    if len(items) < 2:
        return
    log.info("ProblemSpace: grouping %d problems ...", len(items))
    listing = "\n".join(f"- key: {i['key']!r}  problem: {i['text']!r}" for i in items)
    client = OpenAI(api_key=settings.openai_api_key or None)
    response = client.responses.parse(
        model=settings.openai_extraction_model,
        reasoning={"effort": "medium"},  # grouping needs more thought than dedup
        input=[
            {"role": "system", "content": SPACE_SYSTEM},
            {"role": "user", "content": f"Problem statements:\n\n{listing}"},
        ],
        text_format=SpaceResult,
    )
    if response.output_parsed is None:
        return
    valid = {i["key"] for i in items}
    await execute_cypher("MATCH (ps:ProblemSpace) DETACH DELETE ps", collect=False)
    for sp in response.output_parsed.spaces:
        members = [k for k in sp.member_keys if k in valid]
        if not members:
            continue
        await execute_cypher(
            """
            MERGE (ps:ProblemSpace {key: $key})
            SET ps.name = $name, ps.domain = $domain
            WITH ps
            MATCH (p:Problem) WHERE p.key IN $members
            MERGE (p)-[:IN_SPACE]->(ps)
            """,
            {"key": _norm_key(sp.name), "name": sp.name.strip(),
             "members": members, "domain": settings.domain_id},
            collect=False,
        )
    n = await execute_cypher("MATCH (ps:ProblemSpace) RETURN count(ps) AS n")
    log.info("ProblemSpace: %s spaces created", n[0]["n"] if n else 0)


async def derive_edges() -> None:
    """Materialize the cross-startup edges the demo queries traverse."""
    # Competitors: same canonical problem (strong) or same problem space (weaker)
    await execute_cypher(
        """
        MATCH (a:Startup)-[:SOLVES]->(p:Problem)<-[:SOLVES]-(b:Startup)
        WHERE a.key < b.key
        MERGE (a)-[c:COMPETES_WITH]->(b)
        SET c.via = p.name, c.strength = 'same-problem'
        """, collect=False,
    )
    await execute_cypher(
        """
        MATCH (a:Startup)-[:SOLVES]->(:Problem)-[:IN_SPACE]->(ps:ProblemSpace)
              <-[:IN_SPACE]-(:Problem)<-[:SOLVES]-(b:Startup)
        WHERE a.key < b.key AND NOT (a)-[:COMPETES_WITH]-(b)
        MERGE (a)-[c:COMPETES_WITH]->(b)
        SET c.via = ps.name, c.strength = 'same-space'
        """, collect=False,
    )
    # Shared technical approach
    await execute_cypher(
        """
        MATCH (a:Startup)-[:USES]->(t:Technology)<-[:USES]-(b:Startup)
        WHERE a.key < b.key
        MERGE (a)-[s:SHARES_STACK_WITH]->(b)
        SET s.via = coalesce(s.via, []) + t.name
        """, collect=False,
    )
    comp = await execute_cypher(
        "MATCH (:Startup)-[c:COMPETES_WITH]->(:Startup) RETURN count(c) AS n")
    stack = await execute_cypher(
        "MATCH (:Startup)-[s:SHARES_STACK_WITH]->(:Startup) RETURN count(s) AS n")
    log.info("Derived edges: %s COMPETES_WITH, %s SHARES_STACK_WITH",
             comp[0]["n"] if comp else 0, stack[0]["n"] if stack else 0)


async def main() -> None:
    await connect_neo4j()
    try:
        # Clear previously derived edges so re-runs reflect the current graph.
        await execute_cypher(
            "MATCH ()-[r:COMPETES_WITH|SHARES_STACK_WITH]->() DELETE r", collect=False)
        for label in LABELS:
            await canonicalize_label(label)
        await build_problem_spaces()
        await derive_edges()
        log.info("Canonicalization complete.")
    finally:
        await close_neo4j()


if __name__ == "__main__":
    asyncio.run(main())
