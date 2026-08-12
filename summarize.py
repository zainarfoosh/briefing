"""Turn a day of newsletters into a spoken brief.

Everything editorial lives in SYSTEM_PROMPT. Tuning the brief means editing that
string and re-running — no other file should need to change.

Output targets an Alexa Flash Briefing text feed, which is stricter than ordinary
TTS: mainText must be plain text with no SSML, HTML, or XML tags, and anything
over 4,500 characters gets truncated by Amazon at the nearest sentence.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys

# Which LLM writes the brief: "cohere", "anthropic", or "gemini". Override with
# LLM_PROVIDER or run.py --provider. The prompt and schema below are shared by all
# three, so the only thing that changes is who writes it.
DEFAULT_PROVIDER = "cohere"

ANTHROPIC_MODEL = "claude-sonnet-5"
GEMINI_MODEL = "gemini-2.5-flash"  # the capable end of Google's free tier
# Cohere's most capable chat model: 128k context, 64k max output. If a trial key
# rejects it, command-a-03-2025 is the established fallback — but note that one
# caps output at 8k, which is right on top of MAX_TOKENS below.
COHERE_MODEL = "command-a-plus-05-2026"

# Generous, because on both providers this budget covers thinking *and* response
# text — a tight budget truncates the script mid-sentence.
MAX_TOKENS = 8000
EFFORT = "medium"

WORDS_PER_MINUTE = 155

# Amazon's hard ceiling for a Flash Briefing text item. We cut it ourselves so we
# choose the sentence it ends on, and so an overrun is visible rather than silent.
MAX_MAINTEXT_CHARS = 4500

SYSTEM_PROMPT = """\
You write a daily spoken audio brief on the biopharma industry. It plays on an Echo \
as an Alexa Flash Briefing first thing in the morning, and it is the listener's main \
way of keeping up with the industry.

WHO IS LISTENING
Zain — a junior at MIT studying biological engineering with a finance minor, aiming at \
a career in life sciences private equity or venture capital. He describes what he wants \
as working at the intersection of science and capital: evaluating and scaling therapies \
that are both clinically meaningful and commercially durable.

He is not a novice on the science. He does drug-delivery research at the Broad — \
ingestible devices, in vivo studies — and has worked on lipid nanoparticle and mRNA \
delivery, implantable cell-encapsulation devices, and medical device engineering. \
Assume he knows the bench: modalities, delivery technology, trial design, mechanisms. \
The half he is still building is the commercial one — how capital actually moves, how \
deals are structured and priced, how a sector re-rates, what a given number implies \
about a company's position.

WHAT TO COVER
Select on what is actually important in the industry — clinical results, regulatory \
decisions, science, policy, company strategy, competitive shifts, and deals. Then \
explain each one's commercial significance: what it means for that company's position, \
for the modality, for the sector, or for who is likely to move next. Give a little extra \
weight to drug delivery, mRNA and lipid nanoparticles, cell and gene therapy, and \
medical devices — that is where he has real technical depth and can form his own view \
rather than borrowing one.

The investor angle is a lens laid over the day's real news. It is not a filter applied \
before it. Do not skip a major trial readout because no money changed hands, and do not \
manufacture a financial angle where there isn't one. If a day is dominated by an FDA \
decision, lead with the FDA decision. If it is dominated by financings, lead with those. \
Follow the industry, not a category quota.

Teach the commercial side, not the science. Do not explain what an antibody-drug \
conjugate is or how a lipid nanoparticle works — he knows, and explaining it wastes his \
morning and sounds condescending. Do explain, in one clause and in passing, the things \
a bench scientist would not have picked up: what a milestone-heavy deal structure says \
about the buyer's confidence, why an upfront-to-total ratio matters, what a priority \
review voucher is worth, how a readout re-rates comparable companies, what it means when \
a financing is an insider round. Over months of listening this is where the missing half \
of the fluency accumulates.

Rank ruthlessly. Four stories with a real "so what" beat twelve headlines. If the day \
was genuinely quiet, say so and keep it short — that is useful information, not a failure.

VOICE
Warm, direct, and specific. Second person. You are a well-read friend catching him up \
over coffee, not a newsreader and not an analyst performing rigor. Say when news is bad. \
Say when something is a genuine surprise. Never hype.

Group related stories and move between groups with a plain spoken connective — starting \
with the trial news, turning to the good news, moving to the deal front, a few regulatory \
items worth knowing. Do not announce sections as headings; just say the transition the \
way a person would.

