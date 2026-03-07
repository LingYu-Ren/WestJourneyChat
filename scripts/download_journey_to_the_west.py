from __future__ import annotations

import argparse
import html
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://zh.wikisource.org"
BOOK_TITLE = "西遊記"
INDEX_TITLE = BOOK_TITLE
USER_AGENT = "Mozilla/5.0 (compatible; JourneyToTheWestDownloader/1.0)"
CHAPTER_PATTERN = re.compile(r"^\*\[\[/([^|\]]+)\|([^\]]+)\]\]", re.MULTILINE)
LINK_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")
ALT_TEMPLATE_PATTERN = re.compile(r"\{\{另\|([^|{}]+)(?:\|([^{}]+))?\}\}")


@dataclass(frozen=True)
class Chapter:
    slug: str
    label: str

    @property
    def number(self) -> str:
        match = re.search(r"(\d{3})", self.slug)
        if not match:
            raise ValueError(f"Unable to parse chapter number from: {self.slug}")
        return match.group(1)


def fetch_raw_wikitext(page_title: str) -> str:
    encoded_title = quote(page_title, safe="/")
    url = f"{BASE_URL}/wiki/{encoded_title}?action=raw"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_chapters(index_wikitext: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    for slug, label in CHAPTER_PATTERN.findall(index_wikitext):
        if re.fullmatch(r"第\d{3}回", slug):
            chapters.append(Chapter(slug=slug, label=label))

    if not chapters:
        raise RuntimeError("No chapter entries were found in the index page.")
    return chapters


def extract_section_text(raw_wikitext: str) -> str:
    match = re.search(r"^\|\s*section\s*=\s*(.+)$", raw_wikitext, re.MULTILINE)
    if not match:
        return ""
    return clean_wikitext(match.group(1)).strip()


def remove_named_template(raw_wikitext: str, template_name: str) -> str:
    marker = "{{" + template_name
    start = raw_wikitext.find(marker)
    if start == -1:
        return raw_wikitext

    depth = 0
    index = start
    while index < len(raw_wikitext) - 1:
        pair = raw_wikitext[index : index + 2]
        if pair == "{{":
            depth += 1
            index += 2
            continue
        if pair == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                return raw_wikitext[:start] + raw_wikitext[index:]
            continue
        index += 1

    raise RuntimeError(f"Unclosed template block: {template_name}")


def strip_remaining_templates(text: str) -> str:
    pieces: list[str] = []
    index = 0
    depth = 0

    while index < len(text):
        pair = text[index : index + 2]
        if pair == "{{":
            depth += 1
            index += 2
            continue
        if pair == "}}" and depth:
            depth -= 1
            index += 2
            continue
        if depth == 0:
            pieces.append(text[index])
        index += 1

    return "".join(pieces)


def clean_wikitext(text: str) -> str:
    text = ALT_TEMPLATE_PATTERN.sub(lambda match: match.group(1), text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?nowiki>", "", text, flags=re.IGNORECASE)
    text = LINK_PATTERN.sub(_replace_link, text)
    text = strip_remaining_templates(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"(?m)^[:*#;]+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return html.unescape(text)


def _replace_link(match: re.Match[str]) -> str:
    content = match.group(1)
    if "|" in content:
        return content.split("|")[-1]
    return content


def render_chapter(raw_wikitext: str) -> str:
    title = extract_section_text(raw_wikitext)
    body = remove_named_template(raw_wikitext, "header")
    body = remove_named_template(body, "footer")
    body = clean_wikitext(body).strip()

    if title:
        return f"{title}\n\n{body}\n"
    return f"{body}\n"


def write_book(output_dir: Path, chapters: list[Chapter], delay_seconds: float) -> None:
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    collected_parts: list[str] = []
    for chapter in chapters:
        raw_wikitext = fetch_raw_wikitext(f"{BOOK_TITLE}/{chapter.slug}")
        chapter_text = render_chapter(raw_wikitext)

        chapter_file = chapters_dir / f"chapter_{chapter.number}.txt"
        chapter_file.write_text(chapter_text, encoding="utf-8")
        collected_parts.append(chapter_text.rstrip())

        print(f"Saved {chapter_file.name}")
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    book_file = output_dir / "journey_to_the_west.txt"
    book_file.write_text("\n\n".join(collected_parts) + "\n", encoding="utf-8")
    print(f"Saved {book_file.name}")


def parse_args() -> argparse.Namespace:
    default_output = Path(__file__).resolve().parents[1] / "data" / "journey_to_the_west"
    parser = argparse.ArgumentParser(
        description="Download Journey to the West chapters from Chinese Wikisource."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory used for output files (default: {default_output})",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.2,
        help="Delay between requests so the site is not hammered.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    index_wikitext = fetch_raw_wikitext(INDEX_TITLE)
    chapters = parse_chapters(index_wikitext)
    write_book(args.output_dir, chapters, args.delay_seconds)


if __name__ == "__main__":
    main()
