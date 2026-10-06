"""Shared source texts for the briefing tests."""

HEADLINE = "Nova Labs releases Atlas-2 open model"

STRONG_ARTICLE = (
    "Nova Labs on Tuesday released Atlas-2, an open-weight language model with 70 billion parameters. "
    "The model was trained on 15 trillion tokens and supports a context window of 256,000 tokens. "
    "On the MMLU benchmark Atlas-2 scored 86.4 percent, ahead of the 82.1 percent of its predecessor. "
    "Chief executive Dana Reyes said the release \"puts frontier-level reasoning in the hands of every developer.\" "
    "The weights are available under an Apache 2.0 license from today. "
    "Cloud providers including Azure and Google Cloud will host the model starting in November. "
    "Analysts say the release could pressure closed-model pricing, since inference costs fall by about 40 percent."
)

RSS_TEASER = "Nova Labs has released a new model. Read more."

HN_TEASER = "94 points, 145 comments on Hacker News"

BOILERPLATE_ARTICLE = (
    "Subscribe to our newsletter for the latest updates. "
    "Sign up for free to continue reading. "
    "Follow us on Twitter and Facebook. "
    "Copyright 2026 Example Media. All rights reserved. "
    "Read more: Related stories you may like."
)

NEWSLETTER_TAIL = (
    "Nova Labs released Atlas-2 on Tuesday with a 256,000 token context window and 86.4 percent on MMLU. "
    "The weights ship under an Apache 2.0 license and Azure will host the model from November. "
    "Subscribe to our newsletter to get stories like this in your inbox every morning. "
    "Sign up here to never miss an update."
)

TRUNCATED_RSS = (
    "Nova Labs has released Atlas-2, a 70 billion parameter model that scored 86.4 percent on MMLU, and the company "
    "said the weights are free to download but the licence terms for commercial use [...]"
)

LONG_SENTENCE_ARTICLE = (
    "Nova Labs on Tuesday released Atlas-2, an open-weight model with 70 billion parameters that the company says "
    "was trained on 15 trillion tokens of filtered web text, code and licensed books over a period of nine weeks "
    "on a cluster of 8,192 accelerators (the largest it has ever used), which the company said it financed itself. "
    "The weights are available under an Apache 2.0 license from today. "
    "Cloud providers including Azure and Google Cloud will host the model starting in November."
)

DUPLICATED_HEADLINE_ARTICLE = (
    "Nova Labs releases Atlas-2 open model. "
    "Nova Labs releases Atlas-2 open model, the company said. "
    "Nova Labs releases Atlas-2 open model on Tuesday."
)

NO_SIGNIFICANCE_ARTICLE = (
    "Nova Labs on Tuesday released Atlas-2 with 70 billion parameters. "
    "The weights are available under an Apache 2.0 license from today. "
    "Azure and Google Cloud will host the model starting November 4, 2026. "
    "A technical report lists a 256,000 token context window."
)

SIBLING_AGREE = (
    "Reuters reported that Nova Labs released Atlas-2 with 70 billion parameters on Tuesday. "
    "The company said the model scored 86.4 percent on MMLU and ships under an Apache 2.0 license. "
    "Azure will host the model starting in November, according to a company spokesperson."
)

SIBLING_CONFLICT = (
    "Nova Labs released Atlas-2 on Tuesday with 40 billion parameters. "
    "The company said the model scored 91.0 percent on MMLU. "
    "Azure will host the model starting in November, a spokesperson said."
)

THIN_RSS = (
    "Nova Labs released Atlas-2 on Tuesday with 70 billion parameters. "
    "The company said it scored 86.4 percent on the MMLU benchmark."
)

IDEAL_ARTICLE = (
    "Nova Labs on Tuesday released Atlas-2, an open-weight language model with 70 billion parameters that the company trained for nine weeks. "
    "The model was trained on 15 trillion tokens of filtered text and code, and it supports a context window of 256,000 tokens in a single request. "
    "On the MMLU benchmark Atlas-2 scored 86.4 percent, ahead of the 82.1 percent that its predecessor reached when it launched last year. "
    "Chief executive Dana Reyes said the release \"puts frontier-level reasoning in the hands of every developer.\" "
    "The weights are available under an Apache 2.0 license from today."
)

COMPRESSIBLE_SENTENCE = (
    "Nova Labs trained Atlas-2 on 15 trillion tokens of filtered web text and code over nine weeks "
    "(the largest corpus the lab has ever assembled for a single model), which the company said it financed entirely from internal funds."
)

# Short but every sentence carries a fact.
SHORT_DENSE_ARTICLE = (
    "Nova Labs released Atlas-2 on Tuesday with 70 billion parameters. "
    "It scored 86.4 percent on MMLU against 82.1 percent for its predecessor. "
    "The weights ship under an Apache 2.0 license, and Azure hosts the model from November 4."
)
