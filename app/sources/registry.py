NEWS_SOURCES = [
    {
        "name": "OpenAI",
        "url": "https://openai.com/news/rss.xml",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "Google AI",
        "url": "https://blog.google/technology/ai/rss/",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "VentureBeat AI",
        "url": "https://venturebeat.com/category/ai/feed/",
        "source_type": "rss",
        # Re-confirmed 2026-09-17 (still HTTP 429, Vercel bot challenge,
        # same as 2026-09-10): a real fix needs a headless browser to
        # clear a JS challenge, which is out of scope for this
        # project's local-first/minimal-dependency approach -- and not
        # something to build bot-detection evasion for. Disabled until
        # VentureBeat offers a real, non-challenged feed or API.
        "enabled": False,
    },
    {
        "name": "The Verge AI",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "MIT Technology Review AI",
        "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "Microsoft Research Blog",
        "url": "https://www.microsoft.com/en-us/research/blog/feed/",
        "source_type": "rss",
        # The old "Microsoft AI Blog" feed (blogs.microsoft.com/ai/feed/)
        # returns HTTP 410 Gone -- retired, not moved (re-confirmed
        # 2026-09-17). This is a real, currently-active, official
        # replacement (verified HTTP 200, well-formed RSS, recent
        # posts). Broader than AI-only, same as Hacker News already is
        # -- the deterministic AI-relevance filter narrows it down,
        # same pattern as every other source here.
        "enabled": True,
    },
    {
        "name": "NVIDIA Blog",
        "url": "https://blogs.nvidia.com/feed/",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "Hugging Face Blog",
        "url": "https://huggingface.co/blog/feed.xml",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "Ars Technica AI",
        "url": "https://arstechnica.com/ai/feed/",
        "source_type": "rss",
        "enabled": True,
    },
    {
        "name": "Google DeepMind News",
        "url": "https://deepmind.google/blog/rss.xml",
        "source_type": "rss",
        # Verified 2026-09-15: official feed, HTTP 200, valid RSS 2.0,
        # latest item dated 2026-09-08.
        "enabled": True,
    },
    {
        "name": "Wired — Artificial Intelligence",
        "url": "https://www.wired.com/feed/tag/ai/latest/rss",
        "source_type": "rss",
        # Verified 2026-09-15: official Condé Nast tag feed, HTTP 200,
        # valid RSS 2.0, latest item dated 2026-09-14.
        "enabled": True,
    },
    {
        "name": "Simon Willison's Weblog",
        "url": "https://simonwillison.net/atom/everything/",
        "source_type": "rss",
        # Verified 2026-09-29: official Atom feed (feedparser handles
        # Atom transparently, same as every RSS 2.0 source above --
        # source_type stays "rss" for all of them, no separate label
        # exists in this codebase). HTTP 200, 30 entries, latest dated
        # 2026-09-28 (same day). A well-known independent AI
        # practitioner/commentator blog -- directly relevant, not a
        # general tech outlet the relevance filter has to narrow down.
        "enabled": True,
    },
    {
        "name": "Google Research Blog",
        "url": "https://research.google/blog/rss/",
        "source_type": "rss",
        # Verified 2026-09-29: HTTP 200, valid RSS, 100 entries, latest
        # dated 2026-09-24. Broader than AI-only, same as Microsoft
        # Research Blog above -- the deterministic AI-relevance filter
        # narrows it down, same pattern.
        "enabled": True,
    },
    {
        "name": "IEEE Spectrum — Artificial Intelligence",
        "url": "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss",
        "source_type": "rss",
        # Verified 2026-09-29: official IEEE Spectrum AI-topic tag feed,
        # HTTP 200, valid RSS, 30 entries, latest dated 2026-09-28.
        "enabled": True,
    },
    {
        "name": "Amazon Science",
        "url": "https://www.amazon.science/index.rss",
        "source_type": "rss",
        # Verified 2026-09-29: HTTP 200, valid RSS, 25 entries, latest
        # dated 2026-09-25.
        "enabled": True,
    },
    {
        "name": "Berkeley AI Research (BAIR)",
        "url": "https://bair.berkeley.edu/blog/feed.xml",
        "source_type": "rss",
        # Verified 2026-09-29: HTTP 200, valid RSS, 10 entries. Real but
        # low-frequency (latest item dated 2026-07-29, ~2 months old as
        # of verification) -- kept anyway, same as this project accepts
        # any source that doesn't post daily; it simply won't
        # contribute a story most days, not a broken feed.
        "enabled": True,
    },
    # Weekend-active sources, added 2026-10-05 after Ep10 (Sat) and Ep11
    # (Sun) came up short (14 and 16 stories): the weekday outlets above
    # publish almost nothing on Sat/Sun and the ingestion window is the
    # IST calendar day. All verified HTTP 200, valid feed, items dated
    # on Sat/Sun in the latest entries.
    {
        "name": "The Decoder",
        "url": "https://the-decoder.com/feed/",
        "source_type": "rss",
        # AI-only news site; 10 of its latest 10 entries fell on Sat/Sun.
        "enabled": True,
    },
    {
        "name": "The Guardian AI",
        "url": "https://www.theguardian.com/technology/artificialintelligenceai/rss",
        "source_type": "rss",
        # Guardian AI topic feed; 9 of 20 entries on Sat/Sun.
        "enabled": True,
    },
    {
        "name": "Latent Space",
        "url": "https://www.latent.space/feed",
        "source_type": "rss",
        # AI engineering newsletter/podcast; latest entry Sat 2026-10-03.
        "enabled": True,
    },
    {
        "name": "Zvi Mowshowitz (Don't Worry About the Vase)",
        "url": "https://thezvi.substack.com/feed",
        "source_type": "rss",
        # Weekly AI roundup posts, frequently on weekends.
        "enabled": True,
    },
    {
        "name": "Last Week in AI",
        "url": "https://lastweekin.ai/feed",
        "source_type": "rss",
        # Weekly AI news digest, latest entry Sat 2026-10-03.
        "enabled": True,
    },
    {
        "name": "Futurism",
        "url": "https://futurism.com/feed",
        "source_type": "rss",
        # General tech/science, ~40 of 92 entries on Sat/Sun. Broader
        # than AI-only, so the deterministic AI-relevance filter does
        # the narrowing (same pattern as Microsoft/Google Research).
        "enabled": True,
    },
    # Second round (2026-10-05): after the first six, the Oct 4 pool was
    # 28 (needs 30). These are broad tech outlets that publish 7 days a
    # week; the AI-relevance filter narrows them. Probed 2026-10-05:
    # in-window items on Sun Oct 4 / Sat Oct 3 with an AI keyword.
    {
        "name": "Tom's Hardware",
        "url": "https://www.tomshardware.com/feeds/all",
        "source_type": "rss",
        # 16 items Oct 4 (7 AI-ish), 15 Oct 3 (6 AI-ish).
        "enabled": True,
    },
    {
        "name": "The Next Web",
        "url": "https://thenextweb.com/feed",
        "source_type": "rss",
        # 9 items Oct 4 (4 AI-ish).
        "enabled": True,
    },
    {
        "name": "TechRadar",
        "url": "https://www.techradar.com/rss",
        "source_type": "rss",
        # 30 items Oct 4 (4 AI-ish), 13 Oct 3.
        "enabled": True,
    },
    {
        "name": "Business Insider Tech",
        "url": "https://www.businessinsider.com/rss",
        "source_type": "rss",
        # 17 items Oct 4 (3 AI-ish).
        "enabled": True,
    },
    {
        "name": "Engadget",
        "url": "https://www.engadget.com/rss.xml",
        "source_type": "rss",
        # 8 items Oct 4 (3 AI-ish); 18 of 20 entries on Sundays.
        "enabled": True,
    },
    {
        "name": "SiliconANGLE AI",
        "url": "https://siliconangle.com/category/ai/feed/",
        "source_type": "rss",
        # AI category; 1 item Oct 4, 4 Oct 3.
        "enabled": True,
    },
    {
        "name": "Towards Data Science",
        "url": "https://towardsdatascience.com/feed",
        "source_type": "rss",
        # 2 items Oct 4, 2 Oct 3.
        "enabled": True,
    },
]

# Candidates evaluated and deliberately NOT added, so a future pass
# doesn't re-try the same dead ends:
# - Anthropic, Meta AI: no advertised RSS/Atom <link> on their news/blog
#   pages and no working feed found at any common guessed path --
#   genuinely no public feed to subscribe to, not a bot-block (unlike
#   VentureBeat below).
# - r/MachineLearning, r/artificial (Reddit RSS): the first request
#   succeeded, but a second request moments later returned HTTP 429 --
#   same class of unreliable-for-a-scheduled-bot risk as VentureBeat's
#   Vercel challenge below, not something to build retry/backoff
#   evasion for.
# - MIT CSAIL News: feed parses fine but its latest entry is dated
#   2019 -- abandoned, would never contribute a story in any real
#   collection window.
# - Stanford HAI News, IBM Research Blog, Stability AI, MarkTechPost:
#   HTTP 404/403, or a 200 with zero parseable entries (bozo=1) at
#   verification time.
