# Biopharma brief

Writes the daily Alexa Flash Briefing at
[zainarfoosh/briefing](https://github.com/zainarfoosh/briefing). Pulls the newsletters
out of Gmail, has Claude write a ~600-word spoken brief for someone building toward
life sciences PE/VC, and commits `feed.json` for the skill to read aloud.

```
Gmail (IMAP) → clean & dedupe → Claude → feed.json → git push → Alexa
```

## Setup

These files belong in the `briefing` repo alongside the existing `feed.json`, so the
skill keeps reading the same URL and nothing changes on the Alexa side. The repo
already has a `README.md` — keep whichever you prefer.

### 1. Gmail app password

Needs 2-Step Verification on the account. Create one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and paste
it without spaces.

### 2. Find the label's IMAP name

Gmail labels are IMAP folders, but a nested label reads as `Parent/Biopharma`. Confirm
the real string before anything else:

```bash
pip install -r requirements.txt && python fetch.py --list-folders
```

If it isn't plain `Biopharma`, set `GMAIL_FOLDER` to whatever it prints.

### 3. Test locally

```bash
export GMAIL_USER='you@gmail.com' GMAIL_APP_PASSWORD='...' ANTHROPIC_API_KEY='sk-ant-...'
python run.py --dry-run
```

`--dry-run` prints the script and writes nothing. **Read it out loud once** — every TTS
problem worth fixing (a stray symbol, an unexpanded abbreviation, a sentence with no room
to breathe) is obvious when spoken and invisible on screen.

To see what the fetcher is pulling before spending a token:

```bash
python fetch.py --hours 48
```

### 4. Schedule it

Add under **Settings → Secrets and variables → Actions**:

| Name | Kind | Value |
| --- | --- | --- |
| `GMAIL_USER` | secret | your Gmail address |
| `GMAIL_APP_PASSWORD` | secret | the app password from step 1 |
| `ANTHROPIC_API_KEY` | secret | from [console.anthropic.com](https://console.anthropic.com) |
| `GMAIL_FOLDER` | variable | only if the label isn't `Biopharma` |

Secrets stay private even though the repo is public — Actions never exposes them in logs.

Then run the workflow manually once from the **Actions** tab to confirm it commits
without your Mac involved. After that it fires at 10:30 UTC daily — 6:30am Eastern in
summer, 5:30am in winter, because GitHub's cron doesn't follow daylight saving.

If a run fails it opens an issue that @mentions you. That's deliberate: the failure mode
that kills a thing like this is a morning that silently doesn't happen.

## The Flash Briefing format

`feed.json` is a rolling array, newest first, in exactly the shape the skill already
reads:

```json
[
  {
    "uid": "urn:uuid:biopharma-briefing-2026-08-12",
    "updateDate": "2026-08-12T10:32:11.0Z",
    "titleText": "Biopharma Briefing",
    "mainText": "Good morning, Zain. ...",
    "redirectionUrl": "https://..."
  }
]
```

Four Amazon constraints the code enforces, because each one fails quietly rather than
loudly:

- **`mainText` caps at 4,500 characters.** Past that Amazon truncates mid-brief. The
  script targets ~600 words (~3,700 characters); if it ever runs over, `run.py` cuts at a
  sentence itself and prints a warning, so you choose the ending rather than Amazon.
- **No SSML, HTML, or XML.** Flash Briefing text feeds are plain text only — a tag is
  either read aloud or rejects the item. `to_plain_text()` strips tags and markdown as a
  net under the prompt.
- **Only the first 5 items are read**, so the feed keeps a rolling five.
- **Items older than 7 days are ignored**, so those get pruned.

A same-day rerun replaces its own entry instead of duplicating the `uid`.

`redirectionUrl` — where "read more" goes in the Alexa app — points at whichever story
the brief singled out in its "Worth reading deeper" paragraph, falling back to the first
story with a link.

`digest.md` is the same brief with live links for every story, for after the Echo
mentions something worth opening. `state.json` tracks which messages have been covered,
so a rerun never repeats a story.

## Tuning

Everything editorial is `SYSTEM_PROMPT` in [summarize.py](summarize.py) — length, how
many stories, how much to weight deals against science, how much to explain versus
assume, and the voice. It encodes the style of the existing briefs: the "Good morning,
Zain" open, thematic grouping with spoken transitions, spelled-out numbers, and the
"Worth reading deeper" close.

The explain-versus-assume dial is set deliberately asymmetric: assume the science
(modalities, delivery tech, trial design, mechanisms) and explain the commercial side
(deal structure, what a number implies, how a readout re-rates a sector). Stories
touching drug delivery, mRNA/LNP, cell and gene therapy, and devices get extra weight.
That's the setting most likely to need moving as the commercial fluency builds.

Edit and rerun with `--dry-run`. Expect about a week of daily listening before it sounds
right; the first drafts will over-cover and under-rank. The dial most likely to need
moving is explain-versus-assume, since the right level shifts as the fluency builds.

## Choosing the model

The provider is a switch, not a rewrite — the prompt and schema are shared, so only the
writer changes.

| | Default | | |
| --- | --- | --- | --- |
| Provider | `cohere` | `anthropic` | `gemini` |
| Model | `command-a-plus-05-2026` | `claude-sonnet-5` | `gemini-2.5-flash` |
| Cost | $0 (trial key) | ~$5–8/month | $0 |
| Key | `COHERE_API_KEY` | `ANTHROPIC_API_KEY` | `GEMINI_API_KEY` |
| Get one | [dashboard.cohere.com](https://dashboard.cohere.com/api-keys) | [console.anthropic.com](https://console.anthropic.com) | [aistudio.google.com](https://aistudio.google.com/apikey) |

Switch by setting the `LLM_PROVIDER` Actions **variable**; only that provider's key needs
to exist. `COHERE_MODEL`, `GEMINI_MODEL`, and the `ANTHROPIC_MODEL` constant are one-line
overrides in either direction.

**Rate limits are not the deciding factor.** This job makes one request per day. A Cohere
trial key allows 1,000 calls/month and 20 requests/minute on chat models — roughly thirty
times the headroom this needs. Two Cohere-specific notes: trial keys are documented as not
for production or commercial use (a personal morning brief is neither, but it's their
term, not mine), and if a trial key rejects `command-a-plus-05-2026`, fall back to
`command-a-03-2025` — which has a larger 256k context but caps output at 8k, right on top
of `MAX_TOKENS`.

**What actually differs is quality on the part that matters most.** Nearly all the work
here is editorial judgment — deciding which four of twenty stories earn airtime, and
writing a real "so what" for each. That's exactly where model strength shows, and it's
insidious when it's missing: a weaker ranking still produces a fluent, confident brief,
just one that quietly spends your morning on the wrong stories. You get no signal that it
happened.

So don't take my word on it — run them on the same morning and read side by side:

```bash
python run.py --provider cohere    --dry-run
python run.py --provider anthropic --dry-run
```

`--dry-run` writes nothing, so compare as often as you like.

One more consideration if you're weighing the free tiers: Google's free tier uses
submitted data to improve their products (the paid tier does not). Check Cohere's current
terms for the equivalent. The input is newsletters you didn't write, so the exposure is
mild — but the brief does reflect what you read.
