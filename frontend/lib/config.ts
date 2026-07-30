/**
 * Domain configuration for the Startup Pitch Network
 */

export const DOMAIN = {
  id: "video-context-graph",
  name: "Startup Pitch Network",
  description:
    "An agent that watches YC Demo Day pitches and builds a Neo4j context graph — startups, founders, problems, technologies, competitors, and how it all shifted batch over batch.",
  tagline: "62 pitches. Ten years of Demo Days. One graph.",
};

export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export const NODE_COLORS: Record<string, string> = {
  Video: "#8b5cf6",
  Segment: "#0ea5e9",
  Startup: "#f97316",
  Founder: "#22c55e",
  Problem: "#ef4444",
  ProblemSpace: "#dc2626",
  Technology: "#3b82f6",
  Industry: "#a855f7",
  CustomerSegment: "#14b8a6",
  Claim: "#eab308",
  Entity: "#f59e0b",
  Topic: "#10b981",
};

export const NODE_SIZES: Record<string, number> = {
  Video: 30,
  Segment: 16,
  Startup: 36,
  Founder: 22,
  Problem: 26,
  ProblemSpace: 32,
  Technology: 24,
  Industry: 24,
  CustomerSegment: 22,
  Claim: 18,
  Entity: 22,
  Topic: 22,
};

export const DEFAULT_CYPHER = `MATCH (s:Startup)-[r:SOLVES]->(p:Problem) RETURN s, r, p LIMIT 150`;

export const SCHEMA_NODE_SIZE = 30;
export const SCHEMA_REL_COLOR = "#94a3b8";

export interface GraphData {
  results: Record<string, unknown>[];
}

export interface DemoScenario {
  name: string;
  prompts: string[];
}

export const DEMO_SCENARIOS: DemoScenario[] = [
  {
    name: "Market map",
    prompts: [
      "Which startups are competing with each other, and on what problem?",
      "Show me the healthcare market map",
    ],
  },
  {
    name: "Evidence",
    prompts: [
      "List every traction claim Basis made, with timestamps",
      "Which startup claimed the biggest market size, and when did they say it?",
    ],
  },
  {
    name: "Trends",
    prompts: [
      "How did what founders build change after ChatGPT?",
      "Which founders should talk to each other, and why?",
    ],
  },
];
