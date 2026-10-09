# Mock data

All companies, people and emails here are fictional. Domains use the reserved `.example` TLD.

| File | Stands in for | Used by |
| --- | --- | --- |
| `leads.json` | New leads in HubSpot (3 brand, 3 agency) | Prospect / enrich / score |
| `enrichment.json` | An enrichment provider (e.g. Clearbit), keyed by domain | Enrich |
| `replies.json` | Inbound email replies | Reply handling |
| `transcripts/` | Fireflies call transcripts | Call → deal fields + lesson |
| `templates/io_template.md` | The IO / contract template (`{{placeholders}}`) | IO generation |

## Designed test cases

- **Tiers:** each segment has one strong (A), one medium (B) and one weak (C) lead, so the demo shows all three tiers and the approval queue for Tier A.
- **R-001:** a positive reply asking for a meeting.
- **R-002:** an objection that asks for pricing, which must be flagged for a human.
- **R-003:** a reply containing a prompt-injection attempt. Inbound content is untrusted, so the system must classify it without following it.
- **L-001 transcript:** contains budget, timeline, decision-maker, scope and objections, plus a clear lesson (franchise brands care more about brand control than speed).
