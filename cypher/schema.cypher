// Schema for the Video Agent Context Graph
// TwelveLabs (Marengo + Pegasus) -> Neo4j context graph
//
// Thesis: video is evidence. Each Video is broken into time-coded Segments;
// Pegasus/OpenAI extract Entities and Topics per segment; Entities and Topics
// are MERGE'd by normalized name so the SAME entity across many independent
// videos collapses to ONE node — the graph grows richer, not more duplicated.

// --- Core video domain -----------------------------------------------------
CREATE CONSTRAINT video_id_unique IF NOT EXISTS FOR (n:Video) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT segment_id_unique IF NOT EXISTS FOR (n:Segment) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT entity_key_unique IF NOT EXISTS FOR (n:Entity) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT topic_key_unique IF NOT EXISTS FOR (n:Topic) REQUIRE n.key IS UNIQUE;

CREATE INDEX video_title IF NOT EXISTS FOR (n:Video) ON (n.title);
CREATE INDEX segment_video_id IF NOT EXISTS FOR (n:Segment) ON (n.video_id);
CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name);
CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type);
CREATE INDEX topic_name IF NOT EXISTS FOR (n:Topic) ON (n.name);

// Domain scoping (all queries filter by n.domain to support multi-domain reuse)
CREATE INDEX video_domain IF NOT EXISTS FOR (n:Video) ON (n.domain);
CREATE INDEX entity_domain IF NOT EXISTS FOR (n:Entity) ON (n.domain);

// Full-text over human-readable text for keyword fallback / hybrid search
CREATE FULLTEXT INDEX segment_fulltext IF NOT EXISTS
FOR (n:Segment) ON EACH [n.summary, n.on_screen_text, n.transcript];

// --- Vector index for semantic segment search ------------------------------
// Created programmatically by scripts/ingest.py once the true Marengo embedding
// dimension is known (it varies by Marengo version). Template shown for reference:
//
// CREATE VECTOR INDEX segment_embeddings IF NOT EXISTS
// FOR (n:Segment) ON (n.embedding)
// OPTIONS { indexConfig: {
//   `vector.dimensions`: <DIM>, `vector.similarity_function`: 'cosine' } };

// --- Startup Pitch Network domain ------------------------------------------
// Hackathon delta: typed startup ontology on top of the Video/Segment backbone.
// All nodes keyed by normalized name; canonicalize.py merges near-duplicates
// (e.g. "AI scribe for vets" vs "clinical documentation for veterinarians").
CREATE CONSTRAINT startup_key_unique IF NOT EXISTS FOR (n:Startup) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT founder_key_unique IF NOT EXISTS FOR (n:Founder) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT problem_key_unique IF NOT EXISTS FOR (n:Problem) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT technology_key_unique IF NOT EXISTS FOR (n:Technology) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT industry_key_unique IF NOT EXISTS FOR (n:Industry) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT customersegment_key_unique IF NOT EXISTS FOR (n:CustomerSegment) REQUIRE n.key IS UNIQUE;

CREATE INDEX startup_name IF NOT EXISTS FOR (n:Startup) ON (n.name);
CREATE INDEX problem_statement IF NOT EXISTS FOR (n:Problem) ON (n.statement);

// Full-text over the startup layer for keyword lookup from the agent
CREATE FULLTEXT INDEX startup_fulltext IF NOT EXISTS
FOR (n:Startup|Problem|Technology|Industry|CustomerSegment)
ON EACH [n.name, n.statement, n.tagline];