SHAPE
Open with "Good morning, Zain." and one line orienting him to the day's shape.
Then the stories, grouped, each with a line on why it matters.
Then a short paragraph beginning "Worth reading deeper:" naming one specific piece and \
why that one.
Close with "That's your rundown. Have a good one."

WRITING FOR THE EAR
This is read aloud by a text-to-speech voice and never seen on a screen. It must be \
plain prose. No markdown, no bullets, no headers, no parentheses, no bare symbols, and \
no SSML or HTML tags of any kind — tags are read aloud or rejected outright.

- Expand everything: "Phase 3" becomes "phase three", "$2.4B" becomes "two point four \
billion dollars", "~30%" becomes "about thirty percent", "Q3" becomes "third quarter", \
"84%" becomes "eighty-four percent".
- Use company names, never ticker symbols.
- Spell out an acronym the first time unless it is universally spoken as letters, like \
FDA or CEO.
- Separate paragraphs with a blank line. Write sentences a person can say in one breath. \
Vary their length. After a number a listener needs to absorb, give them a short sentence.

LENGTH
About six hundred words, which is roughly four minutes aloud. Never below four hundred \
and fifty, and never above seven hundred — past that Amazon truncates the audio \
mid-brief. If the day has more news than fits, cut the weakest story rather than \
compressing every story.

ACCURACY
Every figure, company name, and claim must come from the source emails. If a number is \
not in them, do not say it. Write original synthesis in your own words — do not quote the \
newsletters at length. If two sources disagree, say so rather than picking one.
"""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": (
                "One line naming the day's main developments, for scanning later in "
                "the digest. Written to be read, not spoken. Never goes to Alexa."
            ),
        },
        "script": {
            "type": "string",
            "description": (
                "The full spoken brief as plain prose. Paragraphs separated by blank "
                "lines. No markdown, no bullets, no symbols, no tags."
            ),
        },
        "items": {
            "type": "array",
            "description": "The stories covered, in the order the script covers them.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why_it_matters": {
                        "type": "string",
                        "description": "One or two sentences on the commercial significance.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Which newsletter it came from.",
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Link to read more, taken from the source email. Empty "
                            "string if the email carried no usable link."
                        ),
                    },
                    "read_deeper": {
                        "type": "boolean",
                        "description": (
                            "True for the one story named in the 'Worth reading "
                            "deeper' paragraph."
                        ),
                    },
                },
                "required": ["title", "why_it_matters", "source", "url", "read_deeper"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "script", "items"],
    "additionalProperties": False,
}


def build_prompt(emails) -> str:
    """Flatten the day's newsletters into one user turn."""
    blocks = []
    for item in emails:
        links = "\n".join(f"- {label}: {url}" for label, url in item.links)
        blocks.append(
            f"<email>\n"
            f"From: {item.sender}\n"
            f"Subject: {item.subject}\n"
            f"Received: {item.date:%Y-%m-%d %H:%M %Z}\n\n"
            f"{item.text}\n\n"
            f"Links in this email:\n{links or '(none)'}\n"
            f"</email>"
        )

    return (
        f"Here are today's {len(emails)} biopharma newsletters.\n\n"
        + "\n\n".join(blocks)
        + "\n\nWrite today's brief."
    )


def _strip_unsupported(schema: dict) -> dict:
    """Gemini and Cohere implement subsets of JSON Schema that reject
    additionalProperties. Anthropic requires it, so this only runs for the others."""
    cleaned = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(cleaned)
    return cleaned


def _summarize_anthropic(prompt: str) -> dict:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY must be set in the environment.")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": BRIEF_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "max_tokens":
        sys.exit(
            "The model hit max_tokens before finishing. Raise MAX_TOKENS in "
            "summarize.py, or lower EFFORT so less of the budget goes to thinking."
        )
    if response.stop_reason == "refusal":
        sys.exit("The model declined to write today's brief.")

    text = next(block.text for block in response.content if block.type == "text")
    brief = json.loads(text)
    brief["usage"] = {
        "model": ANTHROPIC_MODEL,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return brief


def _summarize_gemini(prompt: str) -> dict:
    from google import genai
    from google.genai import types

    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit(
            "GEMINI_API_KEY must be set in the environment. "
            "Create one at https://aistudio.google.com/apikey"
        )

    # `or` not `get(default)`: an unset GitHub Actions variable arrives as "".
    model = os.environ.get("GEMINI_MODEL") or GEMINI_MODEL
    client = genai.Client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_strip_unsupported(BRIEF_SCHEMA),
            max_output_tokens=MAX_TOKENS,
        ),
    )

    candidate = response.candidates[0] if response.candidates else None
    finish = getattr(candidate, "finish_reason", None)
    if finish and str(finish).endswith("MAX_TOKENS"):
        sys.exit(
            "Gemini hit max_output_tokens before finishing. Raise MAX_TOKENS in "
            "summarize.py — on the 2.5 models thinking draws from the same budget."
        )
    if not response.text:
        sys.exit(f"Gemini returned no text (finish_reason={finish}).")

    brief = json.loads(response.text)
    usage = response.usage_metadata
    brief["usage"] = {
        "model": model,
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
    }
    return brief


