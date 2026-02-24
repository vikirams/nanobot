---
name: highperformr
description: Highperformr MCP discovery, deep research orchestration, dataset merging, deduplication, segment activation, and CSV export.
alwaysLoad: true
---

# Highperformr MCP Skill


## Available MCP Tools

discover  
push-data-to-segment  
list-segments  
import-csv  
export-contacts  
run-workflow  


---

# Discovery Protocol

Discovery always occurs in two phases:

Phase 1 — Database Discovery  
Phase 2 — Deep Research Augmentation


---

# Phase 1 Database Discovery

Invoke:

discover tool

Parameters:

type = contact OR company  
query = user discovery intent  

Example:

discover(
 type="contact",
 query="CTOs at fintech companies in India"
)


Store result as primary_dataset


---

# Phase 2 Deep Research Augmentation

Deep research MUST use promptengineering_deepresearch skill.

Invoke:

discover(
 type="deepsearch",
 query=generated_deep_research_prompt,
 expected_format="json"
)


Store result as deep_research_dataset


---

# Dataset Merge Protocol

Merge:

primary_dataset  
deep_research_dataset


Unified schemas:

Contact schema:

name  
linkedin_url  
title  
company_linkedin  
company_url  


Company schema:

company_name  
industry  
company_linkedin  
company_url  


---

# Deduplication Protocol

Contact dedupe priority:

linkedin_url  
name + company_linkedin  
name + company_url  


Company dedupe priority:

company_linkedin  
company_url  
company_name  


Keep most complete record.


---

# Segment Push Protocol

Use push-data-to-segment tool only after confirmation.

Example:

push-data-to-segment(
 segment_id="segment123",
 contacts=dataset
)


---

# CSV Download Protocol

CSV download is generated locally.

Do NOT use export-contacts tool.


---

# Segment Listing Protocol

Use list-segments tool when user wants segment selection.


---

# Workflow Execution Protocol

Use run-workflow only on explicit request.


---

# Dataset Integrity Rules

Never fabricate data.

Never modify factual values.

Always preserve structure.


---

END OF SKILL.md
