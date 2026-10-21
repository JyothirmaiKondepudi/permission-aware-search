# Customer note: permission-aware knowledge search

## What is ready

A local search service that answers support questions from each tenant's own knowledge base. It quotes an exact sentence and cites the document and revision it came from. When it cannot answer confidently, it says so and gives a reason instead of guessing.

- **Access is checked before anything else.** A user only ever gets documents from their own tenant that their groups allow, as of the time asked about. On the sample data, no answer cited a document the user could not see.
- **Changes take effect immediately.** Updates, deletions and permission changes apply from the moment they arrive. Cached answers are discarded at once, so asking the same question after losing access returns nothing protected.
- **Document text is treated as content.** Instructions written inside documents are never followed.
- **Operations:** snapshots for backup and restore; an invalid reload file is rejected and the live index keeps working.

We did not adopt four requested shortcuts: caching by question text alone, filtering after answering, keeping deleted documents searchable, and following instructions in documents. Each could expose protected or withdrawn content.

## Launch risk

- **Wording matters.** Matching is by words, so paraphrased questions ("money back" vs "refund") may be declined rather than answered wrongly.
- **One known gap:** when only one document is visible, some questions are declined even though that document answers them (about 2% of sample questions).
- **Not yet production-scale:** old revisions are kept forever, the index has no size cap, and searches run one at a time.

## Rollout plan

1. Load one low-risk tenant's real documents and permission groups. Run in shadow mode: answers are logged for review, not shown.
2. Review leaks (target zero), declined questions and answer quality with that tenant's support leads.
3. Show answers to that tenant's support staff, then add tenants in small waves. Watch the decline rate, latency and any access alarm.

## Rollback plan

- Take a snapshot before every reload or large import. If answers degrade, restore the previous snapshot.
- A bad reload file is rejected automatically, and the current index stays live.
- Answers can be switched off per tenant, falling back to the existing search with no answers.

## Before wider launch

Purge old and deleted text after a retention window, cap the index size, and run searches in parallel.
