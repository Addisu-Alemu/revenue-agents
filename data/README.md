# Mock data

All companies, people and emails here are fictional. Domains use the reserved `.example` TLD.

| File | Stands in for | Used by |
| --- | --- | --- |
| `leads.json` | New leads in HubSpot (4 brand, 4 agency), arriving over ten days | Prospect / enrich / score |
| `enrichment.json` | An enrichment provider (e.g. Clearbit), keyed by domain | Enrich |
| `replies.json` | Inbound email replies | Reply handling |
| `transcripts/` | Fireflies call transcripts | Call → deal fields + lesson |
| `templates/io_template.md` | The IO / contract template (`{{placeholders}}`) | IO generation |
| `personas.json` | How each prospect behaves (demo only): what they answer, after what, and when | Buyer simulator (live world) |

## Designed test cases

- **Tiers:** each segment has one strong (A), two medium (B) and one weak (C) lead, so the demo shows all three tiers and the approval queue for Tier A.
- **Late arrivals:** Crumb & Co (brand, 9 Oct) and Northwind (agency, 11 Oct) arrive after the BrightBite calls, so their first emails can apply what those calls taught (Northwind gets a global lesson learned in the brand segment).
- **R-001:** a positive reply asking for a meeting.
- **R-002:** an objection that asks for pricing, which must be flagged for a human.
- **R-003:** a reply containing a prompt-injection attempt. Inbound content is untrusted, so the system must classify it without following it.
- **L-001 transcript:** contains budget, timeline, decision-maker, scope and objections, plus a clear lesson (franchise brands care more about brand control than speed).
