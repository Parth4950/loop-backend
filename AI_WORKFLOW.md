## Where the AI was Wrong and I Caught It

### 1. Raw SQL Generation Injection Risk
*   **The AI's Mistake:** The coding agent initially configured the planner agent to emit raw Postgres SQL strings directly to execute against the database. 
*   **My Course-Correction:** I rejected this implementation due to severe SQL injection vulnerabilities and unpredictable query correctness during a live run. I forced a structural redesign: the Gemini agent now outputs a structured JSON filter payload (specifying parameters like `last_ordered_days` or `total_spent`), which my deterministic backend code safely compiles into parameterized SQL queries. The model decides *what* data to target; my code controls *how* it's safely fetched.

### 2. Broken Demo Seed Data (`now()` default trap)
*   **The AI's Mistake:** When creating the mock database seed script, the AI let all historical customer order timestamps default to PostgreSQL's `now()`. Because of this, every single customer's "last order date" registered as today.
*   **My Course-Correction:** When testing the "re-engage dormant buyers (60+ days)" segment prompt, the application returned exactly zero matches. I ran a quick distribution query directly on the database, diagnosed the timestamp clustering, and forced the AI agent to explicitly spread order dates backward across a realistic 6-month window so the filtering logic could actually function.

### 3. Agent Narrative vs. Action Divergence
*   **The AI's Mistake:** In early iterations of the multi-agent planning chain, the agent's natural language reasoning output would state: *"We will target this audience via WhatsApp due to high open rates,"* but the generated tool call payload (`create_campaign`) would pass `"channel": "email"`. 
*   **My Course-Correction:** I caught this divergence during log analysis. I refactored the prompt constraints and backend schema to enforce strict data grounding: the channel field was designated as the single source of truth, and a secondary validation step was introduced requiring the agent's generated narrative text to strictly match the selected structured enum value.