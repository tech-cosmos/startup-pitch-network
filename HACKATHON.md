# Startup Pitch Network — hackathon run book

**Pitch:** An agent watches a whole YC Demo Day batch and builds a live market
map — who's solving the same problem (competitors that don't know it yet), who's
adjacent, what technologies dominate, and every traction claim pinned to the
second it was said on stage.

Built on the video-context-graph starter. The delta:

| File | What it does |
|---|---|
| `backend/scripts/fetch_pitches.sh` | yt-dlp the pitch playlist → `data/videos/pitches/*.mp4` |
| `backend/scripts/ingest_pitch.py` | Pegasus pitch prompt → OpenAI Structured Outputs → typed startup graph + Segment backbone |
| `backend/scripts/canonicalize.py` | **Pass 2:** OpenAI clusters duplicate Problem/Tech/Industry/Segment nodes, merges them, derives `COMPETES_WITH` / `SHARES_STACK_WITH` |
| `cypher/schema.cypher` (appended) | Constraints + fulltext for the startup layer |
| `cypher/demo_queries.cypher` | The 8 demo queries, with the chat phrasing for each |
| `backend/app/agent.py` (edited) | Agent knows the startup ontology; `explore_graph` covers the new labels |
| `Makefile` (edited) | `make fetch-pitches`, `make seed-pitches`, `make canonicalize` |

## Graph model

```
(:Founder)-[:FOUNDED]->(:Startup)-[:PITCHED_IN]->(:Video)-[:HAS_SEGMENT]->(:Segment)-[:NEXT]->
(:Startup)-[:SOLVES]->(:Problem)        <- canonicalized: 2 pitches, 2 wordings, ONE node
(:Startup)-[:USES]->(:Technology)
(:Startup)-[:IN_INDUSTRY]->(:Industry)
(:Startup)-[:TARGETS]->(:CustomerSegment)
(:Startup)-[:CLAIMS]->(:Claim {text, kind, approx_sec})   <- provenance with timestamps
(:Startup)-[:COMPETES_WITH {via}]->(:Startup)             <- derived in pass 2
(:Startup)-[:SHARES_STACK_WITH {via}]->(:Startup)         <- derived in pass 2
```

## Run order

```bash
# 0. One-time setup (proves keys/env BEFORE the venue)
cp .env.example .env        # NEO4J_*, OPENAI_API_KEY, TWELVE_LABS_API_KEY
make install
make seed                   # stock sample clip through the stock pipeline = smoke test
make start                  # localhost:3000 — ask something, confirm end-to-end works

# 1. Get the corpus (do this on home wifi TONIGHT, not venue wifi)
brew install yt-dlp         # if needed
make fetch-pitches          # downloads the YC S19 playlist to data/videos/pitches/

# 2. (Optional but recommended) start clean
make reset && make schema

# 3. Ingest — ~2-4 min per pitch (TwelveLabs indexing is the long pole).
#    Start this EARLY and let it run; it's resumable per-file.
make seed-pitches
#    ...or one file at a time:
#    cd backend && uv run python scripts/ingest_pitch.py ../data/videos/pitches/01_*.mp4

# 4. Pass 2 — the magic. Re-run any time you add more pitches (idempotent).
make canonicalize

# 5. Demo
make start
```

## Fallbacks

- **A pitch extracts badly** (wrong name, garbage problem): just re-run
  `ingest_pitch.py` on that one file — re-ingest is idempotent. Or drop it;
  11 pitches demo the same as 12.
- **Venue wifi dies mid-indexing:** ingest the files you already got through;
  the graph works at any corpus size.
- **Canonicalizer over-merges:** lower ambition — edit `CLUSTER_SYSTEM` in
  `canonicalize.py` to be stricter, `make reset`, re-run steps 3–4 (video
  re-ingest by `--video-id` skips TwelveLabs re-indexing: the videos are
  already in the index).

## Demo script (3 minutes)

1. **Open on the full graph** (query 8 in `cypher/demo_queries.cypher`) — "our
   agent watched the entire batch; every node here came out of raw video."
2. **Chat:** *"Which startups in this batch are competing with each other, and
   on what problem?"* → COMPETES_WITH edges light up. Beat: "no founder said
   the word 'competitor' — the agent worked it out by canonicalizing problem
   statements across pitches."
3. **Chat:** *"List every revenue or traction claim with its timestamp."* →
   click a segment, video seeks to the founder saying the number. Beat:
   "every fact is evidence — a frame and a second, not a model's memory."
4. **Chat:** *"Which founders should talk to each other?"* → multi-hop
   Founder→Startup→Problem→Startup→Founder path. Beat: "that's a 4-hop
   traversal; there is no embedding that answers this."
5. **Close:** "Point the same pipeline at any demo day, earnings calls, or
   conference — the graph is the product."

## If asked "couldn't GPT just answer this?"

These are S19 companies, so GPT knows *of* them — but: (a) it doesn't know
what was said *in these pitches* (exact claims, phrasing, slides) and will
happily confuse post-2019 pivots with the pitch-day story; (b) every answer
here is grounded to a timestamp you can click and verify. The graph is an
evidence layer, not a memory.