def _summarize_cohere(prompt: str) -> dict:
    import cohere

    # The SDK's own env var is CO_API_KEY; accept COHERE_API_KEY too so the name
    # matches the other providers, and pass it explicitly either way.
    api_key = os.environ.get("COHERE_API_KEY") or os.environ.get("CO_API_KEY")
    if not api_key:
        sys.exit(
            "COHERE_API_KEY must be set in the environment. "
            "Create one at https://dashboard.cohere.com/api-keys"
        )

    # `or` not `get(default)`: an unset GitHub Actions variable arrives as "".
    model = os.environ.get("COHERE_MODEL") or COHERE_MODEL
    client = cohere.ClientV2(api_key=api_key)
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        # v2 spells this `json_schema`; `schema` is the v1 shape and is ignored here.
        response_format={
            "type": "json_object",
            "json_schema": _strip_unsupported(BRIEF_SCHEMA),
        },
        max_tokens=MAX_TOKENS,
    )

    finish = getattr(response, "finish_reason", None)
    if finish and str(finish).upper().endswith("MAX_TOKENS"):
        sys.exit(
            "Cohere hit max_tokens before finishing. Raise MAX_TOKENS in "
            "summarize.py, or check the model's output ceiling — command-a-03-2025 "
            "caps at 8k."
        )

    blocks = getattr(response.message, "content", None) or []
    text = "".join(b.text for b in blocks if getattr(b, "type", "text") == "text")
    if not text:
        sys.exit(f"Cohere returned no text (finish_reason={finish}).")

    brief = json.loads(text)
    tokens = getattr(getattr(response, "usage", None), "tokens", None)
    brief["usage"] = {
        "model": model,
        "input_tokens": getattr(tokens, "input_tokens", None),
        "output_tokens": getattr(tokens, "output_tokens", None),
    }
    return brief


def summarize(emails, provider: str | None = None) -> dict:
    """Write the brief with whichever provider is configured."""
    provider = (provider or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    prompt = build_prompt(emails)

    writers = {
        "anthropic": _summarize_anthropic,
        "gemini": _summarize_gemini,
        "cohere": _summarize_cohere,
    }
    if provider not in writers:
        sys.exit(f"Unknown provider {provider!r}. Use one of: {', '.join(writers)}.")
    return writers[provider](prompt)


def to_plain_text(script: str) -> str:
    """Strip anything Alexa would read aloud or reject, and normalize spacing.

    Amazon rejects SSML/HTML/XML tags in a Flash Briefing text item, and a stray
    asterisk or hash gets spoken. The model is told not to emit these; this is the
    net under that.
    """
    text = script.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<[^>]+>", "", text)          # any tag
    text = re.sub(r"[*_`#|]+", "", text)          # markdown emphasis and table pipes
    # Bullet leaders. Match only horizontal space — \s here would swallow the
    # blank lines between paragraphs, which is what gives the brief its pacing.
    text = re.sub(r"^[ \t]*[-•][ \t]+", "", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def enforce_limit(text: str, limit: int = MAX_MAINTEXT_CHARS) -> tuple[str, bool]:
    """Cut to the last complete sentence under `limit`. Returns (text, was_cut)."""
    if len(text) <= limit:
        return text, False

    window = text[:limit]
    # Prefer a paragraph break, then a sentence end, so the cut sounds deliberate.
    for boundary in (window.rfind("\n\n"), max(window.rfind(c) for c in ".!?")):
        if boundary > limit // 2:
            return window[: boundary + 1].strip(), True
    return window.strip(), True


def word_count(script: str) -> int:
    return len(script.split())


def spoken_seconds(script: str) -> int:
    return round(word_count(script) / WORDS_PER_MINUTE * 60)
