"""Ingest STARTUP PITCH videos into the Startup Pitch Network graph.

Same 4-sponsor pipeline as scripts/ingest.py, but with a pitch-specific
ontology instead of generic Entity/Topic:

  (:Startup)-[:PITCHED_IN]->(:Video)-[:HAS_SEGMENT]->(:Segment)-[:NEXT]->(:Segment)
  (:Founder)-[:FOUNDED]->(:Startup)
  (:Startup)-[:SOLVES]->(:Problem)          <- canonicalize.py merges duplicates
  (:Startup)-[:USES]->(:Technology)
  (:Startup)-[:IN_INDUSTRY]->(:Industry)
  (:Startup)-[:TARGETS]->(:CustomerSegment)
  (:Startup)-[:CLAIMS]->(:Claim {text, kind, approx_sec})   <- provenance / traction
  (:Segment)-[:MENTIONS]->(Startup|Founder|Technology|...)  <- graph-panel drilldown

Run (from backend/):
  uv run python scripts/ingest_pitch.py data/videos/pitches/*.mp4
  uv run python scripts/ingest_pitch.py https://.../pitch.mp4
  uv run python scripts/ingest_pitch.py --index-id=IDX --video-id=VID   # already indexed

Then run:  uv run python scripts/canonicalize.py   (pass 2 — merge + derive edges)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_pitch")

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.context_graph_client import connect_neo4j, close_neo4j, execute_cypher  # noqa: E402
from app.vector_client import ensure_segment_vector_index  # noqa: E402
from app import twelvelabs_client as tl  # noqa: E402


# ---------------------------------------------------------------------------
# Extraction prompts + schema
# ---------------------------------------------------------------------------

PITCH_PEGASUS_PROMPT = (
    "This video is a startup pitch (e.g. a Y Combinator Demo Day presentation). "
    "Analyze it thoroughly and report, in order:\n"
    "1. The startup's NAME (read it off the slides/on-screen text if shown) and "
    "one-line tagline.\n"
    "2. Every founder or presenter: their name (from slides, lower-thirds, or "
    "spoken introduction) and role if stated.\n"
    "3. The PROBLEM the startup says it solves, in the speaker's own words.\n"
    "4. The SOLUTION / product and HOW it works, including any technologies, "
    "platforms, or technical approaches mentioned (AI, ML, marketplace, API, "
    "hardware, biotech, etc.).\n"
    "5. The target CUSTOMERS and the INDUSTRY / market they operate in.\n"
    "6. Every concrete TRACTION or MARKET claim with its approximate timestamp: "
    "revenue, growth rate, users, customers signed, market size, funding ask. "
    "Quote the numbers exactly as spoken or shown on slides.\n"
    "7. A chronological breakdown into segments: start/end seconds, what is "
    "happening, all visible on-screen slide text, and the spoken words.\n"
    "Be precise. Prefer on-screen text for names and numbers — slides are more "
    "reliable than audio."
)

STRUCTURE_SYSTEM = (
    "You convert a startup pitch video analysis into strict structured data.\n"
    "Rules:\n"
    "- startup.name: the company's actual name (Title Case). If genuinely absent, "
    "use the most likely name from on-screen text.\n"
    "- problem_statement: ONE sentence, in plain words, phrased generically enough "
    "to compare across startups (e.g. 'Small clinics waste hours on manual "
    "medical billing'), but faithful to the pitch.\n"
    "- technologies: concrete technical approaches only (e.g. 'Computer Vision', "
    "'Marketplace', 'LLM', 'Robotics', 'API Platform'). Title Case, singular.\n"
    "- industries / customer_segments: short canonical names, Title Case "
    "(e.g. 'Healthcare', 'Small Business Owners').\n"
    "- claims: every traction/market/funding number with kind and the quote.\n"
    "- Use the SAME canonical name for the same real-world thing so nodes merge "
    "across videos.\n"
    "- segments: chronological, non-overlapping, covering the whole video."
)


class FounderInfo(BaseModel):
    name: str
    role: str = ""


class PitchClaim(BaseModel):
    text: str = Field(description="The claim with exact numbers, e.g. '$50k MRR, growing 40% m/m'")
    kind: str = Field(description="one of: traction, revenue, users, market_size, funding, growth, other")
    approx_sec: float = Field(description="approximate timestamp in seconds where this claim is made")


class PitchSegment(BaseModel):
    start_sec: float
    end_sec: float
    summary: str
    on_screen_text: str
    transcript: str


class StartupInfo(BaseModel):
    name: str
    tagline: str = ""


class PitchAnalysis(BaseModel):
    startup: StartupInfo
    founders: list[FounderInfo]
    problem_statement: str
    solution_summary: str
    technologies: list[str]
    industries: list[str]
    customer_segments: list[str]
    claims: list[PitchClaim]
    segments: list[PitchSegment]


def _norm_key(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _infer_batch(source: str) -> str:
    """Infer the YC batch (S19, W24, ...) from a filename/title, else ''.

    Overridable with --batch=S21 on the CLI (see main()).
    """
    import re
    m = re.search(r"[._\- ]([SW]\d{2})[._\- ]", source)
    return m.group(1) if m else ""


def structure_with_openai(pegasus_text: str) -> dict:
    """Turn Pegasus pitch prose into a schema-validated PitchAnalysis."""
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key or None)
    response = client.responses.parse(
        model=settings.openai_extraction_model,
        reasoning={"effort": settings.openai_reasoning_effort},
        input=[
            {"role": "system", "content": STRUCTURE_SYSTEM},
            {"role": "user", "content": f"Pitch video analysis:\n\n{pegasus_text}"},
        ],
        text_format=PitchAnalysis,
    )
    if response.output_parsed is None:
        raise RuntimeError("OpenAI did not return a structured pitch analysis.")
    return response.output_parsed.model_dump()


# ---------------------------------------------------------------------------
# Neo4j write
# ---------------------------------------------------------------------------

async def apply_schema() -> None:
    with open("../cypher/schema.cypher", "r") as f:
        body = f.read()
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    for stmt in [s.strip() for s in code.split(";") if s.strip()]:
        try:
            await execute_cypher(stmt, collect=False)
        except Exception as e:
            log.warning("schema stmt skipped: %s (%s)", stmt[:60], e)


async def write_pitch(video: dict, pitch: dict, segments: list[dict], batch: str = "") -> None:
    """Write one pitch: Video/Segment backbone + the typed startup layer."""
    domain = settings.domain_id
    startup_name = (pitch.get("startup") or {}).get("name") or video["title"]
    startup_key = _norm_key(startup_name)

    # Video node (title = startup name, so the graph panel reads well)
    await execute_cypher(
        """
        MERGE (v:Video {id: $id})
        SET v.title = $title, v.url = $url, v.duration_sec = $duration_sec,
            v.summary = $summary, v.tl_index_id = $tl_index_id, v.domain = $domain,
            v.batch = $batch
        """,
        {**video, "title": startup_name, "summary": pitch.get("solution_summary", ""),
         "domain": domain, "batch": batch},
        collect=False,
    )

    # Idempotent re-ingest: replace this video's segments + this startup's
    # claims and outgoing typed edges (shared Problem/Tech/... nodes stay).
    await execute_cypher(
        "MATCH (:Video {id: $vid})-[:HAS_SEGMENT]->(s:Segment) DETACH DELETE s",
        {"vid": video["id"]}, collect=False,
    )
    await execute_cypher(
        """
        MATCH (st:Startup {key: $skey})
        OPTIONAL MATCH (st)-[:CLAIMS]->(c:Claim) DETACH DELETE c
        WITH DISTINCT st
        OPTIONAL MATCH (st)-[r:SOLVES|USES|IN_INDUSTRY|TARGETS]->() DELETE r
        """,
        {"skey": startup_key}, collect=False,
    )

    # Startup + typed layer, all MERGE'd by normalized key
    await execute_cypher(
        """
        MATCH (v:Video {id: $vid})
        MERGE (st:Startup {key: $skey})
        SET st.name = $sname, st.tagline = $tagline, st.domain = $domain,
            st.solution = $solution, st.batch = $batch
        MERGE (st)-[:PITCHED_IN]->(v)
        WITH st
        FOREACH (f IN $founders |
          MERGE (p:Founder {key: f.key})
          SET p.name = f.name, p.role = f.role, p.domain = $domain
          MERGE (p)-[:FOUNDED]->(st))
        FOREACH (_ IN CASE WHEN $problem_key <> '' THEN [1] ELSE [] END |
          MERGE (pr:Problem {key: $problem_key})
          SET pr.statement = $problem, pr.name = $problem, pr.domain = $domain
          MERGE (st)-[:SOLVES]->(pr))
        FOREACH (t IN $technologies |
          MERGE (te:Technology {key: t.key}) SET te.name = t.name, te.domain = $domain
          MERGE (st)-[:USES]->(te))
        FOREACH (i IN $industries |
          MERGE (ind:Industry {key: i.key}) SET ind.name = i.name, ind.domain = $domain
          MERGE (st)-[:IN_INDUSTRY]->(ind))
        FOREACH (c IN $customer_segments |
          MERGE (cs:CustomerSegment {key: c.key}) SET cs.name = c.name, cs.domain = $domain
          MERGE (st)-[:TARGETS]->(cs))
        FOREACH (cl IN $claims |
          CREATE (c:Claim {text: cl.text, kind: cl.kind, approx_sec: cl.approx_sec,
                           video_id: $vid, domain: $domain})
          MERGE (st)-[:CLAIMS]->(c))
        """,
        {
            "vid": video["id"], "skey": startup_key, "sname": startup_name,
            "batch": batch,
            "tagline": (pitch.get("startup") or {}).get("tagline", ""),
            "solution": pitch.get("solution_summary", ""),
            "founders": [{"key": _norm_key(f["name"]), "name": f["name"].strip(),
                          "role": f.get("role", "")}
                         for f in pitch.get("founders", []) if f.get("name")],
            "problem": pitch.get("problem_statement", "").strip(),
            "problem_key": _norm_key(pitch.get("problem_statement", "")),
            "technologies": [{"key": _norm_key(t), "name": t.strip()}
                             for t in pitch.get("technologies", []) if t],
            "industries": [{"key": _norm_key(i), "name": i.strip()}
                           for i in pitch.get("industries", []) if i],
            "customer_segments": [{"key": _norm_key(c), "name": c.strip()}
                                  for c in pitch.get("customer_segments", []) if c],
            "claims": [c for c in pitch.get("claims", []) if c.get("text")],
            "domain": domain,
        },
        collect=False,
    )

    # Segment backbone with embeddings (keeps vector search + video inspector working)
    seg_rows = []
    for i, s in enumerate(segments):
        seg_rows.append({
            "id": f"{video['id']}#{i}", "idx": i, "video_id": video["id"],
            "start_sec": s.get("start_sec"), "end_sec": s.get("end_sec"),
            "summary": s.get("summary", ""),
            "on_screen_text": s.get("on_screen_text", ""),
            "transcript": s.get("transcript", ""),
            "embedding": s.get("embedding"),
        })
    await execute_cypher(
        """
        MATCH (v:Video {id: $vid})
        MATCH (st:Startup {key: $skey})
        UNWIND $rows AS row
          CREATE (s:Segment {id: row.id})
          SET s.video_id = row.video_id, s.start_sec = row.start_sec,
              s.end_sec = row.end_sec, s.summary = row.summary,
              s.on_screen_text = row.on_screen_text, s.transcript = row.transcript,
              s.embedding = row.embedding, s.domain = $domain, s.idx = row.idx
          MERGE (v)-[:HAS_SEGMENT]->(s)
          MERGE (s)-[:MENTIONS]->(st)
        """,
        {"vid": video["id"], "skey": startup_key, "rows": seg_rows,
         "domain": settings.domain_id},
        collect=False,
    )
    await execute_cypher(
        """
        MATCH (v:Video {id: $vid})-[:HAS_SEGMENT]->(s:Segment)
        WITH s ORDER BY s.idx
        WITH collect(s) AS segs
        UNWIND range(0, size(segs)-2) AS i
          WITH segs[i] AS a, segs[i+1] AS b
          MERGE (a)-[:NEXT]->(b)
        """,
        {"vid": video["id"]}, collect=False,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def _analyze_embed_write(index_id: str, video_id: str, url: str | None,
                               title: str, duration_sec, batch: str = "") -> int:
    log.info("Analyzing pitch video_id=%s with Pegasus ...", video_id)
    pegasus_text = tl.analyze_video(video_id, PITCH_PEGASUS_PROMPT, max_tokens=3000)
    pitch = structure_with_openai(pegasus_text)
    segments = pitch.get("segments", [])
    log.info("Extracted startup '%s' (%d founders, %d claims, %d segments). Embedding ...",
             (pitch.get("startup") or {}).get("name"), len(pitch.get("founders", [])),
             len(pitch.get("claims", [])), len(segments))

    dim = 0
    for s in segments:
        basis = " ".join(filter(None, [s.get("summary"), s.get("on_screen_text"),
                                       s.get("transcript")]))
        try:
            vec = tl.embed_text(basis or s.get("summary", ""))
            s["embedding"] = vec
            dim = dim or len(vec)
        except Exception as e:
            log.warning("  embed failed for a segment: %s", e)
            s["embedding"] = None

    video = {"id": video_id, "title": title, "url": url,
             "duration_sec": duration_sec, "tl_index_id": index_id}
    await write_pitch(video, pitch, segments, batch=batch)
    log.info("Wrote pitch '%s' to Neo4j", (pitch.get("startup") or {}).get("name") or title)
    return dim


async def ingest_new(index_id: str, source: str) -> int:
    is_file = not source.lower().startswith(("http://", "https://"))
    log.info("Indexing %s: %s ...", "file" if is_file else "url", source)
    cb = lambda st: log.info("  status: %s", st)  # noqa: E731
    if is_file:
        info = tl.ingest_video(index_id, video_file=source, on_update=cb)
    else:
        info = tl.ingest_video(index_id, video_url=source, on_update=cb)
    video_id = info.get("video_id")
    if not video_id:
        log.error("No video_id returned for %s — skipping", source)
        return 0
    title = (info.get("filename") or source.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
    url = None if is_file else source
    if is_file:
        try:
            url = tl.get_video_meta(index_id, video_id).get("url")
        except Exception:
            pass
    batch = BATCH_OVERRIDE or _infer_batch(source)
    return await _analyze_embed_write(index_id, video_id, url, title,
                                      info.get("duration_sec"), batch=batch)


async def ingest_existing(index_id: str, video_id: str) -> int:
    log.info("Ingesting already-indexed video %s ...", video_id)
    try:
        meta = tl.get_video_meta(index_id, video_id)
    except Exception as e:
        log.error("Cannot read video %s in index %s: %s", video_id, index_id, e)
        return 0
    title = (meta.get("filename") or video_id).rsplit(".", 1)[0]
    batch = BATCH_OVERRIDE or _infer_batch(title)
    return await _analyze_embed_write(index_id, video_id, meta.get("url"),
                                      title, meta.get("duration_sec"), batch=batch)


BATCH_OVERRIDE = ""


async def main() -> None:
    global BATCH_OVERRIDE
    args = sys.argv[1:]
    index_override: str | None = None
    video_ids: list[str] = []
    sources: list[str] = []
    for a in args:
        if a.startswith("--batch="):
            BATCH_OVERRIDE = a.split("=", 1)[1]
        elif a.startswith("--index-id="):
            index_override = a.split("=", 1)[1]
        elif a.startswith("--video-id="):
            video_ids.append(a.split("=", 1)[1])
        elif a.startswith("--"):
            log.warning("unknown flag %s", a)
        else:
            sources.append(a)

    if not sources and not video_ids:
        pitches_dir = Path(__file__).resolve().parents[2] / "data" / "videos" / "pitches"
        if pitches_dir.is_dir():
            sources = sorted(str(p) for p in pitches_dir.glob("*.mp4"))
        if sources:
            log.info("No inputs given — using data/videos/pitches/*.mp4 (%d files)", len(sources))
        else:
            log.error("Nothing to ingest. Pass URLs / file paths / --video-id=..., "
                      "or put .mp4 files in data/videos/pitches/")
            return

    await connect_neo4j()
    try:
        await apply_schema()
        if index_override:
            index_id = index_override
        elif settings.tl_index_id:
            index_id = settings.tl_index_id
        else:
            index_id = await asyncio.to_thread(tl.ensure_index)
        log.info("Using TwelveLabs index %s", index_id)

        dim = 0
        for vid in video_ids:
            try:
                d = await ingest_existing(index_id, vid)
                dim = dim or d
            except Exception as e:
                log.exception("Failed to ingest existing video %s: %s", vid, e)
        for src in sources:
            try:
                d = await ingest_new(index_id, src)
                dim = dim or d
            except Exception as e:
                log.exception("Failed to ingest %s: %s", src, e)

        if dim:
            await ensure_segment_vector_index(dim)
            log.info("Vector index ready (dim=%d)", dim)
        log.info("Done. Now run:  uv run python scripts/canonicalize.py")
    finally:
        await close_neo4j()


if __name__ == "__main__":
    asyncio.run(main())
