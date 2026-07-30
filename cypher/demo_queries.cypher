// Startup Pitch Network — demo queries
// Run in Neo4j Browser, or ask the agent in chat (it has run_cypher).
// Ask the chat versions verbatim — they're phrased to route to these traversals.

// ---------------------------------------------------------------------------
// 1. THE HEADLINE: which startups are (unknowingly) competing?
//    Chat: "Which startups in this batch are competing with each other, and on what problem?"
MATCH (a:Startup)-[c:COMPETES_WITH]->(b:Startup)
RETURN a, c, b;

// 2. The market map: problems as hubs, startups orbiting them
//    Chat: "Show me the market map — every problem and who is attacking it."
MATCH (s:Startup)-[r:SOLVES]->(p:Problem)
RETURN s, r, p;

// 3. Technology landscape: what technical approaches dominate the batch?
//    Chat: "What technologies show up across the most startups?"
MATCH (t:Technology)<-[:USES]-(s:Startup)
WITH t, collect(s) AS startups
WHERE size(startups) >= 2
UNWIND startups AS s
MATCH (s)-[r:USES]->(t)
RETURN t, r, s;

// 4. Adjacency: same industry, DIFFERENT problem — partnership / expansion radar
//    Chat: "Which startups are adjacent — same industry but solving different problems?"
MATCH (a:Startup)-[:IN_INDUSTRY]->(i:Industry)<-[:IN_INDUSTRY]-(b:Startup)
WHERE a.key < b.key
  AND NOT (a)-[:COMPETES_WITH]-(b)
RETURN a.name AS startup_a, b.name AS startup_b, i.name AS shared_industry;

// 5. Provenance wow: every traction claim, with the second it was made on stage
//    Chat: "List every revenue or traction claim in the batch with its timestamp."
MATCH (s:Startup)-[:CLAIMS]->(c:Claim)
MATCH (s)-[:PITCHED_IN]->(v:Video)
RETURN s.name AS startup, c.kind AS kind, c.text AS claim,
       c.approx_sec AS at_sec, v.title AS video
ORDER BY s.name, c.approx_sec;

// 6. Cross-modal check: what was ON THE SLIDES vs what was SAID
//    Chat: "Find the moment where <startup> shows their growth chart."
//    (agent uses search_video_moments / segment fulltext for this)
CALL db.index.fulltext.queryNodes('segment_fulltext', 'growth revenue chart')
YIELD node, score
MATCH (v:Video)-[:HAS_SEGMENT]->(node)
RETURN v.title, node.start_sec, node.on_screen_text, score
ORDER BY score DESC LIMIT 10;

// 7. Multi-hop: founder -> startup -> problem -> competing startup -> its founders
//    "Which founders should talk to each other?"
MATCH path = (f1:Founder)-[:FOUNDED]->(a:Startup)-[:SOLVES]->(p:Problem)
             <-[:SOLVES]-(b:Startup)<-[:FOUNDED]-(f2:Founder)
WHERE a.key < b.key
RETURN f1.name, a.name, p.name AS shared_problem, b.name, f2.name;

// 8. The whole graph (for the closing shot in NVL)
MATCH (n) WHERE n.domain = 'video-context-graph'
OPTIONAL MATCH (n)-[r]-(m)
RETURN n, r, m LIMIT 300;

// ---------------------------------------------------------------------------
// TEMPORAL (multi-batch: S19 vs S21 vs ...) — "how has the batch DNA changed?"

// 9. Technology mix by batch — the ChatGPT-era shift, quantified
//    Chat: "What kind of products are people building lately, and how has that changed?"
MATCH (s:Startup)-[:USES]->(t:Technology)
RETURN s.batch AS batch, t.name AS technology, count(*) AS startups
ORDER BY batch, startups DESC;

// 10. Problem spaces by batch — where founder attention moved
MATCH (s:Startup)-[:SOLVES]->(:Problem)-[:IN_SPACE]->(ps:ProblemSpace)
RETURN ps.name AS space, collect(DISTINCT s.batch + ':' + s.name) AS startups
ORDER BY size(startups) DESC;

// 11. Cross-batch competitors — an S21 startup attacking an S19 problem space
MATCH (a:Startup)-[c:COMPETES_WITH]-(b:Startup)
WHERE a.batch < b.batch
RETURN a.batch, a.name, c.via AS overlap, c.strength, b.batch, b.name;
