# Classification evaluation set (Oct 3-5, 2026)

Purpose: measure candidate classification approaches against HUMAN-CORRECTED labels,
not against the old keyword classifier. Nothing here changes production behaviour.

Files
- `items.csv`            357 items (title + cleaned summary + source). Contains the OLD classifier result
                         in `old_*` columns; draft labels were written WITHOUT looking at those columns
                         for Oct 3-4 (blind). Oct 5 labels are NOT blind: the old result and the 74 rejected
                         titles had already been reviewed in an earlier investigation.
- `labels.csv`           original draft labels (Claude, from title + summary only), before human correction.
- `labels_corrected.csv` HUMAN-CORRECTED labels: the ground truth. Use `--labels labels_corrected.csv`.
- `evaluate.py`          harness (runs candidate approaches, prints metrics).
- `ensemble.py`, `embed.py`  embedding ensemble and embedding generation (the `emb_*.npy` caches were
                         removed; regenerate with `embed.py`).

## Label fields (one row per item)

| field | values | meaning |
|---|---|---|
| tech | y / n | Is it technology-related at all (software, hardware, AI, internet, science-of-computing)? |
| ai | core / adj / none | core = about AI/ML/LLMs/agents/models; adj = AI-adjacent (autonomy, robotics, AI infrastructure/chips, surveillance with ML, AI-driven policy); none |
| ctype | dev / rel / res / proj / opin / tut / rev / deal / event / spons / round / bg / other | development (a concrete announcement/action/event), release, research result, project (OSS/dev project), analysis-opinion, tutorial/how-to, product review/comparison, deal/promo, event promo, sponsored, roundup/listicle/podcast digest, background/explainer, other/non-tech |
| aud | h / m / l | value to developers, engineers, builders, researchers, founders, technical enthusiasts |
| exp | cand / rev / rej | EXPECTED disposition (what a good classifier should do) |
| conf | h / m / l | how sure the labeller is of `exp` (low = please look at this one first) |
| tags | comma list | hard-example themes (below) |

## Rubric for `exp`

- **rej**: not technology; or ctype in tutorial / review / deal / event / sponsored; or a generic opinion,
  roundup or background piece with no new development; routine consumer hardware, gaming, business or
  local-planning news; routine security items; "AI" only as a passing phrase ("AI tax", "AI era").
- **cand**: technology AND a concrete development / release / research result / project AND audience m-h
  AND (AI core/adjacent, or a clearly significant non-AI technical development).
- **rev**: anything borderline: AI-adjacent with unclear angle, policy/legal stories about AI, polls and
  statements by notable people, infrastructure/politics hybrids, product comparisons with a real AI angle,
  thin or empty summaries that cannot be judged.

## Hard-example tags

`name_gap` product/brand names that carry the AI meaning without a generic AI term (ChatGPT, Muse, Apple
Intelligence, Copilot, Codex...) · `implicit` AI/agentic meaning with no AI vocabulary · `robot_emerging`
robotics / physical AI / emerging tech · `ai_nonnews` an AI term is present but the item is a tutorial,
promo, opinion, review, deal or other non-news · `oss_project` open-source or developer project ·
`nonai_tech` technology story with no AI angle · `chips_infra` AI chips / data-center infrastructure ·
`policy_safety` AI policy, safety, legal · `thin` empty or uninformative summary.

Labels are judgments, not facts. Disagreement is expected; edit `labels_corrected.csv` directly.
